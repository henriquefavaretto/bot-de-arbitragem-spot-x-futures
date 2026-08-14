"""
Registro de venues e geração das combinações de arbitragem.

## O que é uma combinação

Uma combinação é um par ordenado de venues `(compra, venda)` para o mesmo
símbolo canônico. A perna de COMPRA é onde se entra comprado; a de VENDA é
onde se abre o short. Com 3 exchanges e 2 mercados:

    Spot x Futures    3 spots x 3 futures = 9
    Futures x Futures C(3,2)              = 3
    -------------------------------------------
    total por símbolo                     = 12

Spot x Spot fica de fora de propósito: sem um instrumento vendido a
descoberto dos dois lados, não existe posição neutra a montar — seria só
comprar em dois lugares.

## Por que Futures x Futures não é direcionado

Em Spot x Futures a direção é fixa: compra-se o spot e vende-se o futures
(não dá para vender spot a descoberto sem margem). Já entre dois futures as
duas direções são possíveis, e qual delas vale depende do sinal do spread
naquele instante. Por isso a combinação é registrada UMA vez por par de
venues, e o sinal do spread indica de que lado entrar — registrar as duas
direções duplicaria cada linha do dashboard com a mesma informação
espelhada.
"""
import logging
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Optional

from exchanges.base import ExchangeAdapter, MarketType, Venue
from exchanges.bingx import BingxFuturesAdapter, BingxSpotAdapter
from exchanges.gate import GateFuturesAdapter, GateSpotAdapter
from exchanges.mexc import MexcFuturesAdapter, MexcSpotAdapter

logger = logging.getLogger("exchanges.registry")

#: Todas as classes de adaptador conhecidas. Adicionar uma exchange nova é
#: só acrescentar aqui — nada mais no sistema precisa saber o nome dela.
ADAPTER_CLASSES = (
    MexcSpotAdapter, MexcFuturesAdapter,
    GateSpotAdapter, GateFuturesAdapter,
    BingxSpotAdapter, BingxFuturesAdapter,
)

EXCHANGES = ("mexc", "gate", "bingx")


@dataclass(frozen=True)
class Combination:
    """
    Uma oportunidade potencial: comprar em `buy_venue`, vender em
    `sell_venue`.

    `cross_exchange` marca as combinações cujas pernas ficam em contas
    diferentes. Elas exigem capital pré-posicionado nas duas exchanges (não
    dá para netar as pernas nem transferir moeda na hora), e o modo de falha
    "uma perna executou e a outra não" é bem mais provável — são duas APIs
    independentes, com dois rate limits e dois conjuntos de erro. O bot usa
    esta marca para exigir credenciais dos dois lados antes de operar.
    """
    symbol: str
    buy_venue: Venue
    sell_venue: Venue

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.buy_venue.key}|{self.sell_venue.key}"

    @property
    def cross_exchange(self) -> bool:
        return self.buy_venue.exchange != self.sell_venue.exchange

    @property
    def kind(self) -> str:
        """'spot_futures' ou 'futures_futures' — decide a economia da operação."""
        if self.buy_venue.market == MarketType.SPOT:
            return "spot_futures"
        return "futures_futures"

    @property
    def label(self) -> str:
        return f"{self.buy_venue.label} → {self.sell_venue.label}"


def build_adapters(http_client) -> dict[str, ExchangeAdapter]:
    """Instancia todos os adaptadores, indexados pela chave do venue."""
    adapters: dict[str, ExchangeAdapter] = {}
    for cls in ADAPTER_CLASSES:
        adapter = cls(http_client)
        adapters[adapter.venue.key] = adapter
    return adapters


def all_venues() -> list[Venue]:
    return [Venue(ex, mt) for ex in EXCHANGES for mt in (MarketType.SPOT, MarketType.FUTURES)]


def venue_pairs(enabled_venues: Optional[Iterable[Venue]] = None) -> list[tuple[Venue, Venue]]:
    """
    Todos os pares `(compra, venda)` possíveis entre os venues habilitados.

    Spot x Futures: a direção é fixa (compra spot, vende futures), porque não
    se vende spot a descoberto.
    Futures x Futures: um par por combinação não ordenada — o sinal do spread
    é que diz de que lado entrar.
    """
    venues = list(enabled_venues) if enabled_venues is not None else all_venues()
    spots = [v for v in venues if v.market == MarketType.SPOT]
    futuros = [v for v in venues if v.market == MarketType.FUTURES]

    pares: list[tuple[Venue, Venue]] = []
    for s in spots:
        for f in futuros:
            pares.append((s, f))
    for a, b in combinations(sorted(futuros, key=lambda v: v.key), 2):
        pares.append((a, b))
    return pares


def build_combinations(
    symbol: str, venues_com_book: Iterable[Venue],
    enabled_venues: Optional[Iterable[Venue]] = None,
) -> list[Combination]:
    """
    Combinações montáveis para um símbolo, considerando só os venues em que
    ele tem book de verdade.

    A regra de ouro do projeto se aplica aqui na origem: um venue sem book
    não gera combinação nenhuma. Preferimos a linha não existir a existir com
    um número calculado a partir de "último negociado" — que já produziu
    cruzamento fantasma e recorde impossível no histórico.
    """
    disponiveis = set(venues_com_book)
    if enabled_venues is not None:
        disponiveis &= set(enabled_venues)

    return [
        Combination(symbol=symbol, buy_venue=compra, sell_venue=venda)
        for compra, venda in venue_pairs(enabled_venues)
        if compra in disponiveis and venda in disponiveis
    ]


def parse_venue_filter(raw: Optional[str]) -> Optional[list[Venue]]:
    """
    Interpreta o filtro de venues vindo da interface
    (ex: "mexc:spot,mexc:futures,gate:futures").

    `None` ou vazio significa "todos" — nunca "nenhum". Um filtro mal
    formado que virasse lista vazia esvaziaria o dashboard inteiro sem
    explicação, então entradas inválidas são descartadas individualmente e
    registradas.
    """
    if not raw or not raw.strip():
        return None
    venues = []
    for parte in raw.split(","):
        parte = parte.strip()
        if not parte:
            continue
        try:
            venues.append(Venue.from_key(parte))
        except (ValueError, KeyError):
            logger.warning("Filtro de venue ignorado por formato inválido: %r", parte)
    return venues or None
