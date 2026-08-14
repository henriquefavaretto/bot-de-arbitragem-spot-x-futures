"""
Máquina de estados do bot de arbitragem Spot x Futures, por par.

FASE 2: este módulo roda exclusivamente em modo SIMULATION. Não existe,
neste arquivo, nenhum caminho de código capaz de enviar uma ordem real -
os métodos "_execute_entry" e "_execute_exit" apenas logam a decisão e
atualizam o estado interno como se a ordem tivesse sido preenchida ao
preço de mercado no momento da decisão. Isso permite validar a lógica de
entrada/saída, o casamento de valores entre pernas, e o comportamento em
reconexão - sem nenhum risco de capital.

A Fase 3 vai introduzir um ExecutionMode.LIVE que troca essas duas funções
por chamadas reais aos clientes REST (bot/mexc_spot_client.py e
bot/mexc_futures_client.py), sem alterar a máquina de estados em si.

Estados possíveis por par:
    IDLE          - monitorando, sem posição, esperando spread de entrada
    ENTERING      - ordens de entrada "enviadas" (simulado), aguardando fill
    OPEN          - posição aberta nas duas pernas, monitorando spread de saída
    EXITING       - ordens de saída "enviadas" (simulado), aguardando fill total
    PAUSED_ERROR  - pausado automaticamente (erro/discrepância) - nunca abre posição nova
    MANUAL_HALT   - pausado manualmente (kill switch ou toggle do usuário)
"""
import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bot.bot_storage import BotStorage
from bot.costs import (
    DEFAULT_FUTURES_TAKER_PCT, DEFAULT_SPOT_TAKER_PCT, FeeModel,
    evaluate_entry, realized_pnl_pct,
)
from bot.depth import (
    ExecutableSpread, OrderBook, entry_executable_spread, exit_executable_spread,
    parse_futures_depth, parse_spot_depth,
)
from bot.execution import (
    LegFill, SlippagePolicy, execute_bounded, limit_price_for_buy, limit_price_for_sell,
)
from bot.sizing import (
    ContractSpec, usdt_to_futures_vol, futures_vol_to_usdt,
    SpotSymbolSpec, round_spot_quantity, spot_price_tick,
)
from bot.venue_trader import UnknownOrderStateError, VenueTrader, build_trader
from exchanges.base import ContractSpec as VenueSpec, Venue

logger = logging.getLogger("bot_engine")

# Idade máxima aceita para o book usado numa decisão de execução.
#
# Antes desta trava, o motor decidia em cima do último bid/ask conhecido, sem
# olhar QUANDO ele chegou. Isso é perigoso justamente nos pares que o bot
# opera: o canal individual de futures (`sub.ticker`) só empurra atualização
# quando há negócios, então num par ilíquido o bid/ask podia ficar congelado
# por minutos enquanto o spot continuava se movendo — e o spread na tela
# virava ficção, calculado com um lado velho e outro novo.
DEFAULT_MAX_BOOK_AGE_S = 3.0

# Intervalo mínimo entre duas confirmações de profundidade do MESMO par.
# A confirmação custa duas chamadas REST; sem essa trava, um par cujo spread
# fica oscilando em volta do limiar geraria centenas de chamadas por minuto e
# esbarraria no rate limit da MEXC justamente na hora de operar.
DEFAULT_DEPTH_CONFIRM_INTERVAL_S = 0.5

# Camadas de book buscadas por consulta. 20 cobre com folga os tamanhos que
# esta estratégia opera; buscar mais só aumenta latência da decisão.
DEFAULT_DEPTH_LIMIT = 20

# Spread de entrada acima do qual a operação é recusada por IMPLAUSIBILIDADE.
#
# Arbitragem real desta estratégia vive na casa de 1-3%. Dois dígitos quase
# sempre significam que os dois lados NÃO SÃO O MESMO ATIVO -- redenominação,
# migração para token v2, ou mercado pré-lançamento.
#
# Caso real e ativo nesta conta: COTI. O spot cota ~0,0106 e o futures ~0,0119
# de forma ESTRUTURAL e persistente (+11%), porque a MEXC migrou o token e os
# dois mercados referenciam versões diferentes. Montar essa "arbitragem"
# compraria um ativo e venderia OUTRO: exposição direcional integral, hedge
# nenhum, e a "convergência" esperada nunca vem porque a diferença não é uma
# deslocação temporária -- é o que os dois ativos valem.
#
# O dashboard multi-exchange já barrava isso via consenso entre venues. O
# caminho MEXC-only não tinha nenhuma proteção equivalente, e é justamente ele
# que envia ordem.
DEFAULT_MAX_PLAUSIBLE_ENTRY_PCT = 10.0

# Venues cuja execução foi VALIDADA CONTRA A API REAL.
#
# A distinção entre "implementado" e "validado" é a lição mais cara deste
# projeto. A execução dos seis venues está implementada e coberta por testes
# (`bot/venue_trader.py`, `tests/test_venue_clients.py`), mas os bugs 4, 5, 6,
# 14, 16 e 17 foram TODOS comportamentos de API que passaram por testes contra
# dublês e só apareceram na conta real:
#
#     bug  5  a MEXC rejeita quantidade com casas decimais a mais
#     bug  6  a MEXC responde `executedQty=0` numa ordem que executou
#     bug 14  IOC parcial termina CANCELED com dinheiro real dentro
#     bug 16  o endpoint de ordens aceita ~2 ordens a cada 2s e recusa o resto
#     bug 17  a MEXC spot ACEITA e IGNORA `timeInForce=IOC`
#
# Nenhum deles era descobrível lendo a documentação. Por isso um venue só
# entra aqui depois de uma ordem real mínima ter sido enviada e conferida:
# rode `python -m bot.validate_venue <venue>` e siga as instruções.
#
# Ampliar esta lista sem essa validação é apostar que Gate e BingX não têm
# nenhuma esquisitice equivalente — e as três exchanges já divergem em coisas
# tão básicas quanto a unidade de uma compra a mercado.
DEFAULT_VALIDATED_VENUES = frozenset({"mexc:spot", "mexc:futures"})

TRADER_VENUE_KEYS = (
    "mexc:spot", "mexc:futures", "gate:spot", "gate:futures",
    "bingx:spot", "bingx:futures",
)


def _validated_venues_from_env() -> frozenset:
    """
    Venues liberados para execução, lidos de `BOT_VALIDATED_VENUES`.

    Fica em variável de ambiente, e não na UI, pelo mesmo motivo que
    `MEXC_BOT_LIVE_MODE`: liberar uma exchange nova para dinheiro real é uma
    decisão deliberada, tomada uma vez com o `.env` aberto, não um clique
    possível durante a operação.
    """
    extra = os.getenv("BOT_VALIDATED_VENUES", "").strip()
    if not extra:
        return DEFAULT_VALIDATED_VENUES
    informados = {v.strip() for v in extra.split(",") if v.strip()}
    desconhecidos = informados - set(TRADER_VENUE_KEYS)
    if desconhecidos:
        raise ValueError(
            f"BOT_VALIDATED_VENUES contém venue(s) sem executor: {', '.join(sorted(desconhecidos))}. "
            f"Válidos: {', '.join(sorted(TRADER_VENUE_KEYS))}"
        )
    return DEFAULT_VALIDATED_VENUES | informados


class ExecutionMode(Enum):
    SIMULATION = "simulation"
    LIVE = "live"  # não utilizado nesta fase


class PairState(Enum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    PAUSED_ERROR = "PAUSED_ERROR"
    MANUAL_HALT = "MANUAL_HALT"


@dataclass
class PairConfig:
    symbol: str
    enabled: bool = False
    entry_spread_pct: float = 1.0   # entra quando |spread| >= este valor
    exit_spread_pct: float = 0.2    # sai quando |spread| <= este valor (reverteu boa parte do movimento)
    position_size_usdt: float = 100.0
    # ONDE operar. Cada par fica travado numa combinacao escolhida na tela; o
    # bot NUNCA troca de venue sozinho. Trocar por conta propria significaria
    # abrir num lugar e tentar fechar em outro -- o pior desfecho possivel.
    buy_venue: str = "mexc:spot"
    sell_venue: str = "mexc:futures"

    @property
    def combo_key(self) -> str:
        return f"{self.symbol}|{self.buy_venue}|{self.sell_venue}"

    @property
    def cross_exchange(self) -> bool:
        return self.buy_venue.split(":")[0] != self.sell_venue.split(":")[0]


@dataclass
class PairRuntime:
    """Estado em memória (não persistido diretamente, espelha o bot_storage)."""
    symbol: str
    state: PairState = PairState.IDLE
    entry_spread_pct: Optional[float] = None
    entry_spot_price: Optional[float] = None
    entry_futures_price: Optional[float] = None
    entry_spot_qty: Optional[float] = None
    entry_futures_vol: Optional[float] = None
    entry_notional_usdt: Optional[float] = None
    entry_ts: Optional[float] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "entry_spread_pct": self.entry_spread_pct,
            "entry_spot_price": self.entry_spot_price,
            "entry_futures_price": self.entry_futures_price,
            "entry_spot_qty": self.entry_spot_qty,
            "entry_futures_vol": self.entry_futures_vol,
            "entry_notional_usdt": self.entry_notional_usdt,
            "entry_ts": self.entry_ts,
            "last_error": self.last_error,
        }


class ArbitrageBotEngine:
    """
    Motor do bot. Recebe atualizações de preço (spot_price, futures_price) do
    engine de monitoramento já existente (dashboard/engine.py) via
    `on_price_update`, e decide entradas/saídas por par com base na config.
    """

    def __init__(
        self,
        bot_storage: BotStorage,
        execution_mode: ExecutionMode = ExecutionMode.SIMULATION,
        spot_client=None,
        futures_client=None,
        max_total_exposure_usdt: Optional[float] = None,
        slippage_policy: Optional[SlippagePolicy] = None,
        market_spot_client=None,
        market_futures_client=None,
        max_book_age_s: float = DEFAULT_MAX_BOOK_AGE_S,
        depth_limit: int = DEFAULT_DEPTH_LIMIT,
        depth_confirm_interval_s: float = DEFAULT_DEPTH_CONFIRM_INTERVAL_S,
        expected_hold_hours: float = 1.0,
        min_net_pct: float = 0.0,
        max_plausible_entry_pct: float = DEFAULT_MAX_PLAUSIBLE_ENTRY_PCT,
        spot_limit_wait_s: float = 1.5,
        venue_clients: Optional[dict] = None,
        venue_adapters: Optional[dict] = None,
        allow_cross_exchange: bool = False,
        validated_venues: Optional[frozenset] = None,
    ):
        self.storage = bot_storage
        self.execution_mode = execution_mode
        self.spot_client = spot_client      # bot.mexc_spot_client.MexcSpotClient (obrigatório se LIVE)
        self.futures_client = futures_client  # bot.mexc_futures_client.MexcFuturesClient (obrigatório se LIVE)
        # Clientes usados só para LER book (endpoints públicos de depth). São
        # separados dos de execução de propósito: a confirmação por
        # profundidade precisa funcionar também em modo SIMULAÇÃO, senão a
        # simulação continuaria mentindo do mesmo jeito que a tela mentia, e
        # deixaria de servir para validar a estratégia antes do dinheiro real.
        self.market_spot_client = market_spot_client or spot_client
        self.market_futures_client = market_futures_client or futures_client
        self.max_total_exposure_usdt = max_total_exposure_usdt  # None = sem teto
        self.slippage_policy = slippage_policy or SlippagePolicy()
        self.max_book_age_s = max_book_age_s
        self.depth_limit = depth_limit
        self.depth_confirm_interval_s = depth_confirm_interval_s
        self.expected_hold_hours = expected_hold_hours
        # Quanto tempo a ordem LIMITE de spot fica viva antes de ser
        # cancelada. É o que emula IOC numa exchange que não tem IOC: curto o
        # bastante para não virar uma ordem passiva esquecida, longo o
        # bastante para o motor de matching processar o que dava para pegar.
        self.spot_limit_wait_s = spot_limit_wait_s
        self.min_net_pct = min_net_pct
        self.max_plausible_entry_pct = max_plausible_entry_pct
        self.configs: dict[str, PairConfig] = {}
        self.runtimes: dict[str, PairRuntime] = {}
        self.contract_specs: dict[str, ContractSpec] = {}
        self.spot_specs: dict[str, SpotSymbolSpec] = {}
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self.connection_degraded = False  # true = internet caiu / MEXC instável -> pausa novas entradas
        # Cliente autenticado por venue ("mexc:spot" -> MexcSpotClient, ...).
        # None significa "sem credencial": a config recusa o par em LIVE, mas
        # a SIMULACAO segue funcionando -- validar a estrategia nao deveria
        # exigir chave de API.
        self.venue_clients: dict[str, object] = venue_clients or {
            "mexc:spot": spot_client, "mexc:futures": futures_client,
            "gate:spot": None, "gate:futures": None,
            "bingx:spot": None, "bingx:futures": None,
        }
        # Adaptador publico por venue, usado para ler profundidade e metadados
        # (contractSize, tick, taxa) do venue configurado -- nao mais so MEXC.
        self.venue_adapters: dict[str, object] = venue_adapters or {}
        self.allow_cross_exchange = allow_cross_exchange
        self.validated_venues = (
            validated_venues if validated_venues is not None else _validated_venues_from_env()
        )
        # ContractSpec por (simbolo, venue), carregado sob demanda.
        self.venue_specs: dict[tuple[str, str], object] = {}
        # Executor por (simbolo, venue). Cacheado porque ele carrega o estado
        # de "ordem de destino desconhecido": recriá-lo a cada ordem apagaria
        # justamente a trava que impede mandar a próxima às cegas.
        self._traders: dict[tuple[str, str], VenueTrader] = {}
        self._last_depth_check: dict[str, float] = {}
        # Pares com uma decisão em voo. `on_price_update` é chamado por várias
        # fontes assíncronas (WS de spot, WS de futures, dois loops de polling
        # REST) e nada impedia duas delas de verem o mesmo par em IDLE e
        # dispararem DUAS entradas para a mesma oportunidade — o estado só
        # mudava para ENTERING depois de vários `await`. Este conjunto é
        # marcado de forma síncrona, antes de qualquer await, e é o que
        # garante uma decisão por par por vez.
        self._busy: set[str] = set()
        # Último preço visto por par, usado só para validar o tamanho mínimo
        # no momento da configuração (não entra em nenhuma decisão de ordem).
        self._last_price_hint: dict[str, float] = {}
        # Limita o log da trava de venue: sem isso ele sairia a cada tick.
        self._venue_warn_ts: dict[str, float] = {}
        # Estatística de diagnóstico: quantas vezes o topo do book prometeu um
        # spread que a profundidade real não confirmou. É o número que mede
        # diretamente o problema que motivou toda esta camada.
        self.depth_rejections: dict[str, int] = {}

        if self.execution_mode == ExecutionMode.LIVE and (spot_client is None or futures_client is None):
            raise RuntimeError(
                "ExecutionMode.LIVE exige spot_client e futures_client configurados. "
                "Sem eles, o motor não tem como enviar ordens reais."
            )

    # ---------------- Custos e profundidade ----------------

    def fees_for(self, symbol: str) -> FeeModel:
        """
        Taxas efetivas do par NOS VENUES EM QUE ELE OPERA.

        A taxa precisa vir do venue configurado, nunca de um padrão: a Gate
        cobra 0,075% de taker no futures contra 0,02% da MEXC — 3,75x. Numa
        estratégia cuja margem inteira vive na casa de 1-3%, quatro pernas com
        a taxa errada mudam o sinal do resultado esperado, e o bot entraria
        convicto numa operação que perde dinheiro por construção.

        A ordem de preferência é a mesma de `_venue_spec`: o que o venue
        reporta primeiro, os metadados da MEXC depois (caminho só-MEXC), e o
        padrão conservador por último.
        """
        cfg = self.configs.get(symbol)
        buy_venue = cfg.buy_venue if cfg else "mexc:spot"
        sell_venue = cfg.sell_venue if cfg else "mexc:futures"

        spot_venue_spec = self.venue_specs.get((symbol, buy_venue))
        futures_venue_spec = self.venue_specs.get((symbol, sell_venue))
        spot_spec = self.spot_specs.get(symbol)
        contract_spec = self.contract_specs.get(symbol)

        if spot_venue_spec is not None:
            spot_taker = spot_venue_spec.taker_fee_pct
        elif spot_spec is not None:
            spot_taker = spot_spec.taker_fee_pct
        else:
            spot_taker = DEFAULT_SPOT_TAKER_PCT

        if futures_venue_spec is not None:
            futures_taker = futures_venue_spec.taker_fee_pct
        elif contract_spec is not None:
            futures_taker = contract_spec.taker_fee_pct
        else:
            futures_taker = DEFAULT_FUTURES_TAKER_PCT

        return FeeModel(spot_taker_pct=spot_taker, futures_taker_pct=futures_taker)

    # ---------------- Execução por venue ----------------

    def _venue_spec(self, symbol: str, venue_key: str) -> Optional[VenueSpec]:
        """
        `ContractSpec` genérico (exchanges/base.py) do símbolo naquele venue.

        Existem DOIS tipos com esse nome no projeto: o de `bot/sizing.py`,
        específico do contrato de futures da MEXC, e o genérico da camada de
        exchanges. Esta função é a única ponte entre eles.

        A ordem de preferência importa: o spec vindo do ADAPTADOR do venue é a
        fonte correta (traz o formato nativo do símbolo, o tick e o passo
        daquela exchange). A derivação a partir dos specs da MEXC existe como
        caminho para quando o motor roda sem adaptadores — que é o caso da
        suíte de testes e de qualquer instalação só-MEXC.
        """
        pronto = self.venue_specs.get((symbol, venue_key))
        if pronto is not None:
            return pronto

        if venue_key == "mexc:futures":
            cs = self.contract_specs.get(symbol)
            if cs is None:
                return None
            return VenueSpec(
                symbol=symbol, venue=Venue.from_key(venue_key),
                native_symbol=f"{symbol}_USDT",
                contract_size=cs.contract_size,
                # `vol_scale` são as casas decimais aceitas em contratos; o
                # passo é 10^-escala (escala 0 = contratos inteiros).
                qty_step=10 ** (-cs.vol_scale),
                price_tick=cs.price_unit,
                min_qty=cs.min_vol,
                taker_fee_pct=cs.taker_fee_pct,
            )

        if venue_key == "mexc:spot":
            ss = self.spot_specs.get(symbol)
            return VenueSpec(
                symbol=symbol, venue=Venue.from_key(venue_key),
                native_symbol=f"{symbol}USDT",
                contract_size=1.0,
                qty_step=10 ** (-ss.base_asset_precision) if ss else 1e-6,
                price_tick=spot_price_tick(ss) if ss else 1e-8,
                taker_fee_pct=ss.taker_fee_pct if ss else DEFAULT_SPOT_TAKER_PCT,
            )

        return None

    async def ensure_venue_specs(self, cfg: PairConfig) -> bool:
        """
        Garante que os metadados de execução dos DOIS venues do par estejam
        carregados, buscando-os no adaptador quando faltarem.

        Sem isto, `_venue_spec` só conhece os venues da MEXC (derivados dos
        specs que o motor já carrega) e qualquer par em Gate ou BingX ficaria
        eternamente sem executor — recusado com segurança, mas nunca operável.

        Os metadados são buscados UMA vez por (símbolo, venue) e guardados: são
        `contractSize`, tick e passo, que não mudam durante uma sessão, e
        buscá-los na hora da decisão colocaria uma chamada REST no caminho
        crítico entre ver a oportunidade e mandar a ordem.
        """
        ok = True
        for venue_key in (cfg.buy_venue, cfg.sell_venue):
            if (cfg.symbol, venue_key) in self.venue_specs:
                continue
            # A MEXC não precisa: `_venue_spec` deriva dos specs já carregados.
            if venue_key.startswith("mexc:"):
                continue
            adaptador = self.venue_adapters.get(venue_key)
            if adaptador is None:
                ok = False
                continue
            try:
                specs = await adaptador.fetch_specs([cfg.symbol])
            except Exception as e:
                logger.warning(
                    "Metadados de execução de %s em %s indisponíveis: %s", cfg.symbol, venue_key, e,
                )
                ok = False
                continue
            spec = specs.get(cfg.symbol)
            if spec is None:
                logger.warning(
                    "O venue %s não devolveu metadados para %s — o par não será operado ali.",
                    venue_key, cfg.symbol,
                )
                ok = False
                continue
            self.venue_specs[(cfg.symbol, venue_key)] = spec
        return ok

    def trader_for(self, symbol: str, venue_key: str) -> Optional[VenueTrader]:
        """
        Executor do símbolo naquele venue, ou None se faltar cliente ou spec.

        Devolver None em vez de levantar é deliberado: quem chama está sempre
        num caminho que sabe recusar a operação inteira, e recusar ANTES de
        qualquer ordem é a diferença entre "não operou" e "operou pela metade".
        """
        chave = (symbol, venue_key)
        pronto = self._traders.get(chave)
        if pronto is not None:
            return pronto

        cliente = self.venue_clients.get(venue_key)
        if cliente is None:
            return None
        spec = self._venue_spec(symbol, venue_key)
        if spec is None:
            return None

        trader = build_trader(
            Venue.from_key(venue_key), spec, cliente, settle_wait_s=self.spot_limit_wait_s,
        )
        self._traders[chave] = trader
        return trader

    def _traders_for_pair(self, cfg: PairConfig) -> tuple[Optional[VenueTrader], Optional[VenueTrader]]:
        """(executor da perna COMPRADA, executor da perna VENDIDA)."""
        return (
            self.trader_for(cfg.symbol, cfg.buy_venue),
            self.trader_for(cfg.symbol, cfg.sell_venue),
        )

    def _depth_throttle_ok(self, symbol: str) -> bool:
        now = time.time()
        if now - self._last_depth_check.get(symbol, 0.0) < self.depth_confirm_interval_s:
            return False
        self._last_depth_check[symbol] = now
        return True

    async def fetch_books(self, symbol: str, cfg: Optional[PairConfig] = None):
        """
        Busca os dois livros de ofertas EM PARALELO, DOS VENUES CONFIGURADOS
        para o par, e devolve `(book_da_perna_comprada, book_da_perna_vendida)`.

        Paralelo, e nao em sequencia, porque os dois snapshots precisam
        descrever o mesmo instante de mercado: buscar um depois do outro
        introduziria entre eles a latencia de uma chamada REST inteira, e o
        spread calculado a partir de dois momentos diferentes e exatamente o
        tipo de numero que parece valido e nao e. Isso vale ainda mais entre
        exchanges diferentes, onde as latencias nao sao nem parecidas.

        Retorna None se qualquer um dos lados falhar, vier vazio ou vier
        cruzado -- nunca devolve um book "meio bom".
        """
        compra_venue = cfg.buy_venue if cfg else "mexc:spot"
        venda_venue = cfg.sell_venue if cfg else "mexc:futures"

        ad_compra = self.venue_adapters.get(compra_venue)
        ad_venda = self.venue_adapters.get(venda_venue)
        if ad_compra is None or ad_venda is None:
            logger.warning(
                "Sem adaptador de mercado para %s ou %s -- decisao de %s adiada.",
                compra_venue, venda_venue, symbol,
            )
            return None

        compra_book, venda_book = await asyncio.gather(
            ad_compra.fetch_depth(symbol, self.depth_limit),
            ad_venda.fetch_depth(symbol, self.depth_limit),
            return_exceptions=True,
        )

        for nome, resultado in ((compra_venue, compra_book), (venda_venue, venda_book)):
            if isinstance(resultado, Exception):
                logger.warning(
                    "Falha ao buscar profundidade de %s em %s: %s. Decisao adiada.",
                    symbol, nome, resultado,
                )
                return None

        if compra_book is None or venda_book is None:
            return None
        if not compra_book.is_usable or not venda_book.is_usable:
            logger.warning(
                "Book de %s inutilizavel (%s ok=%s, %s ok=%s). Decisao adiada.",
                symbol, compra_venue, compra_book.is_usable, venda_venue, venda_book.is_usable,
            )
            return None

        spot_book, futures_book = compra_book, venda_book
        self._avisar_se_teto_menor_que_spread_do_book(symbol, spot_book, futures_book)
        return spot_book, futures_book

    def _avisar_se_teto_menor_que_spread_do_book(
        self, symbol: str, spot_book: OrderBook, futures_book: OrderBook,
    ):
        """
        Avisa quando o teto de slippage é apertado demais para a largura do
        próprio book.

        Um book com bid/ask de 0,57% de distância (medido no futures de
        JIMOTHY em 05/08/2026) e um teto de 0,30% significam que o preço-
        limite mal alcança a primeira camada do lado oposto. Basta a cotação
        do topo piscar entre o snapshot de profundidade e a ordem chegar para
        a IOC não achar nada e ser cancelada — foi o que produziu duas ordens
        com `errorCode 18` (IOC cancelada sem preenchimento) naquela saída.

        Não bloqueia nada: um teto apertado é uma escolha legítima (paga-se
        com menos preenchimento, não com preço ruim), e o escalonamento a
        mercado cobre o resíduo. Mas é uma condição que precisa ser visível,
        porque o sintoma — "o bot não fecha" — não sugere a causa sozinho.
        """
        for nome, book in (("spot", spot_book), ("futures", futures_book)):
            if not book.best_bid or not book.best_ask:
                continue
            largura_pct = (book.best_ask - book.best_bid) / book.best_bid * 100
            if largura_pct > self.slippage_policy.max_slippage_pct * 2:
                logger.warning(
                    "Book de %s (%s) tem spread interno de %.3f%%, mais que o dobro do teto de "
                    "slippage de %.3f%%. As ordens IOC podem não preencher e cair no escalonamento "
                    "a mercado. Considere aumentar MEXC_BOT_MAX_SLIPPAGE_PCT para este par.",
                    symbol, nome, largura_pct, self.slippage_policy.max_slippage_pct,
                )

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """
        O dashboard identifica pares pelo "display_symbol" (ex: "JIMOTHY",
        sem sufixo). O bot precisa usar exatamente essa mesma chave para
        casar com os preços recebidos via on_price_update — caso contrário,
        um par configurado como "JIMOTHYUSDT" nunca recebe atualização de
        preço nenhuma (o par fica preso em IDLE mesmo com o spread batendo).
        Aceita símbolos digitados com ou sem "USDT" e sempre normaliza para
        o formato sem sufixo, para nunca depender de quem está chamando
        acertar o formato certo.
        """
        symbol = symbol.strip().upper()
        if symbol.endswith("USDT") and len(symbol) > 4:
            return symbol[:-4]
        return symbol

    # ---------------- Pub/Sub para a interface ----------------

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _broadcast(self, event: dict):
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def _broadcast_snapshot(self):
        await self._broadcast({"type": "bot_snapshot", "pairs": self.get_snapshot()})

    # ---------------- Inicialização / configuração ----------------

    async def load_configs(self):
        saved = await self.storage.get_all_pair_configs()
        async with self._lock:
            for raw_symbol, cfg in saved.items():
                symbol = self._normalize_symbol(raw_symbol)
                self.configs[symbol] = PairConfig(symbol=symbol, **cfg)
                if symbol not in self.runtimes:
                    self.runtimes[symbol] = PairRuntime(symbol=symbol)
                if symbol != raw_symbol:
                    # Migração: config foi salva com o sufixo USDT (formato antigo,
                    # de antes da normalização) - regrava com a chave correta para
                    # que volte a casar com os preços recebidos do dashboard.
                    logger.warning(
                        "Migrando config do par '%s' para o formato normalizado '%s'.",
                        raw_symbol, symbol,
                    )
                    await self.storage.upsert_pair_config(
                        symbol, cfg["enabled"], cfg["entry_spread_pct"],
                        cfg["exit_spread_pct"], cfg["position_size_usdt"],
                    )
                    await self.storage.delete_pair_config(raw_symbol)

            saved_positions = await self.storage.get_all_positions()
            for raw_symbol, pos in saved_positions.items():
                symbol = self._normalize_symbol(raw_symbol)
                if symbol not in self.runtimes:
                    self.runtimes[symbol] = PairRuntime(symbol=symbol)
                rt = self.runtimes[symbol]
                rt.state = PairState(pos["state"])
                rt.entry_spread_pct = pos["entry_spread_pct"]
                rt.entry_spot_price = pos["entry_spot_price"]
                rt.entry_futures_price = pos["entry_futures_price"]
                rt.entry_spot_qty = pos["entry_spot_qty"]
                rt.entry_futures_vol = pos["entry_futures_vol"]
                rt.entry_notional_usdt = pos["entry_notional_usdt"]
                rt.entry_ts = pos["entry_ts"]

    async def set_pair_config(self, symbol: str, enabled: bool, entry_spread_pct: float,
                               exit_spread_pct: float, position_size_usdt: float,
                               buy_venue: str = "mexc:spot", sell_venue: str = "mexc:futures"):
        symbol = self._normalize_symbol(symbol)

        # --- Validacao do par de venues ---
        if buy_venue not in self.venue_clients or sell_venue not in self.venue_clients:
            raise ValueError(
                f"Venue desconhecido ({buy_venue} / {sell_venue}). "
                f"Conhecidos: {', '.join(sorted(self.venue_clients))}."
            )
        if buy_venue.endswith(":spot") and sell_venue.endswith(":spot"):
            raise ValueError(
                "Nao existe operacao neutra entre dois mercados spot: sem um instrumento "
                "vendido a descoberto dos dois lados, seria so comprar em dois lugares."
            )
        if buy_venue == sell_venue:
            raise ValueError("As duas pernas precisam ser venues diferentes.")

        # Cross-exchange fica bloqueado nesta versao: exige saldo
        # pre-posicionado nas duas exchanges e, quando uma perna falha, NAO da
        # para reverter na mesma conta -- que e exatamente o que
        # `_revert_futures_leg` faz hoje. Liberar isso antes de o caminho
        # simples estar validado repetiria o bug 15 num cenario sem volta.
        if not self.allow_cross_exchange and buy_venue.split(":")[0] != sell_venue.split(":")[0]:
            raise ValueError(
                "Operacao entre exchanges diferentes ainda nao esta habilitada. "
                "Use uma combinacao dentro da mesma exchange (ex: gate:spot + gate:futures)."
            )

        # A execução dos seis venues está implementada, mas só entra em uso
        # depois de VALIDADA contra a API real. Recusar aqui é o que impede o
        # par de ser configurado num venue cuja execução nunca foi conferida.
        nao_validados = [v for v in (buy_venue, sell_venue) if v not in self.validated_venues]
        if nao_validados:
            raise ValueError(
                f"A execução de {', '.join(nao_validados)} ainda não foi validada contra a API real — "
                f"hoje o bot só envia ordem para {', '.join(sorted(self.validated_venues))}. "
                "Rode `python -m bot.validate_venue <venue>` para conferir com uma ordem mínima e "
                "depois inclua o venue em BOT_VALIDATED_VENUES no .env."
            )

        # Sem cliente autenticado dos DOIS lados, a recusa acontece AQUI, na
        # configuracao -- nunca no meio da operacao com uma perna ja aberta.
        if self.execution_mode == ExecutionMode.LIVE:
            faltando = [v for v in (buy_venue, sell_venue) if self.venue_clients.get(v) is None]
            if faltando:
                raise ValueError(
                    f"Sem credenciais para {', '.join(faltando)}. "
                    "Preencha as chaves no .env antes de operar este par em modo LIVE."
                )

        if entry_spread_pct <= 0:
            raise ValueError(
                "entry_spread_pct deve ser positivo. A estratégia (compra Spot + "
                "vende Futures) só é lucrativa quando o spread futures-spot é "
                "positivo; a entrada nunca deve ser configurada para spread negativo."
            )
        if entry_spread_pct <= exit_spread_pct:
            raise ValueError(
                "entry_spread_pct deve ser maior que exit_spread_pct "
                "(o bot entra em spreads grandes e sai quando o spread diminui)."
            )
        if position_size_usdt <= 0:
            raise ValueError("position_size_usdt deve ser maior que zero.")

        # Tamanho menor que 1 contrato nunca vira ordem: o par ficaria
        # habilitado, falhando em toda tentativa com
        # `entry_failed_size_too_small` e indo para PAUSED_ERROR. Recusar aqui
        # transforma um par que "não funciona sem dizer por quê" numa mensagem
        # clara no momento em que dá para corrigir.
        spec = self.contract_specs.get(symbol)
        if spec is not None and spec.contract_size > 0:
            referencia = self._last_price_hint.get(symbol)
            if referencia:
                minimo = spec.contract_size * spec.min_vol * referencia
                if position_size_usdt < minimo:
                    raise ValueError(
                        f"position_size_usdt ({position_size_usdt:g} USDT) e menor que 1 contrato "
                        f"de {symbol}, que custa ~{minimo:.2f} USDT "
                        f"(contractSize={spec.contract_size:g} x {referencia:.8g}). "
                        f"Use pelo menos {minimo:.2f} USDT neste par."
                    )

        async with self._lock:
            self.configs[symbol] = PairConfig(
                symbol=symbol, enabled=enabled, entry_spread_pct=entry_spread_pct,
                exit_spread_pct=exit_spread_pct, position_size_usdt=position_size_usdt,
                buy_venue=buy_venue, sell_venue=sell_venue,
            )
            if symbol not in self.runtimes:
                self.runtimes[symbol] = PairRuntime(symbol=symbol)

        await self.storage.upsert_pair_config(
            symbol, enabled, entry_spread_pct, exit_spread_pct, position_size_usdt,
            buy_venue, sell_venue,
        )
        await self._broadcast_snapshot()

    async def remove_pair_config(self, symbol: str):
        symbol = self._normalize_symbol(symbol)
        async with self._lock:
            self.configs.pop(symbol, None)
            self.runtimes.pop(symbol, None)
            self.contract_specs.pop(symbol, None)
        await self.storage.delete_pair_config(symbol)
        await self.storage.clear_position(symbol)
        await self._broadcast_snapshot()

    # ---------------- Reconexão / degradação ----------------

    def set_connection_degraded(self, degraded: bool):
        """
        Chamado pelo orquestrador quando a conexão com a MEXC cai/volta.
        Conforme especificado: ao degradar, pausa NOVAS entradas, mas não
        mexe em posições já abertas (elas continuam sendo monitoradas e
        podem sair normalmente quando o spread de saída for atingido).
        """
        was_degraded = self.connection_degraded
        self.connection_degraded = degraded
        if degraded and not was_degraded:
            logger.warning("Conexão degradada: novas entradas do bot pausadas até normalizar.")
        elif not degraded and was_degraded:
            logger.info("Conexão normalizada: novas entradas do bot liberadas novamente.")

    def get_current_total_exposure_usdt(self) -> float:
        """Soma o notional de entrada de todas as posições atualmente abertas (OPEN/ENTERING/EXITING)."""
        total = 0.0
        for rt in self.runtimes.values():
            if rt.state in (PairState.OPEN, PairState.ENTERING, PairState.EXITING) and rt.entry_notional_usdt:
                total += rt.entry_notional_usdt
        return total

    # ---------------- Núcleo: reação a atualização de preço ----------------

    async def on_price_update(
        self, symbol: str, spot_price: float, futures_price: float, spread_pct: float,
        prices_from_book: bool = True,
        exit_spread_pct: float | None = None,
        spot_bid: float | None = None, futures_ask: float | None = None,
        spot_book_age_s: float | None = None, futures_book_age_s: float | None = None,
        funding_rate: float = 0.0,
    ):
        """
        Chamado a cada atualização de preço de um par (mesmo dado que já
        alimenta o dashboard). Decide se deve entrar, sair, ou não fazer nada.

        ## Papel do spread de topo de book aqui: GATILHO, não decisão

        Os `spread_pct` / `exit_spread_pct` que chegam por aqui vêm do TOPO do
        book (bid1/ask1). Eles são baratos (chegam de graça pelo WebSocket, a
        cada tick) mas otimistas: descrevem o preço da primeira camada, não o
        preço médio de executar o tamanho da posição.

        Por isso este método NÃO decide mais a operação — ele só decide se
        vale a pena PERGUNTAR. Quando o spread de topo cruza o limiar, o bot
        busca a profundidade real dos dois books e recalcula o spread para o
        tamanho exato da posição (`_confirm_entry` / `_confirm_exit`). Só esse
        segundo número, que é o executável, autoriza a ordem.

        A confirmação custa duas chamadas REST (~100ms em paralelo). É um
        preço barato: no caso real que motivou esta mudança, a diferença
        entre o topo do book e o executável foi de 1,21 ponto percentual numa
        única saída — mais do que o lucro-alvo da operação inteira.

        `prices_from_book`: indica se AMBOS os preços vieram do book. Quando
        False, ao menos um lado está usando "último negociado", que em pares
        ilíquidos pode estar muito longe do executável. Nesse caso o bot não
        abre posição nova (mas continua podendo sair, que é sempre a ação
        mais segura).

        `spot_book_age_s` / `futures_book_age_s`: há quanto tempo cada lado
        do book foi atualizado. Ver `_book_too_old`.
        """
        symbol = self._normalize_symbol(symbol)
        cfg = self.configs.get(symbol)
        if cfg is None or not cfg.enabled:
            return

        rt = self.runtimes.get(symbol)
        if rt is None:
            rt = PairRuntime(symbol=symbol)
            self.runtimes[symbol] = rt

        if futures_price:
            self._last_price_hint[symbol] = futures_price

        if rt.state in (PairState.PAUSED_ERROR, PairState.MANUAL_HALT):
            return  # nunca age nesses estados

        # Uma decisão por par por vez. Ver comentário de `self._busy`: sem
        # isso, duas fontes de preço podem disparar a mesma operação em
        # paralelo, porque a mudança de estado só acontece depois de vários
        # `await`. A marcação é síncrona (nenhum await entre o teste e o
        # `add`), que é o que a torna eficaz num loop de eventos.
        if symbol in self._busy:
            return

        if rt.state == PairState.IDLE:
            if self.connection_degraded:
                return  # não abre posição nova com conexão degradada

            if not prices_from_book:
                # Preço não-executável (último negociado em vez de book).
                # Logar em nível INFO seria ruidoso demais (acontece a cada
                # update de um par ilíquido), então só registra quando o
                # spread teria sido suficiente para entrar - que é quando a
                # trava realmente evitou uma operação ruim.
                if spread_pct >= cfg.entry_spread_pct:
                    logger.warning(
                        "Entrada em %s BLOQUEADA: spread de %.2f%% atingiria o nível de entrada, mas os "
                        "preços não vieram do book (usando último negociado). Spread pode ser fictício - "
                        "aguardando preço executável.",
                        symbol, spread_pct,
                    )
                return

            # A estratégia é: compra Spot + vende Futures. Isso só trava lucro
            # na convergência quando Futures > Spot (spread positivo). Com
            # spread negativo, esta mesma operação trava PREJUÍZO garantido
            # na convergência (compraria caro no spot, venderia barato no
            # futures) - por isso a entrada exige spread positivo, não |spread|.
            if spread_pct < cfg.entry_spread_pct:
                return

            if self._book_too_old(symbol, "entrada", spot_book_age_s, futures_book_age_s):
                return

            if self.max_total_exposure_usdt is not None:
                current_exposure = self.get_current_total_exposure_usdt()
                if current_exposure + cfg.position_size_usdt > self.max_total_exposure_usdt:
                    logger.info(
                        "Entrada em %s bloqueada: exposição atual (%.2f USDT) + nova posição "
                        "(%.2f USDT) excederia o teto global de %.2f USDT.",
                        symbol, current_exposure, cfg.position_size_usdt, self.max_total_exposure_usdt,
                    )
                    return

            if not self._depth_throttle_ok(symbol):
                return

            self._busy.add(symbol)
            try:
                await self._confirm_and_enter(cfg, rt, spread_pct, funding_rate)
            finally:
                self._busy.discard(symbol)

        elif rt.state == PairState.OPEN:
            # A saída executa nos lados OPOSTOS do book em relação à entrada
            # (vende Spot no bid, recompra Futures no ask), então SÓ pode ser
            # decidida com o spread de saída e os preços do lado correto.
            #
            # Sem esses dados (book indisponível), o bot NÃO sai. O fallback
            # anterior - usar o spread de entrada - causava saídas em
            # condições muito piores do que o bot enxergava: um caso real
            # registrou "tela 0,11%" (entrada) quando o spread de saída real
            # era 2,05%, gerando prejuízo.
            #
            # Ficar na posição esperando dados confiáveis é mais seguro do
            # que sair às cegas com a régua errada.
            if exit_spread_pct is None or spot_bid is None or futures_ask is None:
                logger.debug(
                    "Saída de %s adiada: dados de saída incompletos "
                    "(exit_spread=%s, spot_bid=%s, futures_ask=%s).",
                    symbol, exit_spread_pct, spot_bid, futures_ask,
                )
                return

            if not prices_from_book:
                # Mesma exigência da entrada: preço fora do book é
                # "último negociado", que não representa o executável.
                if exit_spread_pct <= cfg.exit_spread_pct:
                    logger.warning(
                        "Saída de %s BLOQUEADA: spread de saída de %.2f%% atingiria o nível, mas os "
                        "preços não vieram do book. Aguardando preço executável.",
                        symbol, exit_spread_pct,
                    )
                return

            if exit_spread_pct > cfg.exit_spread_pct:
                return

            if self._book_too_old(symbol, "saída", spot_book_age_s, futures_book_age_s):
                return

            if not self._depth_throttle_ok(symbol):
                return

            self._busy.add(symbol)
            try:
                await self._confirm_and_exit(cfg, rt, exit_spread_pct)
            finally:
                self._busy.discard(symbol)

    def _venues_executaveis(self, cfg: PairConfig) -> bool:
        """
        Recusa o par se o venue configurado não tiver execução VALIDADA.

        Segunda linha de defesa: `set_pair_config` já recusa na configuração,
        mas uma config pode entrar por outro caminho (linha antiga no banco,
        edição manual do SQLite, migração). Esta checagem roda a cada decisão,
        antes de qualquer ordem.

        Vale em SIMULAÇÃO também, de propósito: uma simulação que finge operar
        um venue não validado valida uma estratégia que não pode rodar, e dá
        confiança falsa para ligar o modo real.
        """
        fora = [v for v in (cfg.buy_venue, cfg.sell_venue) if v not in self.validated_venues]
        if not fora:
            return True
        agora = time.time()
        # Log limitado: isto seria disparado a cada tick de preço.
        if agora - self._venue_warn_ts.get(cfg.symbol, 0) > 300:
            self._venue_warn_ts[cfg.symbol] = agora
            logger.error(
                "Par %s configurado em %s -> %s, mas a execução de %s NÃO foi validada contra a "
                "API real (validados: %s). O bot NÃO vai operar este par.",
                cfg.symbol, cfg.buy_venue, cfg.sell_venue,
                ", ".join(fora), ", ".join(sorted(self.validated_venues)),
            )
        return False

    def _book_too_old(
        self, symbol: str, action: str,
        spot_age_s: float | None, futures_age_s: float | None,
    ) -> bool:
        """
        Recusa a decisão quando qualquer um dos dois lados do book está
        velho demais.

        A idade é medida por LADO, não do par inteiro, porque o problema real
        é a assimetria: spot atualizando a cada tick pelo WebSocket enquanto
        o bid/ask do futures está congelado há dois minutos (o canal
        individual da MEXC só empurra quando há negócios naquele contrato).
        O spread calculado nesse estado mistura um lado de agora com um lado
        de antes — parece uma oportunidade e é só o relógio.

        Idade desconhecida (None) é tratada como aceitável, para não travar o
        bot em caminhos que ainda não propagam o carimbo de tempo; a
        instrumentação de idade vive em engine.py.
        """
        ages = {"spot": spot_age_s, "futures": futures_age_s}
        for side, age in ages.items():
            if age is not None and age > self.max_book_age_s:
                logger.warning(
                    "%s de %s BLOQUEADA: book de %s está desatualizado há %.1fs "
                    "(limite %.1fs). O spread visto agora mistura um lado atual com um lado velho.",
                    action.capitalize(), symbol, side, age, self.max_book_age_s,
                )
                return True
        return False

    # ---------------- Confirmação por profundidade real ----------------

    async def _confirm_and_enter(
        self, cfg: PairConfig, rt: PairRuntime, screen_spread_pct: float, funding_rate: float,
    ):
        """
        Segundo portão da entrada: recalcula o spread com a profundidade real
        para o tamanho exato da posição e verifica se ainda sobra dinheiro
        depois das taxas e do funding.

        Três motivos independentes de recusa, todos logados com os números
        que levaram à decisão:

        1. o book não comporta o tamanho (profundidade insuficiente);
        2. o spread executável não atinge o limiar configurado, ainda que o
           spread de topo tenha atingido;
        3. o resultado líquido esperado não paga as taxas.

        Recusar é a ação correta em todos os três — a alternativa era o que
        vinha acontecendo: descobrir o número verdadeiro só no preço de fill.
        """
        symbol = cfg.symbol
        if not self._venues_executaveis(cfg):
            return

        spec = self.contract_specs.get(symbol)
        if spec is None:
            logger.warning(
                "Entrada em %s adiada: metadados do contrato Futures ainda não carregados. "
                "Sem contractSize não há como dimensionar a perna nem ler o book de contratos.",
                symbol,
            )
            return

        # Metadados de execução dos venues configurados. Carregar ANTES de
        # decidir é o que impede descobrir no meio da operação que falta o
        # tick de preço de um dos lados — com a outra perna já aberta.
        if not await self.ensure_venue_specs(cfg):
            logger.warning(
                "Entrada em %s adiada: metadados de execução de %s / %s ainda não disponíveis.",
                symbol, cfg.buy_venue, cfg.sell_venue,
            )
            return

        books = await self.fetch_books(symbol, cfg)
        if books is None:
            return
        spot_book, futures_book = books

        # Dimensiona pela melhor oferta do futures só para descobrir quantos
        # contratos cabem no tamanho configurado; o preço real da operação sai
        # do VWAP logo abaixo.
        vol = usdt_to_futures_vol(cfg.position_size_usdt, futures_book.best_bid, spec)
        if vol == 0:
            rt.state = PairState.PAUSED_ERROR
            rt.last_error = f"position_size_usdt ({cfg.position_size_usdt}) é pequeno demais para 1 contrato deste par."
            await self.storage.log_event(symbol, "entry_failed_size_too_small", {
                "position_size_usdt": cfg.position_size_usdt, "futures_price": futures_book.best_bid,
            }, simulated=self.execution_mode == ExecutionMode.SIMULATION)
            await self._broadcast_snapshot()
            return

        notional = futures_vol_to_usdt(vol, futures_book.best_bid, spec)
        executable = entry_executable_spread(spot_book, futures_book, notional, vol)

        if executable is None:
            return

        if not executable.complete:
            self.depth_rejections[symbol] = self.depth_rejections.get(symbol, 0) + 1
            logger.warning(
                "Entrada em %s RECUSADA: o book não comporta o tamanho. "
                "Spot cobriu %.1f%% do valor pedido, Futures cobriu %.0f de %.0f contratos. "
                "Executar só o que cabe deixaria as pernas descasadas.",
                symbol,
                executable.spot.notional / notional * 100 if notional > 0 else 0,
                executable.futures.filled_qty, vol,
            )
            return

        # --- Porta de plausibilidade ---
        # Antes de olhar economia, perguntar se os dois lados são o mesmo
        # ativo. Um spread de dois dígitos não é oportunidade: é sintoma.
        if abs(executable.spread_pct) > self.max_plausible_entry_pct:
            self.depth_rejections[symbol] = self.depth_rejections.get(symbol, 0) + 1
            logger.warning(
                "Entrada em %s RECUSADA por implausibilidade: spread executável de %.2f%% "
                "passa do limite de %.2f%%. Spread de dois dígitos quase sempre significa que "
                "o spot e o futures NÃO são o mesmo ativo (redenominação, token v2, "
                "pré-mercado) — operar isso compraria um ativo e venderia outro, sem hedge.",
                symbol, executable.spread_pct, self.max_plausible_entry_pct,
            )
            await self.storage.log_event(symbol, "entry_rejected_implausible", {
                "executable_spread_pct": executable.spread_pct,
                "limit_pct": self.max_plausible_entry_pct,
                "screen_spread_pct": screen_spread_pct,
            }, simulated=self.execution_mode == ExecutionMode.SIMULATION)
            return

        fees = self.fees_for(symbol)
        econ = evaluate_entry(
            entry_spread_pct=executable.spread_pct,
            target_exit_spread_pct=cfg.exit_spread_pct,
            fees=fees,
            funding_rate=funding_rate,
            expected_hold_hours=self.expected_hold_hours,
            min_net_pct=self.min_net_pct,
        )

        if executable.spread_pct < cfg.entry_spread_pct:
            self.depth_rejections[symbol] = self.depth_rejections.get(symbol, 0) + 1
            logger.info(
                "Entrada em %s RECUSADA na confirmação de profundidade: topo do book prometia %.3f%%, "
                "mas o spread EXECUTÁVEL para %.2f USDT é %.3f%% (limiar %.3f%%). "
                "Diferença de %.3f pp — este é exatamente o slippage que antes só aparecia no fill.",
                symbol, screen_spread_pct, notional, executable.spread_pct,
                cfg.entry_spread_pct, executable.depth_cost_pct,
            )
            return

        if not econ.viable:
            self.depth_rejections[symbol] = self.depth_rejections.get(symbol, 0) + 1
            logger.info(
                "Entrada em %s RECUSADA por economia: spread executável %.3f%% menos alvo de saída "
                "%.3f%% menos taxas %.3f%% menos funding %.3f%% = %.3f%% líquido (mínimo exigido %.3f%%). "
                "A operação não pagaria o próprio custo.",
                symbol, executable.spread_pct, cfg.exit_spread_pct, econ.fee_cost_pct,
                econ.funding_cost_pct, econ.net_pct, self.min_net_pct,
            )
            return

        logger.info(
            "Entrada em %s CONFIRMADA: executável %.3f%% (topo do book dizia %.3f%%, diferença %.3f pp), "
            "líquido após custos %.3f%%. Enviando ordens com teto de slippage de %.2f%%.",
            symbol, executable.spread_pct, screen_spread_pct, executable.depth_cost_pct,
            econ.net_pct, self.slippage_policy.max_slippage_pct,
        )

        await self._execute_entry(cfg, rt, executable, vol, spec, screen_spread_pct, econ)

    async def _confirm_and_exit(self, cfg: PairConfig, rt: PairRuntime, screen_exit_spread_pct: float):
        """
        Segundo portão da saída. Mesma ideia da entrada, com uma diferença
        importante de postura: aqui já existe posição aberta.

        Se a profundidade não confirmar o spread de saída, o bot NÃO sai —
        continua posicionado esperando uma janela de verdade. Isso mantém a
        regra que já existia (não sair com a régua errada), agora com uma
        régua que também enxerga o tamanho da posição, não só o topo do book.

        Uma saída recusada aqui não é uma saída perdida: é uma saída que
        teria acontecido 1,2 pp pior do que a tela indicava, que foi
        precisamente o que aconteceu no caso real de 03/08.
        """
        symbol = cfg.symbol
        if not self._venues_executaveis(cfg):
            return

        spec = self.contract_specs.get(symbol)
        if spec is None or not rt.entry_futures_vol or not rt.entry_spot_qty:
            logger.warning(
                "Saída de %s adiada: dados da posição incompletos "
                "(spec=%s, futures_vol=%s, spot_qty=%s).",
                symbol, spec is not None, rt.entry_futures_vol, rt.entry_spot_qty,
            )
            return

        books = await self.fetch_books(symbol, cfg)
        if books is None:
            return
        spot_book, futures_book = books

        executable = exit_executable_spread(
            spot_book, futures_book, rt.entry_spot_qty, rt.entry_futures_vol,
        )
        if executable is None:
            return

        if not executable.complete:
            self.depth_rejections[symbol] = self.depth_rejections.get(symbol, 0) + 1
            logger.warning(
                "Saída de %s adiada: o book não comporta a posição inteira "
                "(spot %.4g de %.4g, futures %.0f de %.0f contratos). Continua posicionado.",
                symbol, executable.spot.filled_qty, rt.entry_spot_qty,
                executable.futures.filled_qty, rt.entry_futures_vol,
            )
            return

        if executable.spread_pct > cfg.exit_spread_pct:
            self.depth_rejections[symbol] = self.depth_rejections.get(symbol, 0) + 1
            logger.info(
                "Saída de %s RECUSADA na confirmação de profundidade: topo do book dizia %.3f%%, "
                "mas o spread de saída EXECUTÁVEL é %.3f%% (alvo %.3f%%). Diferença de %.3f pp. "
                "Sair agora custaria essa diferença — continua posicionado.",
                symbol, screen_exit_spread_pct, executable.spread_pct,
                cfg.exit_spread_pct, executable.depth_cost_pct,
            )
            return

        fees = self.fees_for(symbol)
        projected = realized_pnl_pct(rt.entry_spread_pct or 0.0, executable.spread_pct, fees)
        logger.info(
            "Saída de %s CONFIRMADA: executável %.3f%% (topo do book dizia %.3f%%, diferença %.3f pp). "
            "Resultado projetado da operação: %.3f%% líquido de taxas.",
            symbol, executable.spread_pct, screen_exit_spread_pct,
            executable.depth_cost_pct, projected,
        )

        await self._execute_exit(cfg, rt, executable, spec, screen_exit_spread_pct)

    # ---------------- Entrada ----------------

    async def _execute_entry(
        self, cfg: PairConfig, rt: PairRuntime, executable: ExecutableSpread,
        vol: float, spec: ContractSpec, screen_spread_pct: float, econ,
    ):
        rt.state = PairState.ENTERING
        await self._broadcast_snapshot()

        if self.execution_mode == ExecutionMode.SIMULATION:
            await self._execute_entry_simulated(cfg, rt, executable, vol, spec, screen_spread_pct, econ)
        else:
            await self._execute_entry_live(cfg, rt, executable, vol, spec, screen_spread_pct, econ)

    def _target_spot_qty(self, futures_vol: float, spec: ContractSpec, symbol: str) -> float:
        """
        Quantidade de ativo BASE que a perna Spot precisa comprar para casar
        com `futures_vol` contratos vendidos.

        ## Por que casar por QUANTIDADE e não por valor (mudança de conceito)

        Até aqui as duas pernas eram casadas pelo mesmo valor em USDT. Isso
        parece equivalente, mas não é: os dois mercados executam a preços
        DIFERENTES (a diferença entre eles é o spread, que é a razão de a
        operação existir). Casar valor com preços diferentes produz
        quantidades diferentes.

        No caso real de 03/08: 600 JIMOTHY vendidos no futures contra 608,48
        comprados no spot — 8,48 unidades de exposição comprada não coberta,
        exatamente 1,4% da posição, que é o spread de entrada. Numa
        estratégia cuja premissa inteira é ser neutra a direção, isso é uma
        aposta direcional embutida sem ninguém ter decidido fazê-la.

        Ela custou dinheiro naquela operação: o preço caiu ~3,7% durante os
        25 minutos de posição, e as 8,48 unidades descobertas transformaram
        um resultado de +0,0011 USDT em -0,0010 USDT — inverteram o sinal.

        Casando por quantidade, o resultado passa a ser exatamente:

            lucro = Q * [(F_entrada - S_entrada) - (F_saída - S_saída)]

        ou seja, só spread de entrada menos spread de saída, sem nenhum termo
        direcional. É a identidade que a estratégia promete.

        ## O ajuste da taxa

        A MEXC cobra a taxa de uma COMPRA no Spot na própria moeda comprada:
        comprar 600 JIMOTHY deixa ~599,7 na carteira. Como é essa quantidade
        líquida que ficará disponível para vender na saída, a compra é
        inflada pela taxa para que o SALDO RESULTANTE case com o short — e
        não a quantidade bruta pedida.
        """
        target = futures_vol * spec.contract_size
        fee_fraction = self.fees_for(symbol).spot_taker_pct / 100
        if 0 <= fee_fraction < 0.5:
            target = target / (1 - fee_fraction)
        return target

    async def _execute_entry_simulated(
        self, cfg: PairConfig, rt: PairRuntime, executable: ExecutableSpread,
        vol: float, spec: ContractSpec, screen_spread_pct: float, econ,
    ):
        """
        Simulação com os preços EXECUTÁVEIS (VWAP da profundidade real), não
        com o topo do book.

        Isso muda o valor da simulação: antes ela assumia fill no melhor
        preço do book, ou seja, simulava a versão otimista que a produção
        nunca conseguiu entregar. Agora o número simulado é o mesmo número
        que a execução real persegue, e divergências entre os dois passam a
        significar de fato "o mercado se moveu", não "a simulação era
        otimista por construção".
        """
        spot_price = executable.spot.avg_price
        futures_price = executable.futures.avg_price
        spot_qty = self._target_spot_qty(vol, spec, cfg.symbol)
        real_notional = futures_vol_to_usdt(vol, futures_price, spec)

        rt.entry_spread_pct = executable.spread_pct
        rt.entry_spot_price = spot_price
        rt.entry_futures_price = futures_price
        rt.entry_spot_qty = spot_qty
        rt.entry_futures_vol = vol
        rt.entry_notional_usdt = real_notional
        rt.entry_ts = time.time()
        rt.state = PairState.OPEN
        rt.last_error = None

        await self.storage.upsert_position(
            cfg.symbol, PairState.OPEN.value, simulated=True,
            entry_spread_pct=executable.spread_pct, entry_spot_price=spot_price,
            entry_futures_price=futures_price, entry_spot_qty=spot_qty,
            entry_futures_vol=vol, entry_notional_usdt=real_notional, entry_ts=rt.entry_ts,
        )
        await self.storage.log_event(cfg.symbol, "entry_simulated", {
            "spread_pct": executable.spread_pct,
            "spread_signal_pct": screen_spread_pct,
            "spread_book_top_pct": executable.screen_spread_pct,
            "depth_cost_pct": executable.depth_cost_pct,
            "net_expected_pct": econ.net_pct,
            "fee_cost_pct": econ.fee_cost_pct,
            "spot_price": spot_price, "futures_price": futures_price,
            "spot_qty": spot_qty, "futures_vol": vol, "notional_usdt": real_notional,
        }, simulated=True)

        logger.info(
            "[SIMULAÇÃO] Entrada em %s: executável=%.3f%% (topo do book=%.3f%%) "
            "spot=%.8g futures=%.8g notional=%.2f USDT",
            cfg.symbol, executable.spread_pct, executable.screen_spread_pct,
            spot_price, futures_price, real_notional,
        )
        await self._broadcast_snapshot()

    async def _execute_entry_live(
        self, cfg: PairConfig, rt: PairRuntime, executable: ExecutableSpread,
        vol: float, spec: ContractSpec, screen_spread_pct: float, econ,
    ):
        """
        Execução real, em duas pernas com teto de slippage:

        1. Perna ÂNCORA (Futures, abrir short): ordem IOC com PISO de preço
           ancorado no VWAP confirmado. Define a quantidade real executada.
        2. Perna ESPELHO (Spot, comprar): ordem IOC com TETO de preço,
           dimensionada para casar a QUANTIDADE da âncora (ver
           `_target_spot_qty`), não o valor em USDT.
        3. Qualquer descasamento remanescente — perna espelho que não
           preencheu, ou preencheu menos — é corrigido revertendo a parte
           correspondente da âncora IMEDIATAMENTE, para nunca ficar com
           exposição direcional não intencional.

        O passo 3 agora corrige descasamento PARCIAL também. Antes, ou a
        perna spot preenchia (e assumia-se casamento perfeito) ou não
        preenchia nada (e revertia-se tudo); um preenchimento parcial passava
        direto, deixando a diferença como exposição silenciosa.
        """
        buy_trader, sell_trader = self._traders_for_pair(cfg)
        if buy_trader is None or sell_trader is None:
            # Recusar aqui é o último portão antes da ordem. Chegar até este
            # ponto sem executor significa que faltou credencial ou metadado
            # do venue — e abrir só a perna que dá é o pior desfecho possível.
            rt.state = PairState.PAUSED_ERROR
            rt.last_error = (
                f"Sem executor para {cfg.buy_venue} e/ou {cfg.sell_venue} "
                "(credencial ou metadados do símbolo ausentes). Nenhuma ordem foi enviada."
            )
            logger.error("[LIVE] Entrada em %s abortada: %s", cfg.symbol, rt.last_error)
            await self._broadcast_snapshot()
            return

        futures_symbol = sell_trader.spec.native_symbol
        spot_symbol = buy_trader.spec.native_symbol

        # --- Perna âncora: Futures IOC com piso de preço ---
        #
        # A quantidade que atravessa `execute_bounded` é na MOEDA BASE, não em
        # contratos: é a única unidade que as três exchanges têm em comum
        # (a BingX pede moeda base, MEXC e Gate pedem contratos) e é a unidade
        # em que as duas pernas precisam casar (bug 11). A conversão para
        # contratos acontece dentro do executor do venue.
        anchor_base_qty = vol * spec.contract_size
        futures_floor = limit_price_for_sell(
            executable.futures.avg_price, self.slippage_policy, sell_trader.spec.price_tick,
        )
        futures_leg = await execute_bounded(
            label=f"{cfg.symbol} futures/abrir-short",
            total_qty=anchor_base_qty,
            limit_price=futures_floor,
            send_ioc=lambda q, p: sell_trader.run_leg("open_sell_leg", q, p),
            send_market=lambda q: sell_trader.run_leg(
                "open_sell_leg", q, None, ref_price=executable.futures.avg_price,
            ),
            policy=self.slippage_policy,
            round_qty=sell_trader.round_qty,
        )

        if futures_leg.filled_qty <= 0:
            rt.state = PairState.PAUSED_ERROR
            rt.last_error = (
                "Perna Futures não preencheu nada dentro do teto de slippage "
                f"({self.slippage_policy.max_slippage_pct:.2f}%). Nenhuma posição foi aberta. "
                f"Erros: {'; '.join(futures_leg.errors) or 'nenhum (book fugiu do teto)'}"
            )
            await self.storage.log_event(cfg.symbol, "entry_futures_not_filled", {
                "limit_price": futures_floor, "requested_vol": vol,
                "errors": futures_leg.errors,
            }, simulated=False)
            logger.warning("[LIVE] Entrada em %s abortada sem exposição: %s", cfg.symbol, rt.last_error)
            await self._broadcast_snapshot()
            return

        # De volta para contratos: é a unidade em que a posição é registrada,
        # reportada e fechada. A moeda base é a unidade de TRANSPORTE entre
        # venues; os contratos são a unidade de CONTABILIDADE do futures.
        filled_vol = futures_leg.filled_qty / spec.contract_size
        futures_fill_price = futures_leg.avg_price

        # --- Perna espelho: Spot IOC com teto de preço, casando QUANTIDADE ---
        target_spot_qty = self._target_spot_qty(filled_vol, spec, cfg.symbol)
        spot_cap = limit_price_for_buy(
            executable.spot.avg_price, self.slippage_policy, buy_trader.spec.price_tick,
        )

        spot_leg = await execute_bounded(
            label=f"{cfg.symbol} spot/comprar",
            total_qty=target_spot_qty,
            limit_price=spot_cap,
            send_ioc=lambda q, p: buy_trader.run_leg("open_buy_leg", q, p),
            # `ref_price` é obrigatório em venues que cobram a compra a mercado
            # em USDT (Gate). Passar sempre custa nada e evita que a diferença
            # entre exchanges vire uma condição só descoberta em produção.
            send_market=lambda q: buy_trader.run_leg(
                "open_buy_leg", q, None, ref_price=executable.spot.avg_price,
            ),
            policy=self.slippage_policy,
            round_qty=buy_trader.round_qty,
        )

        if spot_leg.filled_qty <= 0:
            logger.error(
                "[LIVE] Perna Spot em %s não preencheu nada. Revertendo a âncora de Futures IMEDIATAMENTE.",
                cfg.symbol,
            )
            await self._revert_futures_leg(
                cfg.symbol, sell_trader, filled_vol, spec, reason="spot_nao_preencheu",
                ref_price=executable.futures.avg_price,
            )
            rt.state = PairState.PAUSED_ERROR
            rt.last_error = (
                "Perna Spot não preencheu dentro do teto de slippage. Perna Futures foi revertida "
                f"automaticamente. VERIFIQUE MANUALMENTE sua conta em {cfg.sell_venue} para confirmar "
                f"que não sobrou posição. Erros: {'; '.join(spot_leg.errors) or 'nenhum (book fugiu do teto)'}"
            )
            await self.storage.log_event(cfg.symbol, "entry_failed_spot_order_reverted", {
                "errors": spot_leg.errors, "futures_vol_reverted": filled_vol,
                "spot_limit_price": spot_cap, "target_spot_qty": target_spot_qty,
            }, simulated=False)
            await self._broadcast_snapshot()
            return

        # --- Correção de descasamento parcial ---
        # Quanto da exposição vendida no futures NÃO está coberta pela
        # quantidade de spot que efetivamente ficou na carteira.
        spot_net_qty = spot_leg.filled_qty * (1 - self.fees_for(cfg.symbol).spot_taker_pct / 100)
        uncovered_base = filled_vol * spec.contract_size - spot_net_qty

        # Só reverte quando o descoberto passa de UM CONTRATO INTEIRO.
        #
        # Abaixo disso o resíduo é apenas o arredondamento de precisão da
        # quantidade de spot (dezenas de unidades numa posição de dezenas de
        # milhares, tipicamente ~0,01% do tamanho) e não há como corrigi-lo:
        # contratos são indivisíveis. Reverter um contrato inteiro para
        # "consertar" 0,01% de descasamento pagaria duas taxas de taker e
        # criaria um descasamento MAIOR na direção oposta.
        #
        # O que este bloco corrige de verdade é o caso grave: a perna Spot
        # preencheu só uma fração do pedido e a diferença é exposição
        # direcional real. Antes, esse caso passava direto.
        if uncovered_base >= spec.contract_size:
            excess_vol = self._round_futures_vol(uncovered_base / spec.contract_size, spec)
            if excess_vol > 0:
                logger.warning(
                    "[LIVE] Descasamento em %s: %.0f contratos vendidos (%.4g unidades) contra %.4g "
                    "unidades líquidas no Spot. Revertendo %.0f contratos para restaurar a neutralidade.",
                    cfg.symbol, filled_vol, filled_vol * spec.contract_size,
                    spot_net_qty, excess_vol,
                )
                await self._revert_futures_leg(
                    cfg.symbol, sell_trader, excess_vol, spec, reason="descasamento_parcial",
                    ref_price=executable.futures.avg_price,
                )
                filled_vol -= excess_vol

        spot_fill_price = spot_leg.avg_price
        real_notional = futures_vol_to_usdt(filled_vol, futures_fill_price, spec)

        # Spread REALIZADO da entrada: calculado a partir dos preços de fill
        # reais (o que efetivamente foi pago no spot e recebido no futures),
        # não do "spread de tela" que disparou a decisão. É esse o spread que
        # realmente foi travado na operação.
        entry_spread_realized = (
            (futures_fill_price - spot_fill_price) / spot_fill_price * 100
            if spot_fill_price > 0 else 0.0
        )

        rt.entry_spread_pct = entry_spread_realized
        rt.entry_spot_price = spot_fill_price
        rt.entry_futures_price = futures_fill_price
        rt.entry_spot_qty = spot_leg.filled_qty
        rt.entry_futures_vol = filled_vol
        rt.entry_notional_usdt = real_notional
        rt.entry_ts = time.time()
        rt.state = PairState.OPEN
        rt.last_error = None

        await self.storage.upsert_position(
            cfg.symbol, PairState.OPEN.value, simulated=False,
            entry_spread_pct=entry_spread_realized, entry_spot_price=spot_fill_price,
            entry_futures_price=futures_fill_price, entry_spot_qty=spot_leg.filled_qty,
            entry_futures_vol=filled_vol, entry_notional_usdt=real_notional, entry_ts=rt.entry_ts,
        )

        await self.storage.log_event(cfg.symbol, "entry_live", {
            # Spread realizado (dos fills) - é o que a interface mostra
            "spread_pct": entry_spread_realized,
            # Spread que estava na tela (topo do book) no momento do gatilho
            "spread_signal_pct": screen_spread_pct,
            # Spread que a confirmação de profundidade projetou para este tamanho
            "spread_executable_pct": executable.spread_pct,
            # Quanto o topo do book estava otimista em relação ao executável
            "depth_cost_pct": executable.depth_cost_pct,
            # Quanto se perdeu entre o executável projetado e o fill de fato
            "execution_slippage_pct": executable.spread_pct - entry_spread_realized,
            "net_expected_pct": econ.net_pct,
            "fee_cost_pct": econ.fee_cost_pct,
            "spot_price": spot_fill_price, "futures_price": futures_fill_price,
            "spot_fill_price": spot_fill_price, "futures_fill_price": futures_fill_price,
            "spot_limit_price": spot_cap, "futures_limit_price": futures_floor,
            "spot_attempts": spot_leg.attempts, "futures_attempts": futures_leg.attempts,
            "spot_escalated_to_market": spot_leg.escalated,
            "futures_escalated_to_market": futures_leg.escalated,
            "spot_qty": spot_leg.filled_qty, "futures_vol": filled_vol,
            "notional_usdt": real_notional,
        }, simulated=False)

        logger.info(
            "[LIVE] Entrada REAL em %s: realizado=%.3f%% | executável projetado=%.3f%% | "
            "topo do book=%.3f%% | slippage de execução=%.3f pp | spot=%.8g futures=%.8g | notional=%.2f USDT",
            cfg.symbol, entry_spread_realized, executable.spread_pct, screen_spread_pct,
            executable.spread_pct - entry_spread_realized,
            spot_fill_price, futures_fill_price, real_notional,
        )
        await self._broadcast_snapshot()

    # ---------------- Primitivas de envio de ordem ----------------
    #
    # O envio propriamente dito vive em `bot/venue_trader.py`, um executor por
    # venue. Aqui ficam só as conversões que dependem do ContractSpec da MEXC.
    #
    # As antigas `_spot_send` / `_futures_send` / `_wait_*_fill` foram
    # REMOVIDAS junto com a ligação do venue_trader: elas montavam símbolo no
    # formato da MEXC e usavam `self.spot_client` / `self.futures_client`
    # direto, então continuariam mandando tudo para a MEXC independentemente do
    # venue configurado — o bug 18. Mantê-las como "código legado inofensivo"
    # seria um convite a chamá-las de novo, do mesmo jeito que manter o nome
    # `new_order_limit_ioc` seria um convite a reintroduzir o bug 17.

    @staticmethod
    def _round_futures_vol(vol: float, spec: ContractSpec) -> float:
        """Arredonda volume de contratos para baixo na escala permitida."""
        if vol <= 0:
            return 0.0
        if spec.vol_scale <= 0:
            return float(math.floor(vol))
        factor = 10 ** spec.vol_scale
        return math.floor(vol * factor) / factor

    async def _revert_futures_leg(
        self, symbol: str, trader: Optional[VenueTrader], vol: float,
        spec: ContractSpec, reason: str, ref_price: Optional[float] = None,
    ):
        """
        Fecha a mercado uma posição de futures aberta indevidamente (perna órfã).

        Note que isto só existe porque as duas pernas estão na MESMA conta.
        Em cross-exchange não há reversão possível — é a razão de esse modo
        continuar desligado por padrão.
        """
        if trader is None:
            logger.critical(
                "[LIVE] FALHA CRÍTICA: sem executor para reverter a perna Futures em %s "
                "(motivo: %s). INTERVENÇÃO MANUAL URGENTE.", symbol, reason,
            )
            return
        try:
            await trader.run_leg("close_sell_leg", vol * spec.contract_size, None, ref_price=ref_price)
            logger.warning("[LIVE] Perna Futures revertida com sucesso em %s (motivo: %s).", symbol, reason)
        except Exception as e:
            logger.critical(
                "[LIVE] FALHA CRÍTICA: não foi possível reverter a perna Futures em %s após erro na "
                "perna Spot. INTERVENÇÃO MANUAL URGENTE NECESSÁRIA em %s. Erro: %s",
                symbol, trader.venue.key, e,
            )

    # ---------------- Saída ----------------

    async def _execute_exit(
        self, cfg: PairConfig, rt: PairRuntime, executable: ExecutableSpread,
        spec: ContractSpec, screen_exit_spread_pct: float,
    ):
        rt.state = PairState.EXITING
        await self._broadcast_snapshot()

        if self.execution_mode == ExecutionMode.SIMULATION:
            await self._execute_exit_simulated(
                cfg, rt, executable.spot.avg_price, executable.futures.avg_price, executable.spread_pct,
            )
        else:
            await self._execute_exit_live(cfg, rt, executable, spec, screen_exit_spread_pct)

    async def _execute_exit_simulated(self, cfg: PairConfig, rt: PairRuntime, spot_price: float, futures_price: float, spread_pct: float):
        pnl_spot = None
        pnl_futures = None
        if rt.entry_spot_price and rt.entry_spot_qty:
            pnl_spot = (spot_price - rt.entry_spot_price) * rt.entry_spot_qty
        if rt.entry_futures_price and rt.entry_futures_vol and cfg.symbol in self.contract_specs:
            spec = self.contract_specs[cfg.symbol]
            # short: lucro quando o preço CAI
            pnl_futures = (rt.entry_futures_price - futures_price) * rt.entry_futures_vol * spec.contract_size
        elif rt.entry_futures_price and rt.entry_notional_usdt:
            # sem spec de contrato carregado ainda: aproxima pela variação percentual do notional
            pct_change = (rt.entry_futures_price - futures_price) / rt.entry_futures_price
            pnl_futures = rt.entry_notional_usdt * pct_change

        total_pnl = (pnl_spot or 0) + (pnl_futures or 0)

        await self.storage.log_event(cfg.symbol, "exit_simulated", {
            "exit_spread_pct": spread_pct, "exit_spot_price": spot_price, "exit_futures_price": futures_price,
            "entry_spread_pct": rt.entry_spread_pct, "pnl_spot_usdt": pnl_spot, "pnl_futures_usdt": pnl_futures,
            "pnl_total_usdt": total_pnl,
        }, simulated=True)

        logger.info(
            "[SIMULAÇÃO] Saída em %s: spread=%.2f%% pnl_total=%.4f USDT (spot=%.4f, futures=%.4f)",
            cfg.symbol, spread_pct, total_pnl, pnl_spot or 0, pnl_futures or 0,
        )

        self._clear_runtime_position(rt)
        await self.storage.clear_position(cfg.symbol)
        await self._broadcast_snapshot()

    async def _execute_exit_live(
        self, cfg: PairConfig, rt: PairRuntime, executable: ExecutableSpread,
        spec: ContractSpec, screen_exit_spread_pct: float,
    ):
        """
        Fecha as duas pernas EM PARALELO, com TETO DE SLIPPAGE em cada uma.

        ## O que mudou em relação à versão anterior

        A saída continua paralela e continua priorizando velocidade — a
        decisão explícita de "preciso que o bot saia o mais rápido possível"
        está preservada. O que mudou é que "rápido" deixou de significar
        "a qualquer preço".

        Antes, as duas pernas eram ordens a mercado puras, sem nenhum limite.
        Foi essa saída que custou 1,21 ponto percentual em 03/08: a tela
        mostrava spread de saída de 0,23% e o realizado foi 1,44%.

        Agora cada perna sai como IOC com preço ancorado no VWAP que a
        confirmação de profundidade acabou de medir. O tempo de exposição é
        praticamente o mesmo (IOC executa contra o book na mesma latência de
        uma ordem a mercado); o que se ganha é que o preço não pode fugir
        arbitrariamente. Se ainda assim faltar quantidade depois das
        tentativas, o resíduo escala para mercado — porque numa SAÍDA ficar
        com uma perna aberta é pior que pagar caro no pedaço que faltou.

        Continua valendo: se a perna Spot não se confirmar como vendida, o
        par é pausado em PAUSED_ERROR (visível na aba Bot), nunca registrado
        como saída concluída com PnL incompleto.
        """
        buy_trader, sell_trader = self._traders_for_pair(cfg)
        if buy_trader is None or sell_trader is None:
            # Numa SAÍDA não há como recuar: a posição já existe. Pausar com o
            # resíduo preservado é a única conduta correta — declarar saída
            # concluída sem ter fechado foi o bug 15.
            rt.state = PairState.PAUSED_ERROR
            rt.last_error = (
                f"Sem executor para {cfg.buy_venue} e/ou {cfg.sell_venue} no momento da SAÍDA. "
                "A POSIÇÃO CONTINUA ABERTA — feche manualmente."
            )
            logger.critical("[LIVE] Saída de %s impossível: %s", cfg.symbol, rt.last_error)
            await self._broadcast_snapshot()
            return

        futures_symbol = sell_trader.spec.native_symbol
        spot_symbol = buy_trader.spec.native_symbol
        base_asset = cfg.symbol

        # Tetos ancorados no VWAP confirmado. Fechar o short é uma COMPRA de
        # futures (teto de preço); fechar o spot é uma VENDA (piso de preço).
        futures_cap = limit_price_for_buy(
            executable.futures.avg_price, self.slippage_policy, sell_trader.spec.price_tick,
        )
        spot_floor = limit_price_for_sell(
            executable.spot.avg_price, self.slippage_policy, buy_trader.spec.price_tick,
        )

        async def spot_free_balance() -> Optional[float]:
            """
            Saldo realmente disponível do ativo base.

            A exchange desconta a taxa de trading na própria moeda comprada,
            então a quantidade líquida na carteira é sempre um pouco menor que
            a comprada. Tentar vender a quantidade "de livro" dispara
            `Insufficient position` — este teto é o que impede isso, e é
            reconsultado a cada tentativa porque o saldo muda conforme as
            tentativas anteriores vão preenchendo.
            """
            try:
                return await buy_trader.free_balance(base_asset)
            except Exception as e:
                logger.warning("Não foi possível consultar saldo antes de vender %s: %s", spot_symbol, e)
                return None

        # Quantidade da perna vendida na MOEDA BASE (ver a entrada).
        exit_base_qty = rt.entry_futures_vol * spec.contract_size

        # As duas pernas são fechadas SIMULTANEAMENTE (asyncio.gather), não
        # em sequência - isso corta pela metade o tempo total de exposição
        # durante a saída.
        futures_leg, spot_leg = await asyncio.gather(
            execute_bounded(
                label=f"{cfg.symbol} futures/fechar-short",
                total_qty=exit_base_qty,
                limit_price=futures_cap,
                send_ioc=lambda q, p: sell_trader.run_leg("close_sell_leg", q, p),
                send_market=lambda q: sell_trader.run_leg(
                    "close_sell_leg", q, None, ref_price=executable.futures.avg_price,
                ),
                policy=self.slippage_policy,
                round_qty=sell_trader.round_qty,
            ),
            execute_bounded(
                label=f"{cfg.symbol} spot/vender",
                total_qty=rt.entry_spot_qty,
                limit_price=spot_floor,
                send_ioc=lambda q, p: buy_trader.run_leg("close_buy_leg", q, p),
                send_market=lambda q: buy_trader.run_leg(
                    "close_buy_leg", q, None, ref_price=executable.spot.avg_price,
                ),
                policy=self.slippage_policy,
                qty_cap_provider=spot_free_balance,
                round_qty=buy_trader.round_qty,
            ),
            return_exceptions=True,
        )

        if isinstance(futures_leg, Exception):
            logger.critical("[LIVE] Falha ao fechar perna Futures em %s: %s", cfg.symbol, futures_leg)
            futures_leg = LegFill(requested_qty=exit_base_qty, errors=[str(futures_leg)])
        if isinstance(spot_leg, Exception):
            logger.critical("[LIVE] Falha ao fechar perna Spot em %s: %s", cfg.symbol, spot_leg)
            spot_leg = LegFill(requested_qty=rt.entry_spot_qty, errors=[str(spot_leg)])

        # De volta para contratos, a unidade em que a posição é contabilizada.
        futures_closed_vol = futures_leg.filled_qty / spec.contract_size
        spot_closed_qty = spot_leg.filled_qty

        # Preço realizado SÓ existe se houve preenchimento.
        #
        # Antes, o código caía para o VWAP projetado quando a perna não
        # preenchia (`futures_leg.avg_price or executable.futures.avg_price`).
        # Esse fallback fabricava um número: em 05/08/2026 a perna de Futures
        # fechou ZERO contratos e mesmo assim a operação foi registrada com
        # "realizado +1,08%" — calculado contra um preço que nunca executou.
        # É exatamente o fallback silencioso que este projeto proíbe: um
        # número que parece válido e descreve algo que não aconteceu.
        futures_avg_price = futures_leg.avg_price if futures_closed_vol > 0 else None
        spot_avg_price = spot_leg.avg_price if spot_closed_qty > 0 else None

        pnl_spot = (
            (spot_avg_price - rt.entry_spot_price) * spot_closed_qty
            if rt.entry_spot_price and spot_avg_price else None
        )
        pnl_futures = (
            (rt.entry_futures_price - futures_avg_price) * futures_closed_vol * spec.contract_size
            if rt.entry_futures_price and futures_avg_price else None
        )
        total_pnl = (pnl_spot or 0) + (pnl_futures or 0)

        # Custo estimado das taxas desta saída, em USDT, para o PnL reportado
        # deixar de ser bruto. As taxas sempre existiram; só não apareciam.
        fees = self.fees_for(cfg.symbol)
        exit_fee_usdt = (
            (spot_closed_qty * spot_avg_price * fees.spot_taker_pct / 100 if spot_avg_price else 0.0)
            + (futures_closed_vol * spec.contract_size * futures_avg_price * fees.futures_taker_pct / 100
               if futures_avg_price else 0.0)
        )

        # ------------------------------------------------------------------
        # A saída só está concluída quando AS DUAS pernas fecharam.
        #
        # Este bloco existe por causa de 05/08/2026: a perna Spot vendeu, a de
        # Futures não fechou nenhum contrato (3 tentativas IOC + escalonamento
        # a mercado, todas sem preenchimento), e o bot mesmo assim registrou a
        # operação como concluída, limpou a posição do banco e do runtime, e
        # reportou um resultado líquido positivo.
        #
        # Consequências, todas graves:
        #   - restou um SHORT DESCOBERTO de 800 JIMOTHY, sem o spot que o
        #     protegia (o hedge tinha acabado de ser vendido);
        #   - o bot ficou sem saber disso, livre para abrir uma NOVA posição
        #     no próximo sinal, empilhando shorts;
        #   - o PnL reportado (-1,4687 USDT) era só a perna Spot, porque a
        #     perna de Futures contribuiu com zero.
        #
        # A versão anterior só verificava a perna Spot. A verificação precisa
        # ser SIMÉTRICA: qualquer perna que não fechou deixa a operação em um
        # estado que exige intervenção, e o pior desfecho possível é o bot
        # achar que está fora do mercado quando não está.
        # ------------------------------------------------------------------
        pernas_pendentes = []
        if not spot_leg.complete:
            pernas_pendentes.append(
                f"SPOT vendeu {spot_closed_qty:.6g} de {rt.entry_spot_qty:.6g} "
                f"(erros: {'; '.join(spot_leg.errors) or 'nenhum — o book fugiu do teto de slippage'})"
            )
        if not futures_leg.complete:
            pernas_pendentes.append(
                f"FUTURES fechou {futures_closed_vol:.6g} de {rt.entry_futures_vol:.6g} contratos "
                f"(erros: {'; '.join(futures_leg.errors) or 'nenhum — o book fugiu do teto de slippage'})"
            )

        if pernas_pendentes:
            posicao_real = await self._describe_exchange_position(futures_symbol)
            rt.state = PairState.PAUSED_ERROR
            rt.last_error = (
                "SAÍDA INCOMPLETA — a posição NÃO está fechada e você pode estar exposto "
                "direcionalmente agora. " + " | ".join(pernas_pendentes) +
                f". Situação na MEXC: {posicao_real}. "
                "Feche a perna restante manualmente (ou use o kill switch) antes de retomar este par."
            )
            # A posição é PRESERVADA no runtime e no banco de propósito: são
            # esses números que o kill switch usa para conseguir fechar o que
            # sobrou, e que você usa para conferir na MEXC.
            await self.storage.upsert_position(
                cfg.symbol, PairState.PAUSED_ERROR.value, simulated=False,
                entry_spread_pct=rt.entry_spread_pct, entry_spot_price=rt.entry_spot_price,
                entry_futures_price=rt.entry_futures_price,
                entry_spot_qty=max(0.0, rt.entry_spot_qty - spot_closed_qty),
                entry_futures_vol=max(0.0, rt.entry_futures_vol - futures_closed_vol),
                entry_notional_usdt=rt.entry_notional_usdt, entry_ts=rt.entry_ts,
            )
            # O runtime passa a refletir só o que RESTA aberto, para o kill
            # switch fechar exatamente o resíduo e não a posição original.
            rt.entry_spot_qty = max(0.0, rt.entry_spot_qty - spot_closed_qty)
            rt.entry_futures_vol = max(0.0, rt.entry_futures_vol - futures_closed_vol)

            await self.storage.log_event(cfg.symbol, "exit_incomplete_legs", {
                "exit_spread_executable_pct": executable.spread_pct,
                "exit_spread_signal_pct": screen_exit_spread_pct,
                "spot_closed_qty": spot_closed_qty, "spot_requested_qty": spot_leg.requested_qty,
                "futures_closed_vol": futures_closed_vol, "futures_requested_vol": futures_leg.requested_qty,
                "spot_errors": spot_leg.errors, "futures_errors": futures_leg.errors,
                "spot_attempts": spot_leg.attempts, "futures_attempts": futures_leg.attempts,
                "spot_escalated_to_market": spot_leg.escalated,
                "futures_escalated_to_market": futures_leg.escalated,
                "spot_limit_price": spot_floor, "futures_limit_price": futures_cap,
                "exit_spot_price": spot_avg_price, "exit_futures_price": futures_avg_price,
                "pnl_spot_usdt": pnl_spot, "pnl_futures_usdt": pnl_futures,
                "exchange_position": posicao_real,
            }, simulated=False)
            logger.critical(
                "[LIVE] SAÍDA INCOMPLETA em %s — POSIÇÃO AINDA ABERTA. %s. Situação na MEXC: %s",
                cfg.symbol, " | ".join(pernas_pendentes), posicao_real,
            )
            await self._broadcast_snapshot()
            return

        # Spread REALIZADO da saída: calculado a partir dos preços que de
        # fato executaram, não do "spread de tela" que disparou a decisão.
        exit_spread_realized = (
            (futures_avg_price - spot_avg_price) / spot_avg_price * 100 if spot_avg_price > 0 else None
        )

        net_pct = (
            realized_pnl_pct(rt.entry_spread_pct or 0.0, exit_spread_realized, fees)
            if exit_spread_realized is not None else None
        )

        await self.storage.log_event(cfg.symbol, "exit_live", {
            # Spread realizado (dos fills) - é o que a interface mostra
            "exit_spread_pct": exit_spread_realized,
            # Spread de topo de book que disparou o gatilho
            "exit_spread_signal_pct": screen_exit_spread_pct,
            # Spread que a confirmação de profundidade projetou para esta posição
            "exit_spread_executable_pct": executable.spread_pct,
            # Quanto o topo do book estava otimista em relação ao executável
            "depth_cost_pct": executable.depth_cost_pct,
            # Quanto se perdeu entre o executável projetado e o fill de fato
            "execution_slippage_pct": exit_spread_realized - executable.spread_pct
                                      if exit_spread_realized is not None else None,
            "exit_spot_price": spot_avg_price, "exit_futures_price": futures_avg_price,
            "spot_limit_price": spot_floor, "futures_limit_price": futures_cap,
            "spot_attempts": spot_leg.attempts, "futures_attempts": futures_leg.attempts,
            "spot_escalated_to_market": spot_leg.escalated,
            "futures_escalated_to_market": futures_leg.escalated,
            "entry_spread_pct": rt.entry_spread_pct,
            "pnl_spot_usdt": pnl_spot, "pnl_futures_usdt": pnl_futures,
            "pnl_total_usdt": total_pnl,
            "fee_cost_usdt_exit": exit_fee_usdt,
            "net_pct": net_pct,
            # Erros das tentativas são gravados MESMO na saída bem-sucedida.
            # Sem isso, uma perna que só preencheu na terceira tentativa (ou
            # após escalonamento) não deixa rastro do motivo — e o buffer de
            # log em memória é curto e some no reinício, que foi exatamente
            # como o motivo da falha de 05/08 se perdeu.
            "spot_errors": spot_leg.errors, "futures_errors": futures_leg.errors,
            "spot_closed_qty": spot_closed_qty, "futures_closed_vol": futures_closed_vol,
        }, simulated=False)

        logger.info(
            "[LIVE] Saída REAL em %s: realizado=%.3f%% | executável projetado=%.3f%% | "
            "topo do book=%.3f%% | slippage de execução=%.3f pp | resultado da operação=%.3f%% "
            "líquido | pnl_bruto=%.4f USDT (spot=%.4f, futures=%.4f)",
            cfg.symbol, exit_spread_realized if exit_spread_realized is not None else 0,
            executable.spread_pct, screen_exit_spread_pct,
            (exit_spread_realized - executable.spread_pct) if exit_spread_realized is not None else 0,
            net_pct if net_pct is not None else 0,
            total_pnl, pnl_spot or 0, pnl_futures or 0,
        )

        self._clear_runtime_position(rt)
        await self.storage.clear_position(cfg.symbol)
        await self._broadcast_snapshot()

    async def _describe_exchange_position(self, futures_symbol: str) -> str:
        """
        Pergunta à MEXC o que ESTÁ de fato aberto neste contrato, em texto
        pronto para a mensagem de erro.

        O estado interno do bot é uma crença sobre o mundo; a exchange é o
        mundo. Quando os dois divergem — e em 05/08/2026 divergiram: o bot se
        deu por zerado enquanto havia 800 JIMOTHY vendidos — a única
        informação útil é a da exchange. Por isso a mensagem de erro carrega o
        que a MEXC responde, não o que o bot achava.

        Nunca levanta exceção: esta função roda dentro de um caminho de erro,
        e falhar aqui não pode esconder o erro original.
        """
        if self.futures_client is None:
            return "modo simulação, sem posição real."
        try:
            resp = await self.futures_client.get_open_positions(futures_symbol)
            data = resp.get("data") or []
            if not data:
                return "a MEXC não reporta nenhuma posição aberta neste contrato."
            partes = []
            for p in data:
                tipo = "SHORT" if p.get("positionType") == 2 else "LONG"
                partes.append(
                    f"{tipo} de {p.get('holdVol')} contratos "
                    f"(preço médio {p.get('holdAvgPrice')})"
                )
            return "a MEXC reporta POSIÇÃO ABERTA: " + "; ".join(partes)
        except Exception as e:
            return f"não foi possível confirmar na MEXC ({e}) — confira manualmente."

    async def _close_futures_market(
        self, trader: Optional[VenueTrader], vol: float, reference_price: float,
        spec: Optional[ContractSpec], symbol: str = "",
    ) -> tuple[float, float]:
        """
        Fecha a posição de Futures com UMA ÚNICA ordem a mercado (reduce_only).

        Desde que a saída normal passou a usar IOC com teto de slippage
        (`_execute_exit_live`), este caminho ficou reservado ao KILL SWITCH.
        Ali a ausência de teto é a escolha certa e deliberada: o kill switch
        é uma parada de emergência, em que sair a qualquer preço é
        preferível a continuar exposto — que é exatamente o trade-off oposto
        ao da saída normal.
        """
        if not vol or vol <= 0 or trader is None:
            return 0.0, reference_price

        contract_size = spec.contract_size if spec else 1.0
        try:
            fill = await trader.run_leg(
                "close_sell_leg", vol * contract_size, None, ref_price=reference_price,
            )
        except Exception as e:
            logger.critical(
                "[LIVE] FALHA CRÍTICA: fechamento a mercado de Futures falhou em %s (%s). "
                "INTERVENÇÃO MANUAL URGENTE. Erro: %s", symbol, trader.venue.key, e,
            )
            return 0.0, reference_price

        if fill:
            base = fill["filled_qty"]
            preco = fill["notional"] / base if base > 0 else reference_price
            return base / contract_size, preco

        logger.critical(
            "[LIVE] FALHA CRÍTICA: fechamento a mercado de Futures em %s (%s) não preencheu nada. "
            "VERIFIQUE MANUALMENTE.", symbol, trader.venue.key,
        )
        return 0.0, reference_price

    async def _close_spot_market(
        self, trader: Optional[VenueTrader], qty: float, reference_price: float,
        symbol_display: str = "",
    ) -> tuple[float, float]:
        """
        Vende a posição Spot com UMA ÚNICA ordem a mercado.

        Assim como `_close_futures_market`, ficou reservado ao KILL SWITCH
        depois que a saída normal passou a ter teto de slippage. Ver a
        justificativa lá.

        Duas proteções importantes:
        1. Consulta o saldo real disponível antes de vender (a MEXC desconta
           taxa de trading na própria moeda comprada, então a quantidade
           líquida disponível é sempre um pouco menor que a calculada na
           entrada) para nunca tentar vender mais do que existe e disparar
           "Insufficient position".
        2. Arredonda a quantidade para a precisão decimal exigida pela MEXC
           para este símbolo (spot_specs), sempre para BAIXO. Sem isso, a
           MEXC rejeita a ordem com "amount scale is invalid" quando o saldo
           consultado vem com mais casas decimais do que o permitido (comum
           por arredondamento de ponto flutuante).

        Se a venda falhar de qualquer forma, propaga a exceção para o
        chamador em vez de engolir o erro e retornar "vendeu 0" - isso
        garante que o par seja pausado com PAUSED_ERROR visível, em vez de
        a operação parecer ter fechado normalmente com o PnL incompleto.
        """
        if not qty or qty <= 0 or trader is None:
            return 0.0, reference_price

        base_asset = symbol_display or trader.spec.symbol
        spot_symbol = trader.spec.native_symbol
        try:
            livre = await trader.free_balance(base_asset)
            sell_qty = min(qty, livre) if livre is not None else qty
        except Exception as e:
            logger.warning(
                "Não foi possível consultar saldo de %s antes de vender a mercado: %s. "
                "Prosseguindo com a quantidade calculada internamente.", base_asset, e,
            )
            sell_qty = qty

        # Arredondamento na precisão do venue, sempre para baixo. Sem isso a
        # exchange rejeita com "amount scale is invalid" quando o saldo
        # consultado vem com mais casas decimais que o permitido (bug 5).
        sell_qty = trader.round_qty(sell_qty)

        if sell_qty <= 0:
            logger.warning(
                "Saldo livre de %s é zero (ou zerou após arredondamento de precisão) no "
                "fechamento a mercado de %s - nada a vender.",
                base_asset, spot_symbol,
            )
            return 0.0, reference_price

        # Sem try/except aqui de propósito: se a ordem for REJEITADA pela
        # exchange, a exceção sobe para quem chamou, que deve tratar isso como
        # falha real (pausar o par) em vez de "vendeu 0".
        fill = await trader.run_leg("close_buy_leg", sell_qty, None, ref_price=reference_price)

        if not fill or fill["filled_qty"] <= 0:
            raise RuntimeError(
                f"Ordem de venda Spot em {spot_symbol} foi aceita mas o preenchimento não pôde ser "
                f"confirmado (nem na resposta imediata, nem consultando o status da ordem depois)."
            )
        filled_qty = fill["filled_qty"]
        quote_qty = fill["notional"]
        fill_price = quote_qty / filled_qty if quote_qty > 0 else reference_price
        return filled_qty, fill_price

    def _clear_runtime_position(self, rt: PairRuntime, new_state: PairState = PairState.IDLE):
        """
        Limpa os campos de posição (entry_*) e define o novo estado.
        Por padrão volta para IDLE (uso normal de saída), mas o kill switch
        passa MANUAL_HALT explicitamente para não ser sobrescrito.
        """
        rt.state = new_state
        rt.entry_spread_pct = None
        rt.entry_spot_price = None
        rt.entry_futures_price = None
        rt.entry_spot_qty = None
        rt.entry_futures_vol = None
        rt.entry_notional_usdt = None
        rt.entry_ts = None
        rt.last_error = None

    # ---------------- Kill switch ----------------

    async def kill_switch(self):
        """
        Fecha imediatamente todas as posições abertas e move todos os pares
        para MANUAL_HALT. Em modo LIVE, fecha as duas pernas de cada posição
        A MERCADO (prioriza velocidade sobre preço - é uma parada de
        emergência) e cancela quaisquer ordens pendentes.
        """
        mode_label = "LIVE" if self.execution_mode == ExecutionMode.LIVE else "SIMULAÇÃO"
        logger.warning("[%s] KILL SWITCH acionado — fechando todas as posições e pausando o bot.", mode_label)

        async with self._lock:
            # Fecha tudo que tenha QUANTIDADE registrada, não só o que está em
            # OPEN.
            #
            # Antes, o kill switch só olhava PairState.OPEN — justamente o
            # estado em que uma posição problemática NÃO está. Um par que caiu
            # em PAUSED_ERROR por saída incompleta (05/08/2026: short de 800
            # JIMOTHY descoberto) era ignorado pelo botão de emergência, que é
            # exatamente quando ele mais precisa funcionar. ENTERING e EXITING
            # têm o mesmo problema: são os estados de uma operação
            # interrompida no meio.
            open_positions = [
                (s, rt) for s, rt in self.runtimes.items()
                if rt.state in (
                    PairState.OPEN, PairState.ENTERING,
                    PairState.EXITING, PairState.PAUSED_ERROR,
                )
                and (rt.entry_futures_vol or rt.entry_spot_qty)
            ]
            if open_positions:
                logger.warning(
                    "Kill switch: %d par(es) com posição registrada para fechar: %s",
                    len(open_positions),
                    ", ".join(f"{s}({rt.state.value})" for s, rt in open_positions),
                )

        for symbol, rt in open_positions:
            if self.execution_mode == ExecutionMode.LIVE:
                spec = self.contract_specs.get(symbol)
                cfg = self.configs.get(symbol)
                # O kill switch precisa fechar onde a posição REALMENTE está.
                # Assumir MEXC aqui fecharia na exchange errada e deixaria a
                # posição real intacta — com o operador convencido de que o
                # botão de emergência funcionou.
                buy_trader = self.trader_for(symbol, cfg.buy_venue) if cfg else None
                sell_trader = self.trader_for(symbol, cfg.sell_venue) if cfg else None

                for nome, trader in (("Futures", sell_trader), ("Spot", buy_trader)):
                    if trader is None:
                        logger.critical(
                            "Kill switch: SEM EXECUTOR para a perna %s de %s. "
                            "A posição pode continuar aberta. VERIFIQUE MANUALMENTE.", nome, symbol,
                        )
                        continue
                    try:
                        await trader.cancel_all()
                    except Exception as e:
                        logger.warning(
                            "Kill switch: falha ao cancelar ordens abertas de %s em %s: %s", nome, symbol, e,
                        )

                await asyncio.gather(
                    self._close_futures_market(
                        sell_trader, rt.entry_futures_vol, rt.entry_futures_price, spec, symbol,
                    ),
                    self._close_spot_market(
                        buy_trader, rt.entry_spot_qty, rt.entry_spot_price, symbol,
                    ),
                    return_exceptions=True,
                )

            await self.storage.log_event(symbol, "kill_switch_close", {
                "entry_spread_pct": rt.entry_spread_pct,
            }, simulated=(self.execution_mode == ExecutionMode.SIMULATION))
            await self.storage.clear_position(symbol)

        async with self._lock:
            for rt in self.runtimes.values():
                self._clear_runtime_position(rt, new_state=PairState.MANUAL_HALT)
            for cfg in self.configs.values():
                cfg.enabled = False

        for symbol, cfg in self.configs.items():
            await self.storage.upsert_pair_config(
                symbol, False, cfg.entry_spread_pct, cfg.exit_spread_pct, cfg.position_size_usdt
            )

        await self._broadcast_snapshot()

    async def resume_from_halt(self, symbol: str):
        """
        Sai manualmente de MANUAL_HALT/PAUSED_ERROR de volta para IDLE.
        Também limpa quaisquer dados de posição residual (entry_spot_qty,
        etc.) mantidos para referência enquanto pausado - a expectativa é
        que você já tenha conferido manualmente na MEXC antes de retomar,
        então não faz sentido manter esses dados velhos "pendurados" no
        runtime depois disso. Não reabre posição nova sozinho.
        """
        symbol = self._normalize_symbol(symbol)
        rt = self.runtimes.get(symbol)
        if rt and rt.state in (PairState.MANUAL_HALT, PairState.PAUSED_ERROR):
            self._clear_runtime_position(rt, new_state=PairState.IDLE)
            await self.storage.clear_position(symbol)
            await self._broadcast_snapshot()

    # ---------------- Snapshot ----------------

    def get_snapshot(self) -> list:
        result = []
        for symbol, rt in self.runtimes.items():
            cfg = self.configs.get(symbol)
            d = rt.to_dict()
            d["config"] = {
                "enabled": cfg.enabled if cfg else False,
                "entry_spread_pct": cfg.entry_spread_pct if cfg else None,
                "exit_spread_pct": cfg.exit_spread_pct if cfg else None,
                "position_size_usdt": cfg.position_size_usdt if cfg else None,
                "buy_venue": cfg.buy_venue if cfg else None,
                "sell_venue": cfg.sell_venue if cfg else None,
                "cross_exchange": cfg.cross_exchange if cfg else False,
            }
            d["execution_mode"] = self.execution_mode.value
            d["connection_degraded"] = self.connection_degraded
            result.append(d)
        return result
