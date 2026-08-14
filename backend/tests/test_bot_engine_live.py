"""
Testes da execução real (modo LIVE) do motor do bot.

Verificam o que efetivamente vai para a MEXC: tipo de ordem, direção do
preço-limite, casamento das pernas e o tratamento dos desfechos torcidos que
já custaram dinheiro nesta conta (fill não refletido na resposta do POST,
ordem IOC terminando CANCELADA com preenchimento parcial dentro).
"""
import pytest

from bot.bot_engine import ExecutionMode, PairState
from bot.execution import SlippagePolicy
from conftest import (
    FakeFuturesClient, FakeMarketClient, FakeSpotClient,
    futures_depth_payload, make_engine, spot_depth_payload,
)

# ORDER_TYPE da MEXC: 3 = IOC (executa até o limite e cancela o resto),
# 5 = mercado (sem teto de preço).
IOC = 3
MERCADO = 5

SEM_ESCALONAMENTO = SlippagePolicy(
    max_slippage_pct=0.3, max_attempts=3, escalate_to_market=False, attempt_delay_s=0,
)


def books_bons():
    return (
        FakeMarketClient(spot_depth_payload(bids=[(0.999, 1e6)], asks=[(1.00, 1e6)])),
        FakeMarketClient(futures_depth_payload(bids=[(1.02, 1e6)], asks=[(1.021, 1e6)])),
    )


def monta_live(spot_client, futures_client, market=None, policy=None, **kw):
    ms, mf = market or books_bons()
    return make_engine(
        contract_size=1, mode=ExecutionMode.LIVE,
        spot_client=spot_client, futures_client=futures_client,
        market_spot=ms, market_futures=mf,
        slippage_policy=policy or SlippagePolicy(max_slippage_pct=0.3, attempt_delay_s=0),
        **kw,
    )


async def dispara_entrada(engine):
    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )


def abre_posicao(engine, spot_qty=100.0, futures_vol=100):
    rt = engine.runtimes["JIMOTHY"]
    rt.state = PairState.OPEN
    rt.entry_spread_pct = 2.0
    rt.entry_spot_price = 1.00
    rt.entry_futures_price = 1.02
    rt.entry_spot_qty = spot_qty
    rt.entry_futures_vol = futures_vol
    rt.entry_notional_usdt = futures_vol * 1.02
    return rt


# ---------------------------------------------------------------------------
# Entrada: tipo de ordem e direção do limite
# ---------------------------------------------------------------------------

async def test_entrada_usa_ioc_com_limite_e_nao_ordem_a_mercado():
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta_live(spot, futures)

    await dispara_entrada(engine)

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN

    ordem_futures = futures.orders[0]
    assert ordem_futures["type"] == IOC, "a perna Futures não pode mais ir a mercado sem teto"
    assert ordem_futures["side"] == 3  # SIDE_OPEN_SHORT

    ordem_spot = spot.orders[0]
    assert ordem_spot["type"] == "LIMIT", "a perna Spot precisa ter teto de preço"
    assert ordem_spot["side"] == "BUY"


async def test_limites_apontam_para_a_direcao_que_protege_o_preco():
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta_live(spot, futures)

    await dispara_entrada(engine)

    # Vender futures: o limite é um PISO abaixo do VWAP (1,02), nunca acima.
    piso_futures = futures.orders[0]["price"]
    assert piso_futures < 1.02
    assert piso_futures == pytest.approx(1.02 * 0.997, rel=1e-4)

    # Comprar spot: o limite é um TETO acima do VWAP (1,00), nunca abaixo.
    teto_spot = spot.orders[0]["price"]
    assert teto_spot > 1.00
    assert teto_spot == pytest.approx(1.00 * 1.003, rel=1e-4)


async def test_perna_spot_e_dimensionada_pela_quantidade_da_ancora():
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta_live(spot, futures)

    await dispara_entrada(engine)

    vol_executado = futures.orders[0]["vol"]
    qty_spot = spot.orders[0]["quantity"]
    taxa = engine.fees_for("JIMOTHY").spot_taker_pct / 100

    # A compra é inflada pela taxa (cobrada na moeda base) para que o saldo
    # líquido resultante case com a exposição vendida no futures.
    assert qty_spot * (1 - taxa) == pytest.approx(vol_executado, rel=1e-3)


async def test_entrada_bem_sucedida_nao_dispara_reversao():
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta_live(spot, futures)

    await dispara_entrada(engine)

    # Uma única ordem de futures: a de abertura. O resíduo de arredondamento
    # da quantidade de spot (~0,01% da posição) NÃO deve provocar a reversão
    # de um contrato inteiro — isso pagaria duas taxas para criar um
    # descasamento maior na direção oposta.
    assert len(futures.orders) == 1


# ---------------------------------------------------------------------------
# Entrada: desfechos ruins
# ---------------------------------------------------------------------------

async def test_futures_sem_preenchimento_nao_deixa_exposicao_nenhuma():
    futures = FakeFuturesClient(fill_plan=[{"dealVol": 0, "dealAvgPrice": 0, "state": 4}])
    spot = FakeSpotClient()
    engine, storage = monta_live(spot, futures, policy=SEM_ESCALONAMENTO)

    await dispara_entrada(engine)

    assert engine.runtimes["JIMOTHY"].state == PairState.PAUSED_ERROR
    assert spot.orders == [], "não pode comprar spot sem ter conseguido vender futures"
    assert storage.events_of("entry_futures_not_filled")


async def test_spot_sem_preenchimento_reverte_a_ancora_inteira():
    futures = FakeFuturesClient()
    spot = FakeSpotClient(fill_plan=[{"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"}])
    engine, storage = monta_live(spot, futures, policy=SEM_ESCALONAMENTO)

    await dispara_entrada(engine)

    assert engine.runtimes["JIMOTHY"].state == PairState.PAUSED_ERROR
    assert len(futures.orders) == 2
    reversao = futures.orders[1]
    assert reversao["side"] == 2  # SIDE_CLOSE_SHORT
    assert reversao["reduceOnly"] is True
    assert reversao["vol"] == pytest.approx(futures.orders[0]["vol"])
    assert storage.events_of("entry_failed_spot_order_reverted")


async def test_spot_parcial_reverte_apenas_a_parte_descoberta():
    """
    O caso que passava DIREITO antes: a perna Spot preenche uma fração do
    pedido e a diferença fica como exposição direcional silenciosa. Agora a
    parte descoberta é revertida imediatamente.
    """
    futures = FakeFuturesClient()
    spot = FakeSpotClient(fill_plan=[
        {"executedQty": 40, "cummulativeQuoteQty": 40 * 1.003, "status": "CANCELED"},
        {"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"},
        {"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"},
    ])
    engine, _ = monta_live(spot, futures, policy=SEM_ESCALONAMENTO)

    await dispara_entrada(engine)

    rt = engine.runtimes["JIMOTHY"]
    assert rt.state == PairState.OPEN

    vol_aberto = futures.orders[0]["vol"]
    reversao = futures.orders[1]
    assert reversao["side"] == 2
    assert reversao["reduceOnly"] is True
    # Reverte só o descoberto (~58 de 98), não a posição inteira.
    assert reversao["vol"] == pytest.approx(vol_aberto - 40, abs=1)
    assert rt.entry_futures_vol == pytest.approx(40, abs=1)


async def test_escalonamento_para_mercado_quando_o_teto_nao_comporta():
    futures = FakeFuturesClient()
    spot = FakeSpotClient(fill_plan=[
        {"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"},
        {"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"},
        {"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"},
        {"executedQty": 98.04, "cummulativeQuoteQty": 98.04 * 1.01, "status": "FILLED"},
    ])
    engine, _ = monta_live(spot, futures)  # escalonamento LIGADO (padrão)

    await dispara_entrada(engine)

    # Três tentativas IOC e, no fim, uma ordem a mercado com o resíduo.
    assert [o["type"] for o in spot.orders] == ["LIMIT"] * 3 + ["MARKET"]
    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN


# ---------------------------------------------------------------------------
# Confirmação de fill: os desfechos torcidos da API
# ---------------------------------------------------------------------------

async def test_fill_nao_refletido_na_resposta_do_post_e_confirmado_por_consulta():
    """
    Regressão do bug 6 do CLAUDE.md: a MEXC pode devolver `executedQty=0`
    numa ordem que executou de verdade. Confiar nisso fez o bot reenviar a
    ordem e vender duas vezes, confirmado no histórico real de ordens.
    """
    futures = FakeFuturesClient()
    spot = FakeSpotClient(immediate_zero=True)  # POST mente, o status conta a verdade
    engine, _ = monta_live(spot, futures)

    await dispara_entrada(engine)

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN
    # Uma única ordem de spot: o preenchimento foi reconhecido pela consulta
    # de status, sem reenvio.
    assert len(spot.orders) == 1


async def test_ioc_parcial_terminando_cancelada_conta_como_preenchimento():
    """
    Comportamento novo e obrigatório desde a adoção de IOC: uma ordem IOC
    parcialmente preenchida tem o restante CANCELADO e termina com status
    "CANCELED" — com dinheiro que realmente mudou de mãos dentro. Tratar
    isso como "não preencheu" faria o bot ignorar uma compra que existe.
    """
    futures = FakeFuturesClient()
    spot = FakeSpotClient(
        immediate_zero=True,
        fill_plan=[
            {"executedQty": 50, "cummulativeQuoteQty": 50 * 1.003, "status": "CANCELED"},
            {"executedQty": 48.04, "cummulativeQuoteQty": 48.04 * 1.003, "status": "CANCELED"},
        ],
    )
    engine, _ = monta_live(spot, futures, policy=SEM_ESCALONAMENTO)

    await dispara_entrada(engine)

    rt = engine.runtimes["JIMOTHY"]
    assert rt.state == PairState.OPEN
    assert rt.entry_spot_qty == pytest.approx(98.04, abs=0.1)


async def test_futures_ioc_parcial_no_estado_cancelado_conta_como_preenchimento():
    futures = FakeFuturesClient(fill_plan=[
        {"dealVol": 60, "dealAvgPrice": 1.017, "state": 4},   # cancelada, mas com fill
        {"dealVol": 38, "dealAvgPrice": 1.017, "state": 4},
    ])
    spot = FakeSpotClient()
    engine, _ = monta_live(spot, futures, policy=SEM_ESCALONAMENTO)

    await dispara_entrada(engine)

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN
    assert engine.runtimes["JIMOTHY"].entry_futures_vol == pytest.approx(98, abs=1)


# ---------------------------------------------------------------------------
# Saída
# ---------------------------------------------------------------------------

def books_de_saida():
    return (
        FakeMarketClient(spot_depth_payload(bids=[(1.00, 1e6)], asks=[(1.002, 1e6)])),
        FakeMarketClient(futures_depth_payload(bids=[(0.999, 1e6)], asks=[(1.001, 1e6)])),
    )


async def dispara_saida(engine):
    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.001, spread_pct=0.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=1.00, futures_ask=1.001,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )


async def test_saida_usa_ioc_com_reduce_only_e_limites_na_direcao_certa():
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta_live(spot, futures, market=books_de_saida(), exit_spread_pct=0.25)
    abre_posicao(engine)

    await dispara_saida(engine)

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE

    ordem_futures = futures.orders[0]
    assert ordem_futures["type"] == IOC
    assert ordem_futures["side"] == 2          # SIDE_CLOSE_SHORT
    assert ordem_futures["reduceOnly"] is True
    # Recomprar futures é COMPRA: o limite é um teto acima do VWAP (1,001).
    assert ordem_futures["price"] > 1.001

    ordem_spot = spot.orders[0]
    assert ordem_spot["type"] == "LIMIT"
    assert ordem_spot["side"] == "SELL"
    # Vender spot: o limite é um piso abaixo do VWAP (1,00).
    assert ordem_spot["price"] < 1.00
    assert ordem_spot["price"] == pytest.approx(1.00 * 0.997, rel=1e-4)


async def test_saida_limita_a_venda_ao_saldo_real_disponivel():
    """
    Regressão do erro `Insufficient position`: a MEXC desconta a taxa na
    própria moeda comprada, então o saldo vendável é menor que o comprado.
    """
    spot = FakeSpotClient(balance=99.5)
    futures = FakeFuturesClient()
    engine, _ = monta_live(spot, futures, market=books_de_saida(), exit_spread_pct=0.25)
    abre_posicao(engine, spot_qty=100.0)

    await dispara_saida(engine)

    assert spot.orders[0]["quantity"] == pytest.approx(99.5)


async def test_saida_com_perna_spot_falhando_pausa_o_par_e_preserva_a_posicao():
    spot = FakeSpotClient(fill_plan=[{"executedQty": 0, "cummulativeQuoteQty": 0, "status": "REJECTED"}])
    futures = FakeFuturesClient()
    engine, storage = monta_live(
        spot, futures, market=books_de_saida(), exit_spread_pct=0.25, policy=SEM_ESCALONAMENTO,
    )
    abre_posicao(engine)

    await dispara_saida(engine)

    rt = engine.runtimes["JIMOTHY"]
    assert rt.state == PairState.PAUSED_ERROR
    # O resíduo preservado é o que AINDA está aberto: o spot não vendeu nada,
    # então continuam as 100 unidades para vender manualmente.
    assert rt.entry_spot_qty == pytest.approx(100.0)
    # O tratamento é unificado desde a correção de 05/08: qualquer perna
    # pendente — spot ou futures — cai no mesmo evento simétrico.
    evento = storage.events_of("exit_incomplete_legs")
    assert evento
    assert evento[0]["detail"]["spot_closed_qty"] == 0


async def test_evento_de_saida_registra_a_decomposicao_completa_do_slippage():
    """
    Sem esses campos era impossível saber POR QUE uma operação saiu pior que
    o esperado — o log antigo guardava o spread de tela e o realizado, sem
    nada entre os dois para atribuir a diferença.
    """
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, storage = monta_live(spot, futures, market=books_de_saida(), exit_spread_pct=0.25)
    abre_posicao(engine)

    await dispara_saida(engine)

    detalhe = storage.events_of("exit_live")[0]["detail"]
    for campo in (
        "exit_spread_pct",            # o que executou
        "exit_spread_signal_pct",     # o que a tela mostrava (topo do book)
        "exit_spread_executable_pct",  # o que a profundidade projetou
        "depth_cost_pct",             # mentira do topo do book, em pp
        "execution_slippage_pct",     # projetado -> fill, em pp
        "fee_cost_usdt_exit",
        "net_pct",
        "spot_limit_price", "futures_limit_price",
        "spot_escalated_to_market", "futures_escalated_to_market",
    ):
        assert campo in detalhe, f"campo de diagnóstico ausente: {campo}"


# ---------------------------------------------------------------------------
# REGRESSÃO: saída incompleta declarada como concluída (05/08/2026)
# ---------------------------------------------------------------------------

class FuturesQueNaoFecha(FakeFuturesClient):
    """
    Abre normalmente, mas NENHUMA ordem de fechamento preenche — foi o que
    aconteceu em 05/08/2026 (3 tentativas IOC + escalonamento a mercado, todas
    sem preenchimento). `get_open_positions` continua reportando a posição,
    porque na MEXC ela realmente continuava lá.
    """

    async def submit_order(self, symbol, side, vol, order_type=5, price=None,
                           leverage=1, open_type=1, external_oid=None, reduce_only=False):
        resp = await super().submit_order(
            symbol, side, vol, order_type, price, leverage, open_type, external_oid, reduce_only,
        )
        if side == 2:  # fechar short: não preenche nada
            self._status_by_id[resp["data"]] = {"dealVol": 0, "dealAvgPrice": 0, "state": 4}
        return resp

    async def get_open_positions(self, symbol=None):
        return {"success": True, "data": [
            {"symbol": symbol, "positionType": 2, "holdVol": 8, "holdAvgPrice": 0.00611},
        ]}


async def test_saida_com_futures_sem_fechar_nao_pode_ser_declarada_concluida():
    """
    Em 05/08/2026 a perna Spot vendeu, a de Futures fechou ZERO contratos, e o
    bot mesmo assim registrou "Saída (real) +1,08%", limpou a posição e
    reportou resultado líquido positivo. Restou um SHORT DESCOBERTO de 800
    JIMOTHY que o usuário teve que fechar na mão 6 minutos depois — com o bot
    livre para abrir outra posição em cima.

    A verificação precisa ser SIMÉTRICA: qualquer perna que não fechou impede
    a saída de ser considerada concluída.
    """
    spot, futures = FakeSpotClient(), FuturesQueNaoFecha()
    engine, storage = monta_live(
        spot, futures, market=books_de_saida(), exit_spread_pct=0.25, policy=SEM_ESCALONAMENTO,
    )
    abre_posicao(engine, spot_qty=800.4, futures_vol=8)

    await dispara_saida(engine)

    rt = engine.runtimes["JIMOTHY"]
    assert rt.state == PairState.PAUSED_ERROR, "posição meio aberta não pode virar IDLE"
    assert storage.events_of("exit_live") == [], "não pode registrar saída concluída"

    evento = storage.events_of("exit_incomplete_legs")
    assert evento, "a saída incompleta precisa ficar registrada no histórico"
    d = evento[0]["detail"]
    assert d["futures_closed_vol"] == 0
    assert d["futures_requested_vol"] == 8
    # A verdade da exchange entra na mensagem, não a crença do bot.
    assert "POSIÇÃO ABERTA" in d["exchange_position"]
    assert "8 contratos" in d["exchange_position"]


async def test_saida_incompleta_nao_fabrica_preco_realizado():
    """
    O fallback `futures_leg.avg_price or executable.futures.avg_price` fazia
    uma perna que não executou parecer executada: o evento de 05/08 gravou
    `exit_futures_price: 0.004216` e `exit_spread_pct: +1.08%` para uma ordem
    que nunca preencheu. Preço realizado só existe se houve fill.
    """
    spot, futures = FakeSpotClient(), FuturesQueNaoFecha()
    engine, storage = monta_live(
        spot, futures, market=books_de_saida(), exit_spread_pct=0.25, policy=SEM_ESCALONAMENTO,
    )
    abre_posicao(engine, spot_qty=800.4, futures_vol=8)

    await dispara_saida(engine)

    d = storage.events_of("exit_incomplete_legs")[0]["detail"]
    assert d["exit_futures_price"] is None, "sem fill não há preço realizado"
    assert d["pnl_futures_usdt"] is None, "sem fill não há PnL de futures"


async def test_saida_incompleta_preserva_o_residuo_para_o_kill_switch():
    """
    A posição não pode ser apagada: são esses números que o kill switch usa
    para fechar o que sobrou. E devem refletir só o RESÍDUO — o spot já foi
    vendido, então só o futures continua aberto.
    """
    spot, futures = FakeSpotClient(), FuturesQueNaoFecha()
    engine, storage = monta_live(
        spot, futures, market=books_de_saida(), exit_spread_pct=0.25, policy=SEM_ESCALONAMENTO,
    )
    abre_posicao(engine, spot_qty=800.4, futures_vol=8)

    await dispara_saida(engine)

    rt = engine.runtimes["JIMOTHY"]
    assert rt.entry_futures_vol == pytest.approx(8), "o short inteiro continua aberto"
    assert rt.entry_spot_qty == pytest.approx(0, abs=1), "o spot já foi vendido"
    assert "JIMOTHY" in storage.positions, "a posição não pode sumir do banco"


async def test_kill_switch_fecha_par_pausado_com_posicao_residual():
    """
    O kill switch só olhava PairState.OPEN — justamente o estado em que uma
    posição problemática NÃO está. Um par em PAUSED_ERROR por saída incompleta
    era ignorado pelo botão de emergência, exatamente quando ele mais importa.
    """
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta_live(spot, futures, market=books_de_saida())

    rt = engine.runtimes["JIMOTHY"]
    rt.state = PairState.PAUSED_ERROR
    rt.entry_futures_vol = 8
    rt.entry_spot_qty = 0.0
    rt.entry_futures_price = 0.00611
    rt.entry_spot_price = 0.006006

    await engine.kill_switch()

    fechamentos = [o for o in futures.orders if o["side"] == 2 and o["reduceOnly"]]
    assert fechamentos, "o kill switch precisa fechar o resíduo de um par pausado"
    assert fechamentos[0]["vol"] == pytest.approx(8)


async def test_saida_bem_sucedida_registra_erros_das_tentativas():
    """
    O buffer de log em memória é curto e some no reinício — foi assim que o
    motivo real da falha de 05/08 se perdeu. Os erros por tentativa passam a
    ficar no histórico persistente também na saída bem-sucedida.
    """
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, storage = monta_live(spot, futures, market=books_de_saida(), exit_spread_pct=0.25)
    abre_posicao(engine)

    await dispara_saida(engine)

    d = storage.events_of("exit_live")[0]["detail"]
    assert "spot_errors" in d and "futures_errors" in d
    assert "spot_closed_qty" in d and "futures_closed_vol" in d
