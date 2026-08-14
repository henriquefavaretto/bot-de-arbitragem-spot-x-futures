"""
Testes de arredondamento de preço e quantidade (bot/sizing.py).

O arredondamento de PREÇO é novo: só passou a existir quando as ordens
deixaram de ser exclusivamente a mercado. A direção do arredondamento não é
detalhe estético — é o que faz o teto de slippage ser um teto de verdade.
"""
import pytest

from bot.sizing import (
    ContractSpec, SpotSymbolSpec, round_price_for_buy, round_price_for_sell,
    round_spot_quantity, round_spot_quote_qty, spot_price_tick,
    usdt_to_futures_vol, futures_vol_to_usdt,
)


# ---------------------------------------------------------------------------
# Arredondamento de preço: a direção protege o teto
# ---------------------------------------------------------------------------

def test_preco_de_compra_arredonda_para_baixo_para_nunca_estourar_o_teto():
    # O preço vem de "não aceito pagar mais que 1.23456789". Arredondar para
    # cima estouraria esse teto, e a garantia deixaria de ser garantia.
    assert round_price_for_buy(1.23456789, tick=0.0001) == pytest.approx(1.2345)


def test_preco_de_venda_arredonda_para_cima_para_nunca_furar_o_piso():
    # Espelhado: o teto de uma venda é um piso ("não aceito receber menos").
    assert round_price_for_sell(1.23451111, tick=0.0001) == pytest.approx(1.2346)


def test_preco_ja_alinhado_ao_tick_nao_se_move():
    assert round_price_for_buy(1.2345, tick=0.0001) == pytest.approx(1.2345)
    assert round_price_for_sell(1.2345, tick=0.0001) == pytest.approx(1.2345)


def test_erro_de_ponto_flutuante_nao_rouba_um_tick():
    """
    0.007 / 0.000001 dá 6999.999999999999 em ponto flutuante. Um `floor`
    ingênuo devolveria 6999 ticks = 0.006999, deslocando o preço da ordem
    para fora do que foi calculado — silenciosamente, e só de vez em quando,
    que é a pior forma de um bug financeiro aparecer.
    """
    assert round_price_for_buy(0.007, tick=0.000001) == pytest.approx(0.007)
    assert round_price_for_sell(0.007, tick=0.000001) == pytest.approx(0.007)
    # E o resultado não pode voltar com lixo decimal (7000 * 1e-6 costuma dar
    # 0.007000000000000001, que a MEXC rejeita por escala de preço).
    assert repr(round_price_for_buy(0.007, tick=0.000001)) == "0.007"


def test_tick_zero_ou_negativo_nao_altera_o_preco():
    # Spec incompleta não deve corromper o preço; melhor deixar passar
    # intacto do que dividir por zero no meio de uma execução.
    assert round_price_for_buy(1.23456, tick=0) == pytest.approx(1.23456)


def test_preco_nao_positivo_devolve_zero():
    assert round_price_for_buy(0, tick=0.01) == 0.0
    assert round_price_for_sell(-1, tick=0.01) == 0.0


def test_tick_do_spot_vem_da_precisao_de_preco():
    spec = SpotSymbolSpec("XUSDT", base_asset_precision=2, quote_precision=6, price_precision=8)
    assert spot_price_tick(spec) == pytest.approx(1e-8)


# ---------------------------------------------------------------------------
# Metadados: novos campos lidos da API
# ---------------------------------------------------------------------------

def test_contract_spec_le_tick_taxa_e_escala_de_preco():
    spec = ContractSpec.from_api_response({
        "symbol": "JIMOTHY_USDT", "contractSize": 100, "minVol": 1,
        "volScale": 0, "priceScale": 6, "priceUnit": 0.000001,
        "takerFeeRate": 0.0002,
    })
    assert spec.contract_size == 100
    assert spec.price_unit == pytest.approx(0.000001)
    assert spec.taker_fee_pct == pytest.approx(0.02)


def test_contract_spec_deriva_o_tick_da_escala_quando_price_unit_falta():
    spec = ContractSpec.from_api_response({
        "symbol": "X_USDT", "contractSize": 1, "minVol": 1, "priceScale": 4,
    })
    assert spec.price_unit == pytest.approx(0.0001)


def test_spot_spec_le_precisao_de_preco_separada_da_de_valor():
    # Confundir as duas gera "price scale is invalid": um par pode aceitar 2
    # casas no valor total em USDT e 8 no preço unitário.
    spec = SpotSymbolSpec.from_exchange_info({
        "symbol": "JIMOTHYUSDT", "baseAssetPrecision": 2,
        "quotePrecision": 2, "quoteAssetPrecision": 8,
        "takerCommission": 0.0005, "filters": [],
    })
    assert spec.quote_precision == 2
    assert spec.price_precision == 8
    assert spec.taker_fee_pct == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Comportamento já existente que não pode regredir
# ---------------------------------------------------------------------------

def test_quantidade_de_venda_sempre_arredonda_para_baixo():
    # Regressão do bug "amount scale is invalid": o saldo consultado via API
    # vem com imprecisão de ponto flutuante (1188.70000001 em vez de 1188.7).
    spec = SpotSymbolSpec("XUSDT", base_asset_precision=2, quote_precision=6)
    assert round_spot_quantity(1188.70000001, spec) == pytest.approx(1188.70)
    assert round_spot_quantity(1188.799, spec) == pytest.approx(1188.79)


def test_valor_em_usdt_arredonda_para_baixo():
    spec = SpotSymbolSpec("XUSDT", base_asset_precision=2, quote_precision=2)
    assert round_spot_quote_qty(9.31694500000001, spec) == pytest.approx(9.31)


def test_volume_de_contratos_nunca_arredonda_para_cima():
    spec = ContractSpec("X_USDT", contract_size=100, min_vol=1)
    # 4.30 USDT a 0.007169 com contratos de 100 -> 5.99 contratos -> 5
    assert usdt_to_futures_vol(4.30, 0.007169, spec) == 5
    assert futures_vol_to_usdt(6, 0.007169, spec) == pytest.approx(4.3014)


def test_valor_menor_que_um_contrato_devolve_zero_em_vez_de_arredondar():
    spec = ContractSpec("X_USDT", contract_size=100, min_vol=1)
    assert usdt_to_futures_vol(0.10, 0.007169, spec) == 0
