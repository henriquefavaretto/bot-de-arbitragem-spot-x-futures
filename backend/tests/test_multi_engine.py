"""
Testes do motor multi-exchange.

O bloco mais importante é o do CONSENSO DE PREÇO. Sem ele, o modo
multi-exchange é ativamente perigoso: o mesmo ticker em duas exchanges não é
necessariamente o mesmo ativo, e montar a "arbitragem" de um par colidido
compra um ativo e vende outro — exposição direcional integral, hedge nenhum,
numa estratégia cuja premissa inteira é ser neutra.

Os números destes testes são medições reais de 05/08/2026 nos seis venues.
"""
import time

import pytest

from exchanges.base import MarketType, Quote, Venue
from exchanges.registry import Combination
from multi_engine import (
    MAX_PLAUSIBLE_SPREAD_PCT, MAX_VENUE_DEVIATION_PCT, MultiVenueEngine,
    compute_spread, evaluate_consensus,
)

MEXC_S = Venue("mexc", MarketType.SPOT)
MEXC_F = Venue("mexc", MarketType.FUTURES)
GATE_S = Venue("gate", MarketType.SPOT)
GATE_F = Venue("gate", MarketType.FUTURES)
BINGX_S = Venue("bingx", MarketType.SPOT)
BINGX_F = Venue("bingx", MarketType.FUTURES)


def q(venue: Venue, bid: float, ask: float, *, symbol="X", vol=1e6, funding=0.0, age=0.0) -> Quote:
    return Quote(
        symbol=symbol, venue=venue, native_symbol=symbol,
        bid=bid, ask=ask, last=(bid + ask) / 2, vol_usdt=vol,
        funding_rate=funding, book_ts=time.time() - age,
    )


# ---------------------------------------------------------------------------
# Os dois spreads, generalizados para qualquer par de venues
# ---------------------------------------------------------------------------

def test_entrada_e_saida_usam_lados_opostos_do_book():
    combo = Combination("X", MEXC_S, MEXC_F)
    compra = q(MEXC_S, bid=99.0, ask=100.0)
    venda = q(MEXC_F, bid=102.0, ask=103.0)

    s = compute_spread(combo, compra, venda, fee_round_trip_pct=0.14)

    # Entrada: compra no ask (100), vende no bid (102) -> +2%
    assert s.entry_spread_pct == pytest.approx(2.0)
    # Saída: vende no bid (99), recompra no ask (103) -> +4.04%
    assert s.exit_spread_pct == pytest.approx((103 - 99) / 99 * 100)
    # Os dois nunca podem ser o mesmo número: é a distinção que originou o
    # bug mais caro do projeto.
    assert s.entry_spread_pct != s.exit_spread_pct


def test_spread_liquido_desconta_as_quatro_pernas():
    combo = Combination("X", GATE_S, GATE_F)
    s = compute_spread(combo, q(GATE_S, 99.9, 100.0), q(GATE_F, 102.0, 102.1),
                       fee_round_trip_pct=0.55)
    assert s.net_spread_pct == pytest.approx(s.entry_spread_pct - 0.55)


def test_perna_sem_book_nao_produz_spread():
    combo = Combination("X", MEXC_S, MEXC_F)
    sem_book = Quote(symbol="X", venue=MEXC_S, native_symbol="X", bid=None, ask=None, last=1.0)
    assert compute_spread(combo, sem_book, q(MEXC_F, 1, 1.1), 0.14) is None
    assert compute_spread(combo, q(MEXC_S, 1, 1.1), sem_book, 0.14) is None


def test_idade_reportada_e_a_do_lado_mais_velho():
    # Um spread com um lado atual e outro parado é o PIOR caso, não a média
    # dos dois — a lição do bug 12, agora com seis fontes assíncronas.
    combo = Combination("X", MEXC_S, GATE_F)
    s = compute_spread(combo, q(MEXC_S, 99, 100, age=0.5), q(GATE_F, 102, 103, age=12.0), 0.14)
    assert s.max_age_s == pytest.approx(12.0, abs=0.5)


# ---------------------------------------------------------------------------
# CONSENSO DE PREÇO
# ---------------------------------------------------------------------------

def test_venues_concordantes_nao_produzem_outlier():
    # Caso BTC medido em 05/08/2026: os seis venues dentro de 0,07%.
    quotes = {
        "mexc:spot": q(MEXC_S, 64137.64, 64137.65),
        "mexc:futures": q(MEXC_F, 64094.4, 64094.5),
        "gate:spot": q(GATE_S, 64132.3, 64132.4),
        "gate:futures": q(GATE_F, 64104.1, 64104.2),
        "bingx:spot": q(BINGX_S, 64130.17, 64130.19),
        "bingx:futures": q(BINGX_F, 64100.1, 64101.0),
    }
    c = evaluate_consensus(quotes)
    assert c.outliers == set()
    assert c.has_consensus is True
    assert c.reference_price == pytest.approx(64117, abs=50)


def test_caso_vanry_gate_spot_e_detectado_como_ativo_diferente():
    """
    Medição real de 05/08/2026: cinco venues cotam VANRY em ~0,00333 e o
    gate:spot em 0,001451 — 2,3x menor. Não é spread, é outro ativo
    (redenominação). Operar isso compraria uma moeda e venderia outra.
    """
    quotes = {
        "bingx:futures": q(BINGX_F, 0.003325, 0.003338),
        "bingx:spot": q(BINGX_S, 0.00335, 0.003357),
        "gate:futures": q(GATE_F, 0.003331, 0.003333),
        "gate:spot": q(GATE_S, 0.001451, 0.001457),   # <- o impostor
        "mexc:futures": q(MEXC_F, 0.003328, 0.003329),
        "mexc:spot": q(MEXC_S, 0.003091, 0.003105),
    }
    c = evaluate_consensus(quotes)

    # A mediana dos seis mids é ~0,00333, arrastada por nada: quatro venues
    # estão praticamente em cima dela.
    assert c.reference_price == pytest.approx(0.00333, abs=0.00002)
    assert c.deviations_pct["gate:spot"] > 50

    # DOIS venues caem fora do limite de 5%: o gate:spot (56%, claramente
    # outro ativo) e também o mexc:spot, que está 6,97% abaixo do consenso.
    #
    # O segundo caso é o interessante: 7% pode ser uma deslocação real num
    # par ilíquido (o volume 24h do mexc:spot era de só 100k USDT), ou pode
    # ser mais uma colisão. Não dá para distinguir os dois pelo preço, e a
    # regra de ouro do projeto decide o empate: preferir não operar a operar
    # com dado incerto. Se este limite estiver conservador demais para o seu
    # caso, MAX_VENUE_DEVIATION_PCT é o botão — mas o padrão erra para o
    # lado de recusar.
    assert c.outliers == {"gate:spot", "mexc:spot"}
    assert c.deviations_pct["mexc:spot"] == pytest.approx(6.97, abs=0.2)
    assert MAX_VENUE_DEVIATION_PCT == 5.0

    # Os quatro venues que concordam seguem operáveis entre si.
    assert "mexc:futures" not in c.outliers
    assert "gate:futures" not in c.outliers
    assert "bingx:futures" not in c.outliers
    assert "bingx:spot" not in c.outliers


def test_caso_coti_divergencia_spot_versus_futures_na_mesma_exchange():
    """
    Medição real: todos os futures em ~0,0135 e todos os spots em ~0,0108 —
    20% de diferença, inclusive DENTRO da mesma exchange (mexc:spot contra
    mexc:futures). Derruba a intuição de que basta comparar exchanges: a
    verificação precisa ser por venue contra o consenso de todos.
    """
    quotes = {
        "bingx:futures": q(BINGX_F, 0.01358, 0.0136),
        "bingx:spot": q(BINGX_S, 0.01368, 0.01371),
        "gate:futures": q(GATE_F, 0.01347, 0.01348),
        "gate:spot": q(GATE_S, 0.010833, 0.01086),
        "mexc:futures": q(MEXC_F, 0.013557, 0.013558),
        "mexc:spot": q(MEXC_S, 0.01086, 0.010861),
    }
    c = evaluate_consensus(quotes)

    assert c.outliers == {"gate:spot", "mexc:spot"}
    # E o par futures x futures, que concorda com a mediana, segue operável.
    limpo = compute_spread(
        Combination("COTI", GATE_F, MEXC_F),
        quotes["gate:futures"], quotes["mexc:futures"], 0.095, consensus=c,
    )
    assert limpo.tradeable is True


def test_mediana_resiste_a_outlier_que_a_media_nao_resistiria():
    # Com a média, o próprio outlier arrasta a referência e se torna menos
    # detectável — e os venues corretos passam a parecer levemente errados.
    quotes = {
        "mexc:spot": q(MEXC_S, 100, 100),
        "mexc:futures": q(MEXC_F, 100, 100),
        "gate:spot": q(GATE_S, 100, 100),
        "gate:futures": q(GATE_F, 1000, 1000),  # 10x fora
    }
    c = evaluate_consensus(quotes)
    assert c.reference_price == pytest.approx(100)
    assert c.outliers == {"gate:futures"}


def test_sem_venues_suficientes_nao_acusa_ninguem():
    # Com dois venues não há como saber QUAL é o estranho; acusar um deles
    # seria escolher ao acaso.
    quotes = {"mexc:spot": q(MEXC_S, 100, 100.1), "gate:futures": q(GATE_F, 200, 200.1)}
    c = evaluate_consensus(quotes)
    assert c.outliers == set()
    assert c.has_consensus is False


def test_com_poucos_venues_o_limite_de_plausibilidade_assume():
    """
    Caso SPCX medido: gate:futures 115,66 contra gate:spot 99,88 — 15% na
    MESMA exchange, e só dois venues. Sem mediana confiável, quem protege é
    o limite de plausibilidade.
    """
    quotes = {"gate:spot": q(GATE_S, 99.88, 100.0), "gate:futures": q(GATE_F, 115.66, 115.67)}
    c = evaluate_consensus(quotes)
    assert c.outliers == set()

    s = compute_spread(
        Combination("SPCX", GATE_S, GATE_F),
        quotes["gate:spot"], quotes["gate:futures"], 0.55, consensus=c,
    )
    assert s.tradeable is False
    assert "implausível" in s.suspect_reason


def test_spread_normal_nao_e_marcado_como_suspeito():
    quotes = {
        "mexc:spot": q(MEXC_S, 0.006, 0.006006),
        "mexc:futures": q(MEXC_F, 0.00611, 0.006112),
        "gate:futures": q(GATE_F, 0.00609, 0.006095),
    }
    c = evaluate_consensus(quotes)
    s = compute_spread(
        Combination("JIMOTHY", MEXC_S, MEXC_F),
        quotes["mexc:spot"], quotes["mexc:futures"], 0.14, consensus=c,
    )
    assert s.tradeable is True
    assert s.suspect_reason is None
    assert s.entry_spread_pct == pytest.approx(1.73, abs=0.05)


def test_motivo_da_suspeita_e_preservado_e_nao_apenas_omitido():
    # Omitir a linha faria o operador procurar um número que sumiu. O motivo
    # precisa ser legível na tela.
    quotes = {
        "mexc:spot": q(MEXC_S, 100, 100.1),
        "mexc:futures": q(MEXC_F, 100, 100.1),
        "gate:futures": q(GATE_F, 100, 100.1),
        "gate:spot": q(GATE_S, 40, 40.1),
    }
    c = evaluate_consensus(quotes)
    s = compute_spread(
        Combination("X", GATE_S, MEXC_F), quotes["gate:spot"], quotes["mexc:futures"], 0.14, consensus=c,
    )
    assert s.tradeable is False
    assert "GATE Spot" in s.suspect_reason
    assert "não é o mesmo ativo" in s.suspect_reason


# ---------------------------------------------------------------------------
# Snapshot e filtros
# ---------------------------------------------------------------------------

def _engine_com_quotes(quotes_por_simbolo: dict) -> MultiVenueEngine:
    e = MultiVenueEngine(http_client=None)
    e.quotes = quotes_por_simbolo
    return e


def test_snapshot_barra_suspeitas_por_padrao_e_as_conta():
    e = _engine_com_quotes({"VANRY": {
        "bingx:futures": q(BINGX_F, 0.003325, 0.003338, symbol="VANRY"),
        "gate:futures": q(GATE_F, 0.003331, 0.003333, symbol="VANRY"),
        "mexc:futures": q(MEXC_F, 0.003328, 0.003329, symbol="VANRY"),
        "gate:spot": q(GATE_S, 0.001451, 0.001457, symbol="VANRY"),
    }})

    snap = e.get_snapshot()
    assert snap["suspect_filtered"] > 0
    assert all(r["tradeable"] for r in snap["rows"])
    assert not any(r["buy_venue"] == "gate:spot" for r in snap["rows"])

    # Com include_suspect, elas aparecem — com o motivo junto.
    completo = e.get_snapshot(include_suspect=True)
    suspeitas = [r for r in completo["rows"] if not r["tradeable"]]
    assert suspeitas and suspeitas[0]["suspect_reason"]


def test_snapshot_ordena_pelo_liquido_e_nao_pelo_bruto():
    """
    Ordenar pelo bruto colocaria no topo linhas de exchanges caras que não
    pagam a própria taxa: a Gate cobra 0,075% em futures contra 0,02% da
    MEXC, e o round-trip da Gate×Gate (0,55%) é quase 4x o da MEXC×MEXC.
    """
    e = _engine_com_quotes({"A": {
        "mexc:spot": q(MEXC_S, 99.9, 100.0, symbol="A"),
        "mexc:futures": q(MEXC_F, 101.0, 101.1, symbol="A"),
        "gate:spot": q(GATE_S, 99.9, 100.0, symbol="A"),
        "gate:futures": q(GATE_F, 101.05, 101.15, symbol="A"),
    }})
    linhas = e.get_snapshot()["rows"]
    assert linhas == sorted(linhas, key=lambda r: r["net_spread_pct"], reverse=True)
    # A linha Gate×Gate tem spread BRUTO maior e líquido menor.
    gate = next(r for r in linhas if r["key"].endswith("gate:spot|gate:futures"))
    mexc = next(r for r in linhas if r["key"].endswith("mexc:spot|mexc:futures"))
    assert gate["entry_spread_pct"] > mexc["entry_spread_pct"]
    assert gate["net_spread_pct"] < mexc["net_spread_pct"]


def test_filtro_de_venues_e_de_tipo_restringem_o_snapshot():
    e = _engine_com_quotes({"A": {
        "mexc:spot": q(MEXC_S, 99.9, 100.0, symbol="A"),
        "mexc:futures": q(MEXC_F, 101.0, 101.1, symbol="A"),
        "gate:futures": q(GATE_F, 101.0, 101.1, symbol="A"),
    }})

    assert len(e.get_snapshot()["rows"]) == 3  # 2 spot×fut + 1 fut×fut
    so_mexc = e.get_snapshot(enabled_venues=[MEXC_S, MEXC_F])["rows"]
    assert len(so_mexc) == 1
    so_fxf = e.get_snapshot(kinds=["futures_futures"])["rows"]
    assert len(so_fxf) == 1 and so_fxf[0]["kind"] == "futures_futures"


def test_limite_protege_o_tamanho_da_resposta():
    quotes = {
        f"S{i}": {
            "mexc:spot": q(MEXC_S, 99.9, 100.0, symbol=f"S{i}"),
            "mexc:futures": q(MEXC_F, 101.0, 101.1, symbol=f"S{i}"),
        }
        for i in range(50)
    }
    snap = _engine_com_quotes(quotes).get_snapshot(limit=10)
    assert snap["total_matching"] == 50
    assert snap["returned"] == 10
    assert len(snap["rows"]) == 10


def test_cotacao_velha_sai_das_combinacoes():
    e = _engine_com_quotes({"A": {
        "mexc:spot": q(MEXC_S, 99.9, 100.0, symbol="A", age=0.5),
        "mexc:futures": q(MEXC_F, 101.0, 101.1, symbol="A", age=999),
    }})
    assert e.get_snapshot()["rows"] == []


def test_taxa_de_round_trip_soma_as_quatro_pernas():
    e = MultiVenueEngine(http_client=None)
    # MEXC spot 0,05% + MEXC futures 0,02%, ida e volta
    assert e.fee_round_trip(Combination("A", MEXC_S, MEXC_F)) == pytest.approx(0.14)
    # Gate spot 0,20% + Gate futures 0,075%, ida e volta
    assert e.fee_round_trip(Combination("A", GATE_S, GATE_F)) == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# REGRESSÃO: o polling de tickers apagava a profundidade medida
# ---------------------------------------------------------------------------

class TickerFake:
    """Adaptador que devolve só preço, como o ticker em massa de MEXC e Gate."""

    def __init__(self, venue, symbol="A"):
        self.venue = venue
        self.symbol = symbol

    async def fetch_tickers(self):
        return {self.symbol: q(self.venue, 99.9, 100.0, symbol=self.symbol)}


async def test_polling_de_ticker_nao_apaga_a_profundidade_medida():
    """
    O ticker em massa (fora a BingX) não traz quantidade do topo. Substituir o
    objeto Quote inteiro a cada ciclo apagava a medição do enriquecedor a cada
    5 segundos — e como o snapshot é servido logo depois do polling, o número
    nunca chegava à tela, apesar de a medição estar funcionando.

    É o antipadrão do bug 8 ("a fonte pior vence por chegar depois") em outro
    campo: a precedência precisa ser explícita, não acidental.
    """
    e = MultiVenueEngine(http_client=None)
    e.adapters = {"mexc:spot": TickerFake(MEXC_S)}
    e.venue_status = {"mexc:spot": {"ok": False, "symbols": 0, "last_ok_ts": 0.0, "error": None}}

    await e.poll_once()
    assert e.quotes["A"]["mexc:spot"].bid_qty is None

    # O enriquecedor mede a profundidade.
    e.quotes["A"]["mexc:spot"].bid_qty = 5000.0
    e.quotes["A"]["mexc:spot"].ask_qty = 4000.0
    e.quotes["A"]["mexc:spot"].top_ts = time.time()

    # Novo ciclo de polling: a medição precisa SOBREVIVER.
    await e.poll_once()
    atual = e.quotes["A"]["mexc:spot"]
    assert atual.bid_qty == 5000.0, "a profundidade medida foi apagada pelo ticker"
    assert atual.ask_qty == 4000.0
    assert atual.bid == 99.9, "mas o PREÇO tem que ser o novo"


async def test_profundidade_expirada_nao_e_carregada_adiante():
    # Preservar para sempre seria pior que apagar: mostraria liquidez que já
    # sumiu como se estivesse lá.
    e = MultiVenueEngine(http_client=None)
    e.adapters = {"mexc:spot": TickerFake(MEXC_S)}
    e.venue_status = {"mexc:spot": {"ok": False, "symbols": 0, "last_ok_ts": 0.0, "error": None}}

    await e.poll_once()
    e.quotes["A"]["mexc:spot"].bid_qty = 5000.0
    e.quotes["A"]["mexc:spot"].top_ts = time.time() - 999  # muito velha

    await e.poll_once()
    assert e.quotes["A"]["mexc:spot"].bid_qty is None


def test_top_usdt_converte_contratos_em_valor():
    # Em futures a quantidade do book está em CONTRATOS. O contrato de
    # JIMOTHY na MEXC vale 100 moedas; ignorar isso erraria por 100x.
    quote = q(MEXC_F, bid=0.00611, ask=0.006112)
    quote.bid_qty = 40.0  # 40 contratos
    assert quote.top_usdt("bid", contract_size=100.0) == pytest.approx(40 * 100 * 0.00611)
    # Em spot o multiplicador é 1 e a quantidade já é a moeda base.
    spot = q(MEXC_S, bid=0.006, ask=0.006006)
    spot.bid_qty = 5000.0
    assert spot.top_usdt("bid") == pytest.approx(30.0)


def test_quantidade_nao_medida_devolve_none_e_nao_zero():
    # Exibir 0 onde não se sabe faria um book profundo parecer vazio — número
    # inventado na tela é exatamente o que este projeto não tolera.
    quote = q(MEXC_S, bid=1.0, ask=1.1)
    assert quote.bid_qty is None
    assert quote.top_usdt("bid") is None
