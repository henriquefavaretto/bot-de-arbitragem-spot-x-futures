"""
Testes do cálculo de preço executável por profundidade (bot/depth.py).

O teste central deste arquivo é `test_caso_jimothy_...`: ele reproduz, com
os números reais gravados no banco, a saída de 03/08/2026 que mostrou 0,23%
na tela e executou a 1,44%. É a regressão que justifica todo o módulo — se
ele voltar a passar com o cálculo antigo (topo do book), o bug voltou.
"""
import pytest

from bot.depth import (
    BookLevel, OrderBook, entry_executable_spread, exit_executable_spread,
    parse_futures_depth, parse_spot_depth, walk_by_qty, walk_by_quote,
)


# ---------------------------------------------------------------------------
# Caminhada básica pelo book
# ---------------------------------------------------------------------------

def test_quantidade_cabe_na_primeira_camada_usa_o_preco_do_topo():
    levels = [BookLevel(1.00, 100), BookLevel(1.10, 100)]
    result = walk_by_qty(levels, 50)

    assert result.avg_price == pytest.approx(1.00)
    assert result.top_price == pytest.approx(1.00)
    assert result.levels_used == 1
    assert result.complete is True
    # Quando tudo cabe no topo, não há custo de profundidade: é o único caso
    # em que o número da tela antiga estava certo.
    assert result.depth_slippage_pct == pytest.approx(0.0)


def test_quantidade_atravessa_camadas_e_o_vwap_piora():
    levels = [BookLevel(1.00, 10), BookLevel(1.10, 90)]
    result = walk_by_qty(levels, 100)

    # 10 * 1.00 + 90 * 1.10 = 109 sobre 100 unidades
    assert result.avg_price == pytest.approx(1.09)
    assert result.notional == pytest.approx(109.0)
    assert result.levels_used == 2
    assert result.complete is True
    # O topo prometia 1.00; a execução sai a 1.09 -> 9% de custo escondido.
    assert result.depth_slippage_pct == pytest.approx(9.0)


def test_profundidade_insuficiente_marca_incompleto_em_vez_de_fingir_que_coube():
    levels = [BookLevel(1.00, 10), BookLevel(1.10, 20)]
    result = walk_by_qty(levels, 100)

    assert result.complete is False
    assert result.filled_qty == pytest.approx(30)
    assert result.requested_qty == pytest.approx(100)


def test_camadas_invalidas_sao_ignoradas_sem_contaminar_o_vwap():
    # Preço/quantidade zerados ou negativos aparecem em payloads corrompidos.
    # Incluí-los na média distorceria o preço sem nenhum erro visível.
    levels = [BookLevel(1.00, 10), BookLevel(0.0, 500), BookLevel(1.10, 90)]
    result = walk_by_qty(levels, 100)
    assert result.avg_price == pytest.approx(1.09)


def test_compra_por_valor_em_usdt_devolve_quantidade_e_valor_gasto():
    asks = [BookLevel(2.00, 10), BookLevel(4.00, 10)]
    # 10 unidades a 2.00 = 20 USDT; sobram 20 USDT que compram 5 a 4.00.
    result = walk_by_quote(asks, 40.0)

    assert result.notional == pytest.approx(40.0)
    assert result.filled_qty == pytest.approx(15.0)
    assert result.avg_price == pytest.approx(40.0 / 15.0)
    assert result.complete is True


def test_compra_por_valor_marca_incompleto_quando_o_book_acaba():
    asks = [BookLevel(2.00, 10)]
    result = walk_by_quote(asks, 100.0)
    assert result.complete is False
    assert result.notional == pytest.approx(20.0)


def test_book_vazio_ou_quantidade_zero_devolve_none():
    assert walk_by_qty([], 10) is None
    assert walk_by_qty([BookLevel(1.0, 1)], 0) is None
    assert walk_by_quote([], 10) is None


# ---------------------------------------------------------------------------
# Sanidade do book
# ---------------------------------------------------------------------------

def test_book_cruzado_e_detectado_e_marcado_como_inutilizavel():
    # bid >= ask é fisicamente impossível num book consistente: sinal de
    # payload corrompido ou de dois lados vindos de momentos diferentes.
    book = OrderBook("X", bids=[BookLevel(1.10, 10)], asks=[BookLevel(1.00, 10)])
    assert book.is_crossed is True
    assert book.is_usable is False


def test_book_normal_e_utilizavel():
    book = OrderBook("X", bids=[BookLevel(1.00, 10)], asks=[BookLevel(1.01, 10)])
    assert book.is_crossed is False
    assert book.is_usable is True
    assert book.best_bid == pytest.approx(1.00)
    assert book.best_ask == pytest.approx(1.01)


def test_book_com_um_lado_vazio_nao_e_utilizavel():
    assert OrderBook("X", bids=[BookLevel(1.0, 1)], asks=[]).is_usable is False


# ---------------------------------------------------------------------------
# Parsing dos payloads
# ---------------------------------------------------------------------------

def test_parse_spot_converte_strings_para_float():
    payload = {"bids": [["0.006890", "50"]], "asks": [["0.006900", "40"]]}
    book = parse_spot_depth(payload, "JIMOTHYUSDT")
    assert book.bids[0].price == pytest.approx(0.006890)
    assert book.asks[0].qty == pytest.approx(40.0)


def test_parse_futures_ignora_o_terceiro_campo_da_camada():
    payload = {"success": True, "data": {
        "bids": [[0.00689, 120, 3]], "asks": [[0.006906, 90, 2]],
    }}
    book = parse_futures_depth(payload, "JIMOTHY_USDT")
    assert book.bids[0].qty == pytest.approx(120)
    assert book.asks[0].price == pytest.approx(0.006906)


def test_parse_futures_rejeita_timestamp_remoto_implausivel():
    # Um relógio local dessincronizado (ou um ts corrompido) faria o book
    # parecer eternamente fresco, derrubando a proteção de staleness
    # justamente quando ela mais importa.
    payload = {"success": True, "data": {
        "bids": [[1.0, 10]], "asks": [[1.1, 10]],
        "timestamp": 1_000_000,  # 1970, absurdamente no passado
    }}
    book = parse_futures_depth(payload, "X_USDT")
    assert book.age_s() < 5  # caiu no relógio local, não no ts remoto


def test_parse_rejeita_payload_malformado():
    assert parse_spot_depth({"bids": [["abc", "1"]], "asks": []}, "X") is None
    assert parse_futures_depth({"success": False}, "X") is None
    assert parse_futures_depth({"success": True, "data": None}, "X") is None


# ---------------------------------------------------------------------------
# Spreads executáveis
# ---------------------------------------------------------------------------

def test_spread_de_entrada_executavel_e_pior_que_o_de_topo_de_book():
    spot = OrderBook("S", bids=[BookLevel(0.99, 1000)],
                     asks=[BookLevel(1.00, 10), BookLevel(1.10, 1000)])
    futures = OrderBook("F", bids=[BookLevel(1.20, 10), BookLevel(1.05, 1000)],
                        asks=[BookLevel(1.25, 1000)])

    # Comprar 109 USDT de spot e vender 100 contratos de futures.
    result = entry_executable_spread(spot, futures, notional_usdt=109.0, futures_vol=100)

    assert result.complete is True
    # Topo do book: (1.20 - 1.00) / 1.00 = 20%
    assert result.screen_spread_pct == pytest.approx(20.0)
    # Executável: spot VWAP 1.09, futures VWAP 1.065 -> negativo
    assert result.spot.avg_price == pytest.approx(1.09)
    assert result.futures.avg_price == pytest.approx(1.065)
    assert result.spread_pct == pytest.approx((1.065 - 1.09) / 1.09 * 100)
    assert result.spread_pct < 0
    # O topo do book prometia 20% numa operação que na verdade perde dinheiro.
    assert result.depth_cost_pct > 22


def test_spread_de_saida_usa_os_lados_opostos_do_book():
    # Regressão do bug mais caro do projeto (CLAUDE.md, "DOIS spreads"):
    # a saída vende no BID do spot e compra no ASK do futures.
    spot = OrderBook("S", bids=[BookLevel(1.00, 1000)], asks=[BookLevel(1.50, 1000)])
    futures = OrderBook("F", bids=[BookLevel(0.90, 1000)], asks=[BookLevel(1.02, 1000)])

    result = exit_executable_spread(spot, futures, spot_qty=100, futures_vol=100)

    # Usa spot_bid=1.00 e futures_ask=1.02, NÃO spot_ask nem futures_bid.
    assert result.spread_pct == pytest.approx(2.0)


def test_entrada_com_book_inutilizavel_devolve_none():
    crossed = OrderBook("S", bids=[BookLevel(1.1, 10)], asks=[BookLevel(1.0, 10)])
    ok = OrderBook("F", bids=[BookLevel(1.0, 10)], asks=[BookLevel(1.1, 10)])
    assert entry_executable_spread(crossed, ok, 10, 10) is None
    assert exit_executable_spread(crossed, ok, 10, 10) is None


# ---------------------------------------------------------------------------
# REGRESSÃO: o caso real de 03/08/2026
# ---------------------------------------------------------------------------

def test_caso_jimothy_saida_topo_do_book_mentia_em_mais_de_um_ponto_percentual():
    """
    Reproduz a saída real registrada em `bot_trade_log` em 03/08/2026 18:23:31:

        exit_spread_signal_pct (tela, topo do book):  +0,2323%
        exit_spread_pct (realizado, dos fills):       +1,4395%
        exit_spot_price:   0,006808
        exit_futures_price: 0,006906
        pnl_total_usdt:    -0,0010

    O topo do book do spot estava em 0,006890 com pouquíssima quantidade; a
    posição de 608,48 unidades varreu até 0,006800. O bot decidiu com o
    primeiro número e executou no segundo.

    Este teste exige que o cálculo por profundidade enxergue a diferença
    ANTES de mandar a ordem. Com o cálculo antigo (só topo de book), o valor
    encontrado seria ~0,23% e a asserção falharia.
    """
    spot_qty = 608.48
    futures_vol = 6  # contratos de 100 unidades = 600 JIMOTHY

    spot = OrderBook(
        "JIMOTHYUSDT",
        # Topo bonito, mas com quase nada disponível — o padrão de memecoin
        # ilíquida que criou a ilusão de spread favorável.
        bids=[BookLevel(0.006890, 50), BookLevel(0.006800, 10_000)],
        asks=[BookLevel(0.006950, 10_000)],
    )
    futures = OrderBook(
        "JIMOTHY_USDT",
        bids=[BookLevel(0.006880, 1_000)],
        asks=[BookLevel(0.006906, 1_000)],
    )

    result = exit_executable_spread(spot, futures, spot_qty=spot_qty, futures_vol=futures_vol)

    assert result.complete is True

    # O que a tela mostrava: (0.006906 - 0.006890) / 0.006890 = 0,232%
    assert result.screen_spread_pct == pytest.approx(0.2323, abs=0.005)

    # O que era executável de verdade para esta posição: ~1,44%
    assert result.spread_pct == pytest.approx(1.44, abs=0.05)
    assert result.spot.avg_price == pytest.approx(0.006808, abs=0.000002)

    # A mentira do topo do book, em pontos percentuais. Foi exatamente isso
    # que consumiu o lucro da operação.
    assert result.depth_cost_pct > 1.2

    # E a consequência prática: com um alvo de saída de 0,25%, o topo do book
    # autorizava a saída e o executável não. É essa recusa que o bot ganhou.
    alvo_de_saida = 0.25
    assert result.screen_spread_pct <= alvo_de_saida, "topo do book autorizaria (era o bug)"
    assert result.spread_pct > alvo_de_saida, "executável recusa (é a correção)"
