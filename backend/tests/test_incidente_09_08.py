"""
Regressão do incidente de 09/08/2026: a MEXC spot não tem IOC.

## O que aconteceu

O bot mandava `type=LIMIT` + `timeInForce=IOC` acreditando que o resto seria
cancelado. A MEXC spot ACEITA E IGNORA `timeInForce` -- o `orderTypes` do
exchangeInfo lista apenas LIMIT, MARKET e LIMIT_MAKER. A ordem virou GTC e
ficou viva no book.

Historico real da conta:

    17:36:11  BUY  LIMIT   tif=None  qty=1100.55  executed=1100.55  FILLED
    17:36:20  BUY  MARKET  tif=None  qty=1100.55  executed=350.64   FILLED
    (fill da LIMIT so aconteceu as 17:36:44, como MAKER)

Sequencia do estrago:
  1. LIMIT de 1100,55 fica pendurada; o bot espera 6s, nao confirma, desiste.
  2. Escalona para MARKET de 1100,55 -- mas o saldo esta travado pela LIMIT
     viva, entao so 350,64 preenchem.
  3. Bot registra a entrada com spot_qty=350,64 e REVERTE 7 dos 11 contratos
     de futures, por achar que o spot nao preencheu.
  4. 33 segundos depois a LIMIT abandonada preenche sozinha: +1100,55.

Resultado: 1451,19 comprados no spot contra 400 vendidos no futures. Sobrou
mais de mil unidades de exposicao comprada que ninguem decidiu ter, e o
usuario teve que fechar na mao.

A licao: **uma ordem nao confirmada nao e uma ordem que nao executou.**
Enquanto ela existir, ela pode preencher -- e vai preencher exatamente
quando o bot ja decidiu outra coisa.
"""
import pytest

from bot.bot_engine import ExecutionMode, PairState
from bot.execution import SlippagePolicy
from conftest import (
    FakeFuturesClient, FakeMarketClient, FakeSpotClient,
    futures_depth_payload, make_engine, spot_depth_payload,
)

SEM_ESCALONAMENTO = SlippagePolicy(
    max_slippage_pct=0.3, max_attempts=3, escalate_to_market=False, attempt_delay_s=0,
)


def books():
    return (
        FakeMarketClient(spot_depth_payload(bids=[(0.999, 1e6)], asks=[(1.00, 1e6)]), kind="spot"),
        FakeMarketClient(futures_depth_payload(bids=[(1.02, 1e6)], asks=[(1.021, 1e6)]), kind="futures"),
    )


def monta(spot, futures, policy=None, **kw):
    ms, mf = books()
    return make_engine(
        contract_size=1, mode=ExecutionMode.LIVE,
        spot_client=spot, futures_client=futures,
        market_spot=ms, market_futures=mf,
        slippage_policy=policy or SlippagePolicy(max_slippage_pct=0.3, attempt_delay_s=0),
        spot_limit_wait_s=0,
        **kw,
    )


async def entrada(engine):
    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )


async def test_nenhuma_ordem_limite_de_spot_fica_viva():
    """
    O nucleo da correcao. Toda ordem LIMITE precisa ser cancelada antes de a
    funcao retornar -- em qualquer desfecho, inclusive sucesso. Uma ordem viva
    e uma decisao que o bot ainda nao sabe que vai tomar.
    """
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta(spot, futures)

    await entrada(engine)

    limites = [o for o in spot.orders if o["type"] == "LIMIT"]
    assert limites, "a perna spot precisa usar LIMITE (teto de preco)"
    assert len(spot.cancelamentos) >= len(limites), (
        "toda ordem LIMITE precisa ter sido cancelada: a MEXC spot nao tem IOC, "
        "entao o que nao for cancelado fica no book e preenche depois"
    )


async def test_nao_envia_timeinforce_ioc_que_a_mexc_ignora():
    """
    Mandar `timeInForce=IOC` era pior que inutil: criava a CRENCA de que a
    ordem se auto-cancelaria. A MEXC aceita o parametro, ignora, e devolve
    `timeInForce: null`.
    """
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = monta(spot, futures)

    await entrada(engine)

    for o in spot.orders:
        assert "timeInForce" not in o, (
            "a MEXC spot nao suporta timeInForce; enviar isso mascara o fato "
            "de a ordem ser GTC"
        )


async def test_quantidade_lida_e_a_de_depois_do_cancelamento():
    """
    A quantidade preenchida so e definitiva quando a ordem nao pode mais
    preencher. Ler antes de cancelar devolve um numero que ainda vai crescer
    -- foi exatamente essa leitura precoce que fez o bot achar que tinha
    350,64 quando o total viria a ser 1451,19.
    """
    # A resposta do POST mente (executedQty=0); so o estado final tem a verdade.
    spot = FakeSpotClient(immediate_zero=True)
    futures = FakeFuturesClient()
    engine, _ = monta(spot, futures)

    await entrada(engine)

    rt = engine.runtimes["JIMOTHY"]
    assert rt.state == PairState.OPEN
    # Uma unica ordem: o valor final foi lido corretamente na primeira, sem
    # reenviar por achar que nao preencheu.
    assert len([o for o in spot.orders if o["type"] == "LIMIT"]) == 1
    assert rt.entry_spot_qty > 0


async def test_ordem_de_futures_nao_confirmada_e_cancelada_e_recontabilizada():
    """
    O mesmo principio na perna de futures: se o fill nao confirmar, a ordem e
    CANCELADA e relida. O que ela tiver preenchido entra na conta, em vez de
    virar posicao fantasma que o bot nao sabe que tem.
    """
    # state=2 = ainda aberta: nenhum estado terminal chega, entao o executor
    # do venue estoura a espera. Mas a ordem preencheu 5 contratos de verdade,
    # e so o cancelamento seguido de releitura revela isso.
    futures = FakeFuturesClient(fill_plan=[{"dealVol": 5, "dealAvgPrice": 1.017, "state": 2}])
    spot = FakeSpotClient()
    engine, _ = monta(spot, futures, policy=SEM_ESCALONAMENTO)

    await entrada(engine)

    assert futures.cancelamentos, "ordem nao confirmada precisa ser cancelada"


async def test_nenhuma_tentativa_repede_o_total_depois_de_preenchimento_parcial():
    """
    A propriedade que 09/08 violou.

    Somar as quantidades PEDIDAS entre retentativas passa do alvo por
    construcao -- cada retentativa repede o que faltou, e isso e correto. O
    que nao pode acontecer e uma tentativa pedir o TOTAL depois de ja ter
    havido preenchimento.

    Foi isso que aconteceu: a LIMIT de 1100,55 preencheu inteira, mas o bot
    nao contabilizou (ela ainda estava viva quando ele desistiu), e o
    escalonamento pediu 1100,55 de novo -- o total, nao o restante. Resultado:
    1451,19 comprados para um alvo de 1100,55.
    """
    futures = FakeFuturesClient()
    # Primeira tentativa preenche parte; as seguintes, nada.
    spot = FakeSpotClient(fill_plan=[
        {"executedQty": 40, "cummulativeQuoteQty": 40 * 1.003, "status": "CANCELED"},
        {"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"},
        {"executedQty": 0, "cummulativeQuoteQty": 0, "status": "CANCELED"},
    ])
    engine, _ = monta(spot, futures, policy=SEM_ESCALONAMENTO)

    await entrada(engine)

    alvo = futures.orders[0]["vol"] / (1 - engine.fees_for("JIMOTHY").spot_taker_pct / 100)
    pedidos = [o["quantity"] for o in spot.orders]

    # Nenhuma tentativa isolada pode pedir mais que o alvo.
    for q in pedidos:
        assert q <= alvo * 1.001, f"pediu {q:.2f} para um alvo de {alvo:.2f}"

    # E depois do preenchimento parcial de 40, as seguintes pedem SO o
    # restante -- nunca o total de novo.
    assert pedidos[0] == pytest.approx(alvo, rel=1e-3)
    for q in pedidos[1:]:
        assert q == pytest.approx(alvo - 40, rel=1e-2), (
            f"pediu {q:.2f} depois de 40 ja preenchidos; deveria pedir {alvo - 40:.2f}"
        )


def test_cliente_spot_nao_expoe_mais_um_ioc_que_nao_existe():
    from bot.mexc_spot_client import MexcSpotClient
    # O metodo antigo prometia um comportamento que a exchange nao tem. Manter
    # o nome disponivel convidaria a reintroducao do bug.
    assert not hasattr(MexcSpotClient, "new_order_limit_ioc")
    assert hasattr(MexcSpotClient, "limit_then_cancel")
