"""
Abstração de exchange: o vocabulário comum que MEXC, Gate e BingX passam a falar.

## Por que esta camada existe

Até aqui o sistema inteiro era MEXC-específico: `spot_symbol`/`futures_symbol`
embutidos no estado, `mexc_rest`, `mexc_ws_*`. Adicionar duas exchanges por
cópia-e-cola multiplicaria por três cada bug já corrigido, e cada correção
futura teria que ser lembrada em três lugares.

Aqui um par deixa de ser "spot e futures" e passa a ser um **símbolo
canônico** (`BTC`) cotado em vários **venues** (`mexc:spot`, `gate:futures`,
`bingx:futures`...). As combinações de arbitragem são derivadas depois, a
partir de quaisquer dois venues.

## As três armadilhas que esta camada resolve

**1. Ordenação do book.** Cada exchange devolve as camadas na ordem que
quiser. Medido em 05/08/2026, nos seis venues:

    gate spot     bids=DECR   asks=CRESC
    gate futures  bids=DECR   asks=CRESC
    bingx spot    bids=DECR   asks=DECR    <-- único fora do padrão
    bingx swap    bids=DECR   asks=CRESC
    mexc spot     bids=DECR   asks=CRESC
    mexc futures  bids=DECR   asks=CRESC

O spot da BingX devolve os asks do PIOR para o melhor. Ler `asks[0]` como
topo do book daria o pior preço disponível como se fosse o melhor, e o VWAP
sairia calculado consumindo o book ao contrário — um erro que não levanta
exceção, não aparece em nenhum log, e só se manifesta no preço de fill.
`build_order_book` normaliza sempre, e AVISA quando a ordem recebida
contradiz a declarada pelo adaptador (sinal de que a API mudou).

**2. Unidade da quantidade.** Book de spot vem na moeda base; book de futures
vem em CONTRATOS, e cada exchange tem seu multiplicador (`contractSize` na
MEXC, `quanto_multiplier` na Gate, `size` na BingX). Converter errado
descasa as pernas silenciosamente.

**3. Formato do símbolo.** `BTCUSDT` (MEXC spot), `BTC_USDT` (MEXC futures,
Gate) e `BTC-USDT` (BingX). O bug 2 deste projeto foi exatamente um símbolo
com formato divergente atravessando módulos — com três exchanges o risco
triplica, então a conversão canônico<->nativo vive só aqui.
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from bot.depth import BookLevel, OrderBook

logger = logging.getLogger("exchanges")


class MarketType(Enum):
    SPOT = "spot"
    FUTURES = "futures"


@dataclass(frozen=True)
class Venue:
    """
    Um mercado específico de uma exchange específica: `mexc:spot`,
    `gate:futures`, etc. É a unidade sobre a qual as combinações de
    arbitragem são montadas.
    """
    exchange: str
    market: MarketType

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.market.value}"

    @property
    def label(self) -> str:
        nome = {"spot": "Spot", "futures": "Futures"}[self.market.value]
        return f"{self.exchange.upper()} {nome}"

    def __str__(self) -> str:
        return self.key

    @staticmethod
    def from_key(key: str) -> "Venue":
        exchange, market = key.split(":", 1)
        return Venue(exchange=exchange, market=MarketType(market))


@dataclass
class Quote:
    """
    Cotação normalizada de um símbolo num venue.

    `bid`/`ask` são o TOPO do book — gatilho, nunca autorização de ordem (ver
    CLAUDE.md, "topo do book vs preço executável"). `last` só existe para
    diagnóstico e NUNCA deve alimentar decisão de trading.

    `book_ts` é o carimbo de quando bid/ask foram atualizados, medido POR
    VENUE. Frescor agregado esconde justamente a assimetria que produz spread
    fantasma — a lição do bug 12.
    """
    symbol: str            # canônico, ex: "BTC"
    venue: Venue
    native_symbol: str     # como a exchange chama, ex: "BTC-USDT"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    vol_usdt: float = 0.0
    funding_rate: float = 0.0
    book_ts: float = 0.0
    # Quantidade disponível NO TOPO do book, na unidade nativa do venue.
    # `None` = ainda não medida (nem toda API devolve isso no ticker em massa;
    # a BingX devolve, MEXC e Gate exigem consulta de profundidade).
    #
    # Distinguir "não medido" de "zero" é essencial: exibir 0 onde não se sabe
    # faria um book profundo parecer vazio, e é exatamente o tipo de número
    # inventado que este projeto não tolera na tela.
    bid_qty: Optional[float] = None
    ask_qty: Optional[float] = None
    top_ts: float = 0.0   # quando bid_qty/ask_qty foram medidos

    def top_usdt(self, side: str, contract_size: float = 1.0) -> Optional[float]:
        """
        Valor em USDT disponível no topo do book do lado pedido.

        É a resposta para "quanto dá para executar A ESTE PREÇO" — o número
        que separa um spread real de um spread que existe só para 5 dólares.
        Multiplica pelo `contract_size` porque em futures a quantidade está
        em contratos, não na moeda base.
        """
        qty = self.bid_qty if side == "bid" else self.ask_qty
        preco = self.bid if side == "bid" else self.ask
        if qty is None or not preco:
            return None
        return qty * contract_size * preco

    @property
    def has_book(self) -> bool:
        """Só cotações com os dois lados do book servem para decidir qualquer coisa."""
        return bool(self.bid and self.ask and self.bid > 0 and self.ask > 0)

    def age_s(self, now: Optional[float] = None) -> float:
        if not self.book_ts:
            return float("inf")
        return (now if now is not None else time.time()) - self.book_ts


@dataclass
class ContractSpec:
    """
    Metadados de execução de um símbolo num venue.

    `contract_size` é 1.0 em spot (a quantidade JÁ está na moeda base) e o
    multiplicador do contrato em futures. Manter o campo nos dois casos
    permite que todo o resto do código faça `qty_base = qty_nativa *
    contract_size` sem se perguntar em que mercado está — a conversão
    condicional espalhada é o que produz erro de unidade.
    """
    symbol: str
    venue: Venue
    native_symbol: str
    contract_size: float = 1.0
    qty_step: float = 0.0        # menor incremento de quantidade aceito
    price_tick: float = 0.0      # menor incremento de preço aceito
    min_qty: float = 0.0
    min_notional: float = 0.0
    taker_fee_pct: float = 0.05  # percentual, não fração


# ---------------------------------------------------------------------------
# Normalização de book — a parte que evita erro silencioso de preço
# ---------------------------------------------------------------------------

def _esta_ordenado(precos: Sequence[float], crescente: bool) -> bool:
    if len(precos) < 2:
        return True
    if crescente:
        return all(precos[i] <= precos[i + 1] for i in range(len(precos) - 1))
    return all(precos[i] >= precos[i + 1] for i in range(len(precos) - 1))


def build_order_book(
    symbol: str,
    bids: list[BookLevel],
    asks: list[BookLevel],
    *,
    venue: Venue,
    declared_bid_order: str = "desc",
    declared_ask_order: str = "asc",
    ts: Optional[float] = None,
) -> OrderBook:
    """
    Monta um `OrderBook` com bids do maior para o menor e asks do menor para o
    maior — a ordem em que uma ordem a mercado realmente consome as camadas,
    e a única ordem que `walk_by_qty` interpreta corretamente.

    A ordenação é SEMPRE aplicada; `declared_*_order` diz apenas o que o
    adaptador espera receber daquela exchange. Quando o recebido contradiz o
    declarado, isso é registrado em WARNING: significa que a API mudou de
    comportamento, e é a única chance de perceber isso antes de o preço errado
    virar ordem.

    `bot/depth.py` deliberadamente NÃO reordena (reordenar lá mascararia um
    payload corrompido). A responsabilidade fica aqui, no adaptador, que é
    quem conhece a convenção de cada exchange.
    """
    for nome, niveis, declarado, crescente in (
        ("bids", bids, declared_bid_order, False),
        ("asks", asks, declared_ask_order, True),
    ):
        if not niveis:
            continue
        precos = [n.price for n in niveis]
        esperado_crescente = declarado == "asc"
        if not _esta_ordenado(precos, esperado_crescente):
            logger.warning(
                "Book de %s em %s: %s chegaram fora da ordem declarada (%s). "
                "A API pode ter mudado — a ordenação foi corrigida, mas confira o adaptador.",
                symbol, venue.key, nome, declarado,
            )

    return OrderBook(
        symbol=symbol,
        bids=sorted(bids, key=lambda n: n.price, reverse=True),
        asks=sorted(asks, key=lambda n: n.price),
        ts=ts if ts is not None else time.time(),
    )


def levels_from_pairs(raw: Sequence, *, price_idx=0, qty_idx=1) -> list[BookLevel]:
    """
    Converte `[[preco, qtd], ...]` (o formato de MEXC, Gate spot e BingX) em
    camadas. Preços vêm como string em várias das APIs; comparar string daria
    ordenação lexicográfica silenciosamente errada, então a conversão para
    float é obrigatória e não opcional.

    Camadas inválidas (preço ou quantidade não numérico, zerado ou negativo)
    são descartadas em vez de contaminar o VWAP.
    """
    niveis = []
    for item in raw or []:
        try:
            preco = float(item[price_idx])
            qtd = float(item[qty_idx])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if preco > 0 and qtd > 0:
            niveis.append(BookLevel(preco, qtd))
    return niveis


def levels_from_dicts(raw: Sequence, *, price_key="p", qty_key="s") -> list[BookLevel]:
    """
    Converte `[{"p": preco, "s": tamanho}, ...]` — o formato do book de
    futures da Gate, que é o único que usa objetos em vez de listas.
    """
    niveis = []
    for item in raw or []:
        try:
            preco = float(item[price_key])
            qtd = float(item[qty_key])
        except (TypeError, ValueError, KeyError):
            continue
        if preco > 0 and qtd > 0:
            niveis.append(BookLevel(preco, qtd))
    return niveis


# ---------------------------------------------------------------------------
# Interface do adaptador
# ---------------------------------------------------------------------------

class ExchangeAdapter(ABC):
    """
    Contrato que todo venue precisa cumprir para entrar no dashboard e no bot.

    Só dados PÚBLICOS: nenhum método aqui exige credencial. Isso é
    deliberado — o monitoramento e a confirmação por profundidade precisam
    funcionar para exchanges em que o usuário ainda não tem conta, e nenhuma
    chamada de leitura de mercado deve carregar chave junto.
    """

    #: identificador curto e estável ("mexc", "gate", "bingx")
    name: str
    #: mercado que este adaptador cobre
    market: MarketType

    def __init__(self, http_client):
        self._client = http_client

    @property
    def venue(self) -> Venue:
        return Venue(exchange=self.name, market=self.market)

    # -- símbolos --

    @abstractmethod
    def to_native(self, symbol: str) -> str:
        """Converte o símbolo canônico ('BTC') para o formato da exchange."""

    @abstractmethod
    def to_canonical(self, native_symbol: str) -> Optional[str]:
        """
        Converte o símbolo nativo para canônico, ou None se não for um par
        USDT que nos interesse.
        """

    # -- dados de mercado --

    @abstractmethod
    async def fetch_tickers(self) -> dict[str, Quote]:
        """
        Todas as cotações do venue numa única chamada, indexadas pelo símbolo
        CANÔNICO. Uma chamada só é obrigatório: com 3 exchanges e centenas de
        símbolos, uma requisição por par estouraria qualquer rate limit.
        """

    @abstractmethod
    async def fetch_depth(self, symbol: str, limit: int = 20) -> Optional[OrderBook]:
        """
        Livro de ofertas do símbolo, já normalizado (bids desc, asks asc).
        Quantidades na unidade NATIVA do venue — contratos em futures. A
        conversão para moeda base usa `ContractSpec.contract_size`.
        """

    @abstractmethod
    async def fetch_specs(self, symbols: list[str]) -> dict[str, ContractSpec]:
        """Metadados de execução dos símbolos pedidos, indexados por canônico."""

    # -- utilidades comuns --

    def _headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

    async def _get(self, url: str, params: Optional[dict] = None, timeout: float = 12.0):
        resp = await self._client.get(url, params=params, headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
