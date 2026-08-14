"""
Testes do modelo de custos (bot/costs.py).

Inclui a reconciliação com o PnL real medido em 03/08/2026: se a identidade
`lucro = spread_entrada - spread_saída - taxas` não reproduzir o número que
saiu da conta MEXC, o modelo de custos está errado em algum lugar.
"""
import pytest

from bot.costs import (
    DEFAULT_FUTURES_TAKER_PCT, DEFAULT_SPOT_TAKER_PCT, FeeModel,
    evaluate_entry, funding_cost_pct, parse_futures_taker_pct,
    parse_spot_taker_pct, realized_pnl_pct,
)


# ---------------------------------------------------------------------------
# Taxas
# ---------------------------------------------------------------------------

def test_round_trip_cobra_as_quatro_pernas():
    fees = FeeModel(spot_taker_pct=0.05, futures_taker_pct=0.02)
    # entrada: compra spot + abre short; saída: vende spot + fecha short
    assert fees.entry_cost_pct == pytest.approx(0.07)
    assert fees.exit_cost_pct == pytest.approx(0.07)
    assert fees.round_trip_pct == pytest.approx(0.14)


def test_taxa_reportada_pela_api_e_usada_quando_plausivel():
    assert parse_spot_taker_pct({"symbol": "X", "takerCommission": 0.0005}) == pytest.approx(0.05)
    assert parse_futures_taker_pct({"symbol": "X", "takerFeeRate": 0.0002}) == pytest.approx(0.02)


def test_taxa_implausivel_cai_no_padrao_conservador():
    # Um erro de fator 100 aqui (fração lida como percentual, ou vice-versa)
    # inverteria a decisão de entrada sem nenhum sintoma visível.
    assert parse_spot_taker_pct({"symbol": "X", "takerCommission": 5}) == DEFAULT_SPOT_TAKER_PCT
    assert parse_futures_taker_pct({"symbol": "X", "takerFeeRate": -1}) == DEFAULT_FUTURES_TAKER_PCT


def test_taxa_ausente_ou_ilegivel_cai_no_padrao():
    assert parse_spot_taker_pct({}) == DEFAULT_SPOT_TAKER_PCT
    assert parse_spot_taker_pct({"takerCommission": "nao-e-numero"}) == DEFAULT_SPOT_TAKER_PCT


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------

def test_funding_positivo_nao_vira_receita_na_decisao():
    # Com rate positivo quem está short RECEBE, mas nunca somamos receita
    # incerta para justificar uma entrada.
    assert funding_cost_pct(0.0005, expected_hold_hours=8) == 0.0


def test_funding_negativo_vira_custo_proporcional_ao_tempo():
    # -0,03% por período de 8h, mantido 4h -> metade do período.
    assert funding_cost_pct(-0.0003, expected_hold_hours=4) == pytest.approx(0.015)
    assert funding_cost_pct(-0.0003, expected_hold_hours=8) == pytest.approx(0.03)


def test_funding_com_permanencia_zero_e_zero():
    assert funding_cost_pct(-0.01, expected_hold_hours=0) == 0.0


# ---------------------------------------------------------------------------
# Viabilidade da entrada
# ---------------------------------------------------------------------------

def test_entrada_viavel_quando_o_spread_paga_alvo_de_saida_e_custos():
    fees = FeeModel(0.05, 0.02)
    econ = evaluate_entry(
        entry_spread_pct=1.50, target_exit_spread_pct=0.20,
        fees=fees, funding_rate=0.0, expected_hold_hours=1.0,
    )
    # 1.50 - 0.20 - 0.14 = 1.16
    assert econ.net_pct == pytest.approx(1.16)
    assert econ.viable is True


def test_entrada_inviavel_quando_as_taxas_comem_a_margem():
    fees = FeeModel(0.05, 0.02)
    econ = evaluate_entry(
        entry_spread_pct=0.30, target_exit_spread_pct=0.20,
        fees=fees, funding_rate=0.0,
    )
    # 0.30 - 0.20 - 0.14 = -0.04: o spread bruto parecia positivo, mas a
    # operação inteira perde dinheiro. Era invisível antes deste módulo.
    assert econ.net_pct == pytest.approx(-0.04)
    assert econ.viable is False


def test_margem_minima_exigida_bloqueia_entrada_marginal():
    fees = FeeModel(0.05, 0.02)
    econ = evaluate_entry(
        entry_spread_pct=0.50, target_exit_spread_pct=0.20, fees=fees,
        min_net_pct=0.30,
    )
    assert econ.net_pct == pytest.approx(0.16)
    assert econ.viable is False  # positivo, mas abaixo da margem exigida


def test_funding_negativo_pode_inviabilizar_uma_entrada_que_parecia_boa():
    fees = FeeModel(0.05, 0.02)
    econ = evaluate_entry(
        entry_spread_pct=0.40, target_exit_spread_pct=0.20, fees=fees,
        funding_rate=-0.0080, expected_hold_hours=8.0,  # -0,8% por período
    )
    assert econ.funding_cost_pct == pytest.approx(0.80)
    assert econ.viable is False


# ---------------------------------------------------------------------------
# REGRESSÃO: reconciliação com o PnL real de 03/08/2026
# ---------------------------------------------------------------------------

def test_identidade_do_lucro_reproduz_o_pnl_real_de_jimothy():
    """
    Evento real gravado em `bot_trade_log`:

        entry_live (17:58:44): spread_pct = +1,4146272457207687%
        exit_live  (18:23:31): exit_spread_pct = +1,4394829612220916%
                               pnl_total_usdt = -0,00101328
                               notional = 4,3014 USDT

    A identidade da estratégia diz que o resultado bruto é simplesmente
    `spread_entrada - spread_saída`. Este teste confirma que ela bate com o
    dinheiro que de fato saiu da conta — é o que dá confiança de que os
    números que o bot registra descrevem a realidade.
    """
    entry_realizado = 1.4146272457207687
    saida_realizada = 1.4394829612220916
    notional = 4.3014
    pnl_medido = -0.00101328

    bruto_pct = entry_realizado - saida_realizada
    pnl_previsto = bruto_pct / 100 * notional

    # Bate na quarta casa decimal de USDT — a diferença residual vem do
    # descasamento de quantidade entre as pernas (608,48 spot contra 600
    # futures), que é justamente o outro defeito corrigido nesta rodada.
    assert pnl_previsto == pytest.approx(pnl_medido, abs=0.0002)

    # E com as taxas incluídas, a operação era ainda pior do que o registrado:
    # o PnL bruto ignorava 0,14% de custo de round-trip.
    fees = FeeModel(0.05, 0.02)
    liquido = realized_pnl_pct(entry_realizado, saida_realizada, fees)
    assert liquido == pytest.approx(-0.1649, abs=0.001)
    assert liquido < bruto_pct


def test_o_que_a_tela_prometia_versus_o_que_foi_executado():
    """
    Quantifica o problema em uma linha: a tela prometia +1,79% e a execução
    entregou -0,02%. A diferença de 1,82 pp é o que a camada de profundidade
    e o teto de slippage existem para recuperar.
    """
    tela = 2.0254957507082136 - 0.23232176564542453
    realizado = 1.4146272457207687 - 1.4394829612220916

    assert tela == pytest.approx(1.793, abs=0.001)
    assert realizado == pytest.approx(-0.025, abs=0.001)
    assert tela - realizado == pytest.approx(1.818, abs=0.005)
