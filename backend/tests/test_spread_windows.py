"""
Testes das janelas móveis de spread (5/15/30/60 min).

A assimetria entre as duas metricas e o ponto central: entra-se no spread mais
ALTO e sai-se no mais BAIXO, entao cada janela guarda o MAXIMO da entrada e o
MINIMO da saida -- os dois juntos delimitam a melhor operacao que teria sido
possivel naquele intervalo.
"""
import pytest

from engine import SPREAD_BUCKETS_MAX, SPREAD_WINDOWS_MIN, PairState


def par():
    return PairState("X", "XUSDT", "X_USDT")


def amostra(p, base_min, entrada, saida=None):
    """Registra uma amostra no minuto `base_min` (epoch em minutos)."""
    p.spread_pct = entrada
    p.exit_spread_pct = saida
    p.record_spread_sample(now=base_min * 60)


def test_maximo_de_entrada_e_minimo_de_saida_na_janela():
    p = par()
    agora = 1_000_000
    amostra(p, agora - 2, entrada=1.0, saida=0.9)
    amostra(p, agora - 1, entrada=3.0, saida=0.2)   # melhor entrada e melhor saida
    amostra(p, agora, entrada=2.0, saida=0.5)

    w = p.window_stats(now=agora * 60)
    assert w["max_entry_5m"] == pytest.approx(3.0)
    assert w["min_exit_5m"] == pytest.approx(0.2)


def test_janelas_maiores_enxergam_o_que_as_menores_ja_perderam():
    p = par()
    agora = 1_000_000
    # Pico de 9% ha 20 minutos: fora de 5m e 15m, dentro de 30m e 60m.
    amostra(p, agora - 20, entrada=9.0, saida=-1.0)
    amostra(p, agora, entrada=1.0, saida=0.5)

    w = p.window_stats(now=agora * 60)
    assert w["max_entry_5m"] == pytest.approx(1.0)
    assert w["max_entry_15m"] == pytest.approx(1.0)
    assert w["max_entry_30m"] == pytest.approx(9.0)
    assert w["max_entry_60m"] == pytest.approx(9.0)
    assert w["min_exit_30m"] == pytest.approx(-1.0)
    assert w["min_exit_5m"] == pytest.approx(0.5)


def test_varias_amostras_no_mesmo_minuto_agregam_no_mesmo_balde():
    # E o que impede a memoria de crescer com a frequencia do polling: o
    # numero de baldes depende do TEMPO, nao de quantas amostras chegaram.
    p = par()
    agora = 1_000_000
    for entrada, saida in ((1.0, 0.8), (5.0, 0.1), (2.0, 0.4)):
        amostra(p, agora, entrada, saida)

    assert len(p._spread_buckets) == 1
    w = p.window_stats(now=agora * 60)
    assert w["max_entry_5m"] == pytest.approx(5.0)
    assert w["min_exit_5m"] == pytest.approx(0.1)


def test_memoria_limitada_a_sessenta_baldes():
    # 578 pares x 60 baldes e trivial; guardar amostra a amostra passaria de
    # um milhao de registros vivos.
    p = par()
    for i in range(500):
        amostra(p, 1_000_000 + i, entrada=float(i), saida=float(-i))
    assert len(p._spread_buckets) == SPREAD_BUCKETS_MAX == 60


def test_par_sem_amostras_devolve_none_e_nao_zero():
    # Zero seria um numero inventado: "sem dado" e "spread de 0%" sao coisas
    # diferentes e nao podem parecer a mesma na tela.
    w = par().window_stats()
    for janela in SPREAD_WINDOWS_MIN:
        assert w[f"max_entry_{janela}m"] is None
        assert w[f"min_exit_{janela}m"] is None


def test_amostra_sem_spread_de_entrada_e_ignorada():
    p = par()
    p.spread_pct = None
    p.exit_spread_pct = 0.5
    p.record_spread_sample()
    assert len(p._spread_buckets) == 0


def test_saida_ausente_nao_contamina_o_minimo():
    # exit_spread_pct fica None quando falta book de saida. Tratar isso como
    # zero produziria um minimo de saida que nunca existiu.
    p = par()
    agora = 1_000_000
    amostra(p, agora - 1, entrada=2.0, saida=None)
    amostra(p, agora, entrada=1.0, saida=0.7)

    w = p.window_stats(now=agora * 60)
    assert w["min_exit_5m"] == pytest.approx(0.7)
    assert w["max_entry_5m"] == pytest.approx(2.0)


def test_spreads_negativos_sao_preservados():
    # Spread de saida negativo e o cenario BOM desta estrategia (sai-se no
    # menor possivel); truncar em zero esconderia a melhor janela.
    p = par()
    agora = 1_000_000
    amostra(p, agora, entrada=-0.5, saida=-2.3)
    w = p.window_stats(now=agora * 60)
    assert w["max_entry_5m"] == pytest.approx(-0.5)
    assert w["min_exit_5m"] == pytest.approx(-2.3)


def test_snapshot_expoe_as_oito_metricas():
    p = par()
    amostra(p, 1_000_000, entrada=2.0, saida=0.3)
    d = p.to_dict()
    for janela in SPREAD_WINDOWS_MIN:
        assert f"max_entry_{janela}m" in d
        assert f"min_exit_{janela}m" in d
