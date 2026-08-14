"""
Testes da decisão do bot: a confirmação por profundidade e as travas.

Estes testes descrevem o comportamento novo mais importante: o spread de
topo de book virou um GATILHO, e quem autoriza a ordem é o spread executável
para o tamanho real da posição. Cada recusa aqui é uma operação que a versão
anterior teria feito com o preço errado.
"""
import asyncio

import pytest

from bot.bot_engine import ExecutionMode, PairState
from conftest import (
    FakeMarketClient, futures_depth_payload, make_engine, spot_depth_payload,
)


def books_bons():
    """Book profundo dos dois lados: spot a 1,00 e futures a 1,02 -> 2% executável."""
    spot = spot_depth_payload(bids=[(0.999, 100_000)], asks=[(1.00, 100_000)])
    futures = futures_depth_payload(bids=[(1.02, 100_000)], asks=[(1.021, 100_000)])
    return FakeMarketClient(spot), FakeMarketClient(futures)


def books_com_topo_enganoso():
    """
    Topo do book promete 20% de spread com quantidade irrisória; a
    profundidade real inverte o sinal da operação. É a forma exata do
    problema que motivou toda esta camada.
    """
    # Magnitudes escolhidas para o executável ficar em ~-1,6%: dentro da faixa
    # PLAUSÍVEL, para este fixture isolar a recusa por profundidade da recusa
    # por implausibilidade (que tem teste próprio).
    spot = spot_depth_payload(
        bids=[(0.99, 100_000)],
        asks=[(1.00, 5), (1.05, 100_000)],
    )
    futures = futures_depth_payload(
        bids=[(1.20, 5), (1.02, 100_000)],
        asks=[(1.25, 100_000)],
    )
    return FakeMarketClient(spot), FakeMarketClient(futures)


async def dispara_entrada(engine, spread_pct=20.0, **kwargs):
    await engine.on_price_update(
        "JIMOTHY", spot_price=1.00, futures_price=1.02, spread_pct=spread_pct,
        prices_from_book=True, exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1, **kwargs,
    )


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------

async def test_entrada_confirmada_quando_a_profundidade_sustenta_o_spread():
    ms, mf = books_bons()
    engine, storage = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await dispara_entrada(engine, spread_pct=2.0)

    rt = engine.runtimes["JIMOTHY"]
    assert rt.state == PairState.OPEN
    # Spread executável = (1.02 - 1.00) / 1.00 = 2%
    assert rt.entry_spread_pct == pytest.approx(2.0)
    assert storage.events_of("entry_simulated")


async def test_entrada_recusada_quando_o_topo_do_book_mente():
    """
    O gatilho vê 20% (topo do book). A profundidade real, para o tamanho da
    posição, dá spread NEGATIVO. A versão anterior abria a posição aqui.
    """
    ms, mf = books_com_topo_enganoso()
    engine, storage = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await dispara_entrada(engine, spread_pct=20.0)

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert storage.events == []
    assert engine.depth_rejections["JIMOTHY"] == 1


async def test_entrada_recusada_quando_as_taxas_comem_a_margem():
    # Spot 1,000 / futures 1,003 -> 0,3% executável. Com alvo de saída de
    # 0,2% e 0,14% de taxas, sobram -0,04%: a operação perde dinheiro.
    spot = FakeMarketClient(spot_depth_payload(bids=[(0.999, 1e6)], asks=[(1.000, 1e6)]))
    futures = FakeMarketClient(futures_depth_payload(bids=[(1.003, 1e6)], asks=[(1.004, 1e6)]))
    engine, storage = make_engine(
        contract_size=1, entry_spread_pct=0.25, exit_spread_pct=0.20,
        market_spot=spot, market_futures=futures,
    )

    await dispara_entrada(engine, spread_pct=0.3)

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert storage.events == []


async def test_entrada_recusada_quando_o_book_nao_comporta_o_tamanho():
    # Book raso: nem 20 USDT de profundidade para uma posição de 100 USDT.
    spot = FakeMarketClient(spot_depth_payload(bids=[(0.999, 20)], asks=[(1.00, 20)]))
    futures = FakeMarketClient(futures_depth_payload(bids=[(1.02, 20)], asks=[(1.021, 20)]))
    engine, storage = make_engine(contract_size=1, market_spot=spot, market_futures=futures)

    await dispara_entrada(engine, spread_pct=2.0)

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert engine.depth_rejections["JIMOTHY"] == 1


async def test_spread_de_topo_abaixo_do_limiar_nem_consulta_profundidade():
    # A consulta de profundidade custa duas chamadas REST; o gatilho barato
    # existe justamente para não gastá-las à toa.
    ms, mf = books_bons()
    engine, _ = make_engine(contract_size=1, entry_spread_pct=5.0, market_spot=ms, market_futures=mf)

    await dispara_entrada(engine, spread_pct=2.0)

    assert ms.calls == 0
    assert mf.calls == 0


async def test_pernas_sao_casadas_por_quantidade_e_nao_por_valor():
    """
    Regressão do descasamento que inverteu o sinal do PnL em 03/08: casar as
    pernas por valor em USDT produz quantidades diferentes, porque os dois
    mercados executam a preços diferentes. O que precisa casar é a
    QUANTIDADE — ver `_target_spot_qty`.
    """
    ms, mf = books_bons()
    engine, _ = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await dispara_entrada(engine, spread_pct=2.0)

    rt = engine.runtimes["JIMOTHY"]
    spec = engine.contract_specs["JIMOTHY"]
    taxa = engine.fees_for("JIMOTHY").spot_taker_pct / 100

    exposicao_futures = rt.entry_futures_vol * spec.contract_size
    # A compra é inflada pela taxa (cobrada na moeda base) para que o SALDO
    # resultante — que é o que ficará disponível na saída — case com o short.
    saldo_spot_liquido = rt.entry_spot_qty * (1 - taxa)

    assert saldo_spot_liquido == pytest.approx(exposicao_futures, rel=1e-6)

    # Sob o critério antigo (mesmo valor em USDT), as quantidades divergiriam
    # pelo tamanho do spread — 2% aqui.
    qty_pelo_criterio_antigo = rt.entry_notional_usdt / rt.entry_spot_price
    assert abs(qty_pelo_criterio_antigo - exposicao_futures) / exposicao_futures > 0.015


# ---------------------------------------------------------------------------
# Travas de segurança
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spot_age,futures_age", [(10.0, 0.1), (0.1, 10.0)])
async def test_book_velho_de_qualquer_lado_bloqueia_a_decisao(spot_age, futures_age):
    """
    A idade é medida POR LADO. O caso perigoso é a assimetria: spot ticando
    a cada segundo enquanto o bid/ask do futures está congelado — o canal
    individual da MEXC só empurra quando há negócios naquele contrato.
    """
    ms, mf = books_bons()
    engine, _ = make_engine(contract_size=1, market_spot=ms, market_futures=mf, max_book_age_s=3.0)

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=spot_age, futures_book_age_s=futures_age,
    )

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert ms.calls == 0  # nem chegou a consultar profundidade


async def test_idade_desconhecida_nao_trava_o_bot():
    ms, mf = books_bons()
    engine, _ = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=None, futures_book_age_s=None,
    )

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN


async def test_duas_fontes_de_preco_simultaneas_nao_abrem_posicao_dobrada():
    """
    `on_price_update` é chamado por cinco fontes assíncronas independentes
    (WS de spot, WS de futures, dois pollings REST e o fallback). Nada
    impedia duas delas de verem o mesmo par em IDLE e dispararem DUAS
    entradas para a mesma oportunidade — a mudança de estado só acontecia
    depois de vários `await`.
    """
    ms, mf = books_bons()
    engine, storage = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await asyncio.gather(
        dispara_entrada(engine, spread_pct=2.0),
        dispara_entrada(engine, spread_pct=2.0),
        dispara_entrada(engine, spread_pct=2.0),
    )

    assert len(storage.events_of("entry_simulated")) == 1


async def test_falha_ao_buscar_book_adia_a_decisao_em_vez_de_operar_as_cegas():
    ms, mf = books_bons()
    ms.raise_error = RuntimeError("timeout na MEXC")
    engine, storage = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await dispara_entrada(engine, spread_pct=2.0)

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert storage.events == []


async def test_book_cruzado_adia_a_decisao():
    # bid >= ask é impossível num book consistente: snapshot corrompido.
    spot = FakeMarketClient(spot_depth_payload(bids=[(1.10, 1e6)], asks=[(1.00, 1e6)]))
    futures = FakeMarketClient(futures_depth_payload(bids=[(1.02, 1e6)], asks=[(1.021, 1e6)]))
    engine, storage = make_engine(contract_size=1, market_spot=spot, market_futures=futures)

    await dispara_entrada(engine, spread_pct=2.0)

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE


async def test_teto_global_de_exposicao_bloqueia_antes_de_consultar_o_book():
    ms, mf = books_bons()
    engine, _ = make_engine(
        contract_size=1, position_size_usdt=100.0,
        market_spot=ms, market_futures=mf, max_total_exposure_usdt=50.0,
    )

    await dispara_entrada(engine, spread_pct=2.0)

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert ms.calls == 0


async def test_estados_pausados_nunca_agem():
    ms, mf = books_bons()
    engine, storage = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    for estado in (PairState.PAUSED_ERROR, PairState.MANUAL_HALT):
        engine.runtimes["JIMOTHY"].state = estado
        await dispara_entrada(engine, spread_pct=2.0)
        assert engine.runtimes["JIMOTHY"].state == estado
        assert storage.events == []


# ---------------------------------------------------------------------------
# Saída
# ---------------------------------------------------------------------------

def abre_posicao(engine, spot_qty=100.0, futures_vol=100, spot_price=1.00, futures_price=1.02):
    rt = engine.runtimes["JIMOTHY"]
    rt.state = PairState.OPEN
    rt.entry_spread_pct = 2.0
    rt.entry_spot_price = spot_price
    rt.entry_futures_price = futures_price
    rt.entry_spot_qty = spot_qty
    rt.entry_futures_vol = futures_vol
    rt.entry_notional_usdt = futures_vol * futures_price
    return rt


async def test_saida_recusada_quando_a_profundidade_nao_confirma():
    """
    Reproduz a estrutura da saída real de 03/08: o topo do book mostrava
    0,23% e o executável para a posição era 1,44%. O bot ficava posicionado
    em vez de sair 1,2 pp pior do que enxergava.
    """
    spot = FakeMarketClient(spot_depth_payload(
        bids=[(0.006890, 50), (0.006800, 100_000)],
        asks=[(0.006950, 100_000)],
    ))
    futures = FakeMarketClient(futures_depth_payload(
        bids=[(0.006880, 100_000)], asks=[(0.006906, 100_000)],
    ))
    engine, storage = make_engine(
        contract_size=100, exit_spread_pct=0.25, market_spot=spot, market_futures=futures,
    )
    abre_posicao(engine, spot_qty=608.48, futures_vol=6)

    await engine.on_price_update(
        "JIMOTHY", 0.006890, 0.006906, spread_pct=0.0, prices_from_book=True,
        exit_spread_pct=0.2323,  # o que a tela mostrava
        spot_bid=0.006890, futures_ask=0.006906,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN
    assert storage.events_of("exit_simulated") == []
    assert engine.depth_rejections["JIMOTHY"] == 1


async def test_saida_executada_quando_a_profundidade_confirma():
    spot = FakeMarketClient(spot_depth_payload(bids=[(1.00, 1e6)], asks=[(1.001, 1e6)]))
    futures = FakeMarketClient(futures_depth_payload(bids=[(1.0005, 1e6)], asks=[(1.001, 1e6)]))
    engine, storage = make_engine(
        contract_size=1, exit_spread_pct=0.25, market_spot=spot, market_futures=futures,
    )
    abre_posicao(engine)

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.001, spread_pct=0.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=1.00, futures_ask=1.001,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert len(storage.events_of("exit_simulated")) == 1


async def test_saida_adiada_quando_o_book_nao_comporta_a_posicao():
    spot = FakeMarketClient(spot_depth_payload(bids=[(1.00, 10)], asks=[(1.001, 10)]))
    futures = FakeMarketClient(futures_depth_payload(bids=[(1.0005, 10)], asks=[(1.001, 10)]))
    engine, storage = make_engine(
        contract_size=1, exit_spread_pct=0.25, market_spot=spot, market_futures=futures,
    )
    abre_posicao(engine, spot_qty=100.0, futures_vol=100)

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.001, spread_pct=0.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=1.00, futures_ask=1.001,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN
    assert storage.events_of("exit_simulated") == []


async def test_saida_sem_dados_de_saida_completos_nao_consulta_book():
    # Regra preservada do bug mais caro do projeto: sem exit_spread_pct,
    # spot_bid e futures_ask, o bot não sai — nunca cai para o spread de
    # entrada como régua.
    ms, mf = books_bons()
    engine, _ = make_engine(contract_size=1, market_spot=ms, market_futures=mf)
    abre_posicao(engine)

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=None, spot_bid=None, futures_ask=None,
    )

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN
    assert ms.calls == 0


# ---------------------------------------------------------------------------
# Mudar configuração com posição aberta
# ---------------------------------------------------------------------------

async def test_mudar_config_com_posicao_aberta_preserva_a_posicao():
    """
    Pergunta levantada depois do incidente de 05/08: mudar `exit_spread_pct`
    no meio de uma operação aberta pode quebrar o fechamento?

    Não pode, e este teste garante isso: `set_pair_config` é um upsert que
    toca apenas a configuração. As quantidades usadas para fechar vêm do
    RUNTIME da posição (`entry_spot_qty` / `entry_futures_vol`), nunca da
    config — então mudar tamanho ou limiar no meio do caminho não altera o
    que será fechado.
    """
    ms, mf = books_bons()
    engine, _ = make_engine(contract_size=1, market_spot=ms, market_futures=mf)
    abre_posicao(engine, spot_qty=800.4, futures_vol=8)

    await engine.set_pair_config(
        "JIMOTHY", enabled=True, entry_spread_pct=2.5,
        exit_spread_pct=1.2, position_size_usdt=250.0,
    )

    rt = engine.runtimes["JIMOTHY"]
    assert rt.state == PairState.OPEN
    assert rt.entry_spot_qty == pytest.approx(800.4)
    assert rt.entry_futures_vol == pytest.approx(8)
    assert rt.entry_futures_price == pytest.approx(1.02)
    # E a nova config vale a partir de agora.
    assert engine.configs["JIMOTHY"].exit_spread_pct == pytest.approx(1.2)


async def test_novo_limiar_de_saida_passa_a_valer_imediatamente():
    """
    O efeito REAL de mudar `exit_spread_pct` no meio da operação: o bot passa
    a aceitar sair num spread pior. Foi isso que aconteceu em 05/08 — a saída
    disparou com executável 1,079%, contra 0,62% e 0,68% das operações
    anteriores. Muda QUANDO sai, não SE consegue fechar.
    """
    spot = FakeMarketClient(spot_depth_payload(bids=[(1.00, 1e6)], asks=[(1.002, 1e6)]))
    futures = FakeMarketClient(futures_depth_payload(bids=[(1.009, 1e6)], asks=[(1.010, 1e6)]))
    engine, storage = make_engine(
        contract_size=1, exit_spread_pct=0.25, market_spot=spot, market_futures=futures,
    )
    abre_posicao(engine)

    # Spread de saída executável = (1.010 - 1.00) / 1.00 = 1,0%: acima do
    # alvo de 0,25%, então o bot NÃO sai.
    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.010, spread_pct=0.0, prices_from_book=True,
        exit_spread_pct=0.9, spot_bid=1.00, futures_ask=1.010,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )
    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN

    # Usuário afrouxa o alvo para 1,2% no meio da operação: agora sai.
    await engine.set_pair_config(
        "JIMOTHY", enabled=True, entry_spread_pct=2.0,
        exit_spread_pct=1.2, position_size_usdt=100.0,
    )
    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.010, spread_pct=0.0, prices_from_book=True,
        exit_spread_pct=0.9, spot_bid=1.00, futures_ask=1.010,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )
    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert storage.events_of("exit_simulated")


# ---------------------------------------------------------------------------
# Plausibilidade: os dois lados sao o mesmo ativo?
# ---------------------------------------------------------------------------

async def test_spread_de_dois_digitos_e_recusado_como_colisao_de_simbolo():
    """
    Caso REAL e ativo nesta conta: COTI. O spot cota ~0,0106 e o futures
    ~0,0119 de forma estrutural (+11%), porque a MEXC migrou o token e os dois
    mercados referenciam versoes diferentes.

    Montar essa "arbitragem" compraria um ativo e venderia OUTRO -- exposicao
    direcional integral -- e a convergencia esperada nunca vem, porque a
    diferenca nao e uma deslocacao temporaria: e o que os dois ativos valem.

    O dashboard multi-exchange ja barrava isso por consenso entre venues. O
    caminho MEXC-only, que e o que ENVIA ORDEM, nao tinha protecao nenhuma.
    """
    spot = FakeMarketClient(spot_depth_payload(bids=[(0.010642, 1e7)], asks=[(0.010666, 1e7)]))
    futures = FakeMarketClient(futures_depth_payload(bids=[(0.011872, 1e7)], asks=[(0.011873, 1e7)]))
    engine, storage = make_engine(
        symbol="COTI", contract_size=1, entry_spread_pct=10.0, exit_spread_pct=5.0,
        market_spot=spot, market_futures=futures,
    )

    await engine.on_price_update(
        "COTI", 0.010666, 0.011872, spread_pct=11.31, prices_from_book=True,
        exit_spread_pct=11.57, spot_bid=0.010642, futures_ask=0.011873,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert engine.runtimes["COTI"].state == PairState.IDLE, "nao pode abrir posicao"
    evento = storage.events_of("entry_rejected_implausible")
    assert evento, "a recusa precisa ficar registrada, nao passar em silencio"
    assert evento[0]["detail"]["executable_spread_pct"] > 10


async def test_spread_normal_de_um_digito_continua_passando():
    # A porta nao pode barrar operacao legitima: 2% e o regime normal desta
    # estrategia.
    ms, mf = books_bons()
    engine, storage = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await dispara_entrada(engine, spread_pct=2.0)

    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN
    assert storage.events_of("entry_rejected_implausible") == []


async def test_limite_de_plausibilidade_e_configuravel():
    ms, mf = books_bons()
    # Com teto de 1%, ate os 2% normais passam a ser recusados.
    engine, storage = make_engine(
        contract_size=1, market_spot=ms, market_futures=mf, max_plausible_entry_pct=1.0,
    )
    await dispara_entrada(engine, spread_pct=2.0)
    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert storage.events_of("entry_rejected_implausible")


# ---------------------------------------------------------------------------
# Tamanho minimo: 1 contrato
# ---------------------------------------------------------------------------

async def test_tamanho_menor_que_um_contrato_e_recusado_na_configuracao():
    """
    Caso REAL: TUT ficou habilitado com 10 USDT, mas 1 contrato custa 22,81
    (contractSize=100 x 0,228). O par falhava em toda tentativa com
    `entry_failed_size_too_small` e ia para PAUSED_ERROR -- habilitado e
    inutil, sem dizer por que.

    Recusar na CONFIGURACAO transforma isso numa mensagem no momento em que da
    para corrigir.
    """
    engine, _ = make_engine(symbol="TUT", contract_size=100.0)
    engine._last_price_hint["TUT"] = 0.22807  # 1 contrato = 22,81 USDT

    with pytest.raises(ValueError, match="menor que 1 contrato"):
        await engine.set_pair_config("TUT", True, 1.0, -0.5, 10.0)

    # Com tamanho suficiente, passa.
    await engine.set_pair_config("TUT", True, 1.0, -0.5, 25.0)
    assert engine.configs["TUT"].position_size_usdt == 25.0


async def test_sem_preco_conhecido_a_validacao_nao_bloqueia():
    # Na primeira configuracao de um par o bot ainda nao viu preco nenhum.
    # Bloquear ali impediria configurar qualquer par novo.
    engine, _ = make_engine(symbol="NOVO", contract_size=100.0)
    engine._last_price_hint.pop("NOVO", None)
    await engine.set_pair_config("NOVO", True, 1.0, 0.5, 5.0)
    assert engine.configs["NOVO"].position_size_usdt == 5.0
