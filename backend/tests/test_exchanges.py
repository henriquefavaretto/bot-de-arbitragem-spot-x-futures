"""
Testes da camada de adaptadores de exchange.

O teste mais importante daqui é
`test_asks_descendentes_da_bingx_sao_normalizados`: o spot da BingX é o único
dos seis venues que devolve os asks do PIOR para o melhor. Ler `asks[0]` como
topo do book não levanta exceção, não aparece em log, e faz o VWAP consumir o
livro ao contrário — reportando um preço executável melhor que a realidade
justamente na ponta que autoriza a ordem.
"""
import pytest

from bot.depth import BookLevel, walk_by_qty
from exchanges.base import (
    MarketType, Quote, Venue, build_order_book, levels_from_dicts, levels_from_pairs,
)
from exchanges.bingx import BingxFuturesAdapter, BingxSpotAdapter, _unwrap
from exchanges.gate import GateFuturesAdapter, GateSpotAdapter
from exchanges.mexc import MexcFuturesAdapter, MexcSpotAdapter
from exchanges.registry import (
    Combination, build_combinations, parse_venue_filter, venue_pairs,
)


class FakeHttp:
    """Devolve payloads fixos por URL, sem rede."""

    def __init__(self, por_url: dict):
        self.por_url = por_url
        self.chamadas: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.chamadas.append((url, params or {}))
        for chave, payload in self.por_url.items():
            if chave in url:
                return _Resp(payload)
        raise AssertionError(f"URL inesperada no teste: {url}")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# Normalização de ordenação do book
# ---------------------------------------------------------------------------

def test_build_order_book_ordena_bids_desc_e_asks_asc():
    book = build_order_book(
        "BTC",
        bids=[BookLevel(100, 1), BookLevel(102, 1), BookLevel(101, 1)],
        asks=[BookLevel(105, 1), BookLevel(103, 1), BookLevel(104, 1)],
        venue=Venue("x", MarketType.SPOT),
    )
    assert [n.price for n in book.bids] == [102, 101, 100]
    assert [n.price for n in book.asks] == [103, 104, 105]
    assert book.best_bid == 102
    assert book.best_ask == 103


def test_ordem_inesperada_gera_aviso_mas_e_corrigida(caplog):
    # Se a exchange mudar de convenção, a ordenação continua correta E o
    # aviso aparece — é a única chance de perceber antes de virar ordem.
    with caplog.at_level("WARNING"):
        book = build_order_book(
            "BTC",
            bids=[BookLevel(100, 1), BookLevel(102, 1)],  # crescente: contradiz "desc"
            asks=[BookLevel(103, 1), BookLevel(104, 1)],
            venue=Venue("x", MarketType.SPOT),
            declared_bid_order="desc",
        )
    assert book.best_bid == 102
    assert any("fora da ordem declarada" in r.message for r in caplog.records)


def test_ordem_declarada_corretamente_nao_gera_aviso(caplog):
    with caplog.at_level("WARNING"):
        build_order_book(
            "BTC",
            bids=[BookLevel(102, 1), BookLevel(100, 1)],
            asks=[BookLevel(105, 1), BookLevel(103, 1)],  # decrescente, e declarado assim
            venue=Venue("bingx", MarketType.SPOT),
            declared_ask_order="desc",
        )
    assert not any("fora da ordem declarada" in r.message for r in caplog.records)


async def test_asks_descendentes_da_bingx_sao_normalizados():
    """
    Payload com a forma REAL medida em 05/08/2026 para BTC-USDT na BingX
    spot: os asks vêm do pior para o melhor.

        64107.80, 64107.79, 64107.46, 64107.44, 64105.09

    O melhor ask é 64105.09 (o ÚLTIMO). Sem normalização, o topo do book
    sairia como 64107.80 e o VWAP consumiria o livro de trás para frente.
    """
    payload = {"code": 0, "data": {
        "bids": [["64105.07", "0.004416"], ["64102.71", "0.124464"]],
        "asks": [["64107.80", "0.212159"], ["64107.79", "0.000151"],
                 ["64107.46", "0.000751"], ["64107.44", "9.335185"],
                 ["64105.09", "0.002583"]],
    }}
    adapter = BingxSpotAdapter(FakeHttp({"market/depth": payload}))
    book = await adapter.fetch_depth("BTC")

    assert book.best_ask == pytest.approx(64105.09), "o melhor ask é o menor, não o primeiro"
    assert [n.price for n in book.asks] == sorted(n.price for n in book.asks)
    assert book.is_usable is True

    # A consequência prática: comprar 0.002 unidades sai ao melhor preço,
    # não ao pior. Sem normalização o VWAP seria ~0,004% maior — pequeno por
    # unidade, sistemático em toda operação, e invisível.
    exec_price = book.buy(0.002)
    assert exec_price.avg_price == pytest.approx(64105.09)


async def test_book_de_futures_da_gate_usa_objetos_s_p():
    # Único dos seis venues cujo book vem como [{"s": tamanho, "p": preco}].
    payload = {
        "current": 0,  # timestamp implausível: deve cair no relógio local
        "asks": [{"s": 30800, "p": "64086.2"}, {"s": 389, "p": "64086.6"}],
        "bids": [{"s": 31148, "p": "64086.1"}, {"s": 1561, "p": "64085.4"}],
    }
    adapter = GateFuturesAdapter(FakeHttp({"futures/usdt/order_book": payload}))
    book = await adapter.fetch_depth("BTC")

    assert book.best_ask == pytest.approx(64086.2)
    assert book.best_bid == pytest.approx(64086.1)
    # `s` é a quantidade em CONTRATOS e precisa sobreviver à conversão.
    assert book.asks[0].qty == pytest.approx(30800)
    assert book.age_s() < 5  # timestamp absurdo foi descartado


def test_niveis_invalidos_sao_descartados():
    niveis = levels_from_pairs([["100", "1"], ["abc", "1"], ["101", "0"], ["102", "-5"], ["103", "2"]])
    assert [n.price for n in niveis] == [100.0, 103.0]
    assert levels_from_dicts([{"p": "1", "s": "2"}, {"p": "x", "s": "1"}])[0].qty == 2.0


# ---------------------------------------------------------------------------
# Símbolos: canônico <-> nativo
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("adapter_cls,nativo", [
    (MexcSpotAdapter, "BTCUSDT"),
    (MexcFuturesAdapter, "BTC_USDT"),
    (GateSpotAdapter, "BTC_USDT"),
    (GateFuturesAdapter, "BTC_USDT"),
    (BingxSpotAdapter, "BTC-USDT"),
    (BingxFuturesAdapter, "BTC-USDT"),
])
def test_conversao_de_simbolo_faz_ida_e_volta(adapter_cls, nativo):
    # O bug 2 do projeto foi um símbolo com formato divergente atravessando
    # módulos. Com três exchanges e três formatos, a conversão precisa ser
    # exata nos dois sentidos, em todos os adaptadores.
    a = adapter_cls(None)
    assert a.to_native("BTC") == nativo
    assert a.to_canonical(nativo) == "BTC"


@pytest.mark.parametrize("adapter_cls,lixo", [
    (MexcSpotAdapter, "BTCBUSD"),
    (MexcFuturesAdapter, "BTCUSDT"),
    (GateSpotAdapter, "BTC-USDT"),
    (BingxSpotAdapter, "BTC_USDT"),
])
def test_simbolo_de_outro_formato_e_rejeitado(adapter_cls, lixo):
    assert adapter_cls(None).to_canonical(lixo) is None


# ---------------------------------------------------------------------------
# Tickers
# ---------------------------------------------------------------------------

async def test_gate_spot_le_lowest_ask_como_melhor_ask():
    payload = [{
        "currency_pair": "BTC_USDT", "last": "64100", "lowest_ask": "64112.7",
        "highest_bid": "64112.6", "quote_volume": "1000",
    }]
    a = GateSpotAdapter(FakeHttp({"spot/tickers": payload}))
    quotes = await a.fetch_tickers()
    q = quotes["BTC"]
    assert q.ask == pytest.approx(64112.7)
    assert q.bid == pytest.approx(64112.6)
    assert q.has_book is True
    assert q.venue.key == "gate:spot"


async def test_bingx_trata_code_diferente_de_zero_como_erro():
    # A BingX devolve HTTP 200 com code != 0 em caso de erro; tratar o status
    # HTTP como sucesso engoliria a falha silenciosamente.
    assert _unwrap({"code": 100400, "msg": "erro"}, "teste") is None
    assert _unwrap({"code": 0, "data": [1]}, "teste") == [1]

    a = BingxSpotAdapter(FakeHttp({"ticker/24hr": {"code": 100001, "msg": "falhou"}}))
    assert await a.fetch_tickers() == {}


async def test_quote_sem_book_nao_e_utilizavel():
    payload = [{"symbol": "XYZUSDT", "bidPrice": "0", "askPrice": "0",
                "lastPrice": "1.23", "quoteVolume": "10"}]
    a = MexcSpotAdapter(FakeHttp({"ticker/24hr": payload}))
    q = (await a.fetch_tickers())["XYZ"]
    # `last` existe, mas sem book a cotação não autoriza nada — a regra de
    # ouro do projeto aplicada já na entrada dos dados.
    assert q.last == pytest.approx(1.23)
    assert q.has_book is False
    assert q.book_ts == 0.0


# ---------------------------------------------------------------------------
# Combinações
# ---------------------------------------------------------------------------

def test_doze_combinacoes_por_simbolo_com_tres_exchanges():
    pares = venue_pairs()
    assert len(pares) == 12
    # 9 spot x futures + 3 futures x futures
    assert sum(1 for a, b in pares if a.market == MarketType.SPOT) == 9
    assert sum(1 for a, b in pares if a.market == MarketType.FUTURES) == 3


def test_nunca_gera_spot_contra_spot():
    # Sem instrumento vendido a descoberto dos dois lados não existe posição
    # neutra a montar — seria só comprar em dois lugares.
    for compra, venda in venue_pairs():
        assert not (compra.market == MarketType.SPOT and venda.market == MarketType.SPOT)


def test_perna_de_compra_de_spot_futures_e_sempre_o_spot():
    for compra, venda in venue_pairs():
        if venda.market == MarketType.SPOT:
            pytest.fail("spot nunca pode ser a perna vendida em spot x futures")


def test_combinacao_marca_cross_exchange_corretamente():
    mexc_s = Venue("mexc", MarketType.SPOT)
    mexc_f = Venue("mexc", MarketType.FUTURES)
    gate_f = Venue("gate", MarketType.FUTURES)

    assert Combination("BTC", mexc_s, mexc_f).cross_exchange is False
    assert Combination("BTC", mexc_s, gate_f).cross_exchange is True
    assert Combination("BTC", mexc_s, mexc_f).kind == "spot_futures"
    assert Combination("BTC", gate_f, mexc_f).kind == "futures_futures"


def test_venue_sem_book_nao_gera_combinacao():
    # Regra de ouro na origem: preferimos a linha não existir a existir com
    # um número calculado a partir de "último negociado".
    com_book = [Venue("mexc", MarketType.SPOT), Venue("mexc", MarketType.FUTURES)]
    combos = build_combinations("BTC", com_book)
    assert len(combos) == 1
    assert combos[0].buy_venue.key == "mexc:spot"


def test_filtro_de_venues_restringe_as_combinacoes():
    com_book = [
        Venue("mexc", MarketType.SPOT), Venue("mexc", MarketType.FUTURES),
        Venue("gate", MarketType.SPOT), Venue("gate", MarketType.FUTURES),
    ]
    apenas_gate = [Venue("gate", MarketType.SPOT), Venue("gate", MarketType.FUTURES)]
    combos = build_combinations("BTC", com_book, enabled_venues=apenas_gate)
    assert len(combos) == 1
    assert combos[0].key == "BTC|gate:spot|gate:futures"


def test_parse_do_filtro_de_venues():
    v = parse_venue_filter("mexc:spot, gate:futures")
    assert [x.key for x in v] == ["mexc:spot", "gate:futures"]


def test_filtro_vazio_significa_todos_nunca_nenhum():
    # Um filtro mal formado que virasse lista vazia esvaziaria o dashboard
    # inteiro sem explicação nenhuma.
    assert parse_venue_filter(None) is None
    assert parse_venue_filter("") is None
    assert parse_venue_filter("   ") is None
    assert parse_venue_filter("lixo,invalido") is None


def test_venue_ida_e_volta_pela_chave():
    v = Venue("bingx", MarketType.FUTURES)
    assert Venue.from_key(v.key) == v
    assert v.label == "BINGX Futures"
