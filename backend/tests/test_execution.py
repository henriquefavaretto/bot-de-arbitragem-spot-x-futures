"""
Testes da política de execução com teto de slippage (bot/execution.py).

O teste mais importante deste arquivo é
`test_o_teto_nao_se_move_entre_tentativas`: um teto que se recalcula a cada
retry persegue o preço para longe e deixa de ser um teto. Sem essa
propriedade, três tentativas com 0,3% de tolerância acabariam executando
0,9% pior que o decidido — e a proteção seria só aparente.
"""
import pytest

from bot.execution import (
    LegFill, SlippagePolicy, execute_bounded, limit_price_for_buy, limit_price_for_sell,
)


FAST = SlippagePolicy(max_slippage_pct=0.3, max_attempts=3, attempt_delay_s=0)


class RecordingSender:
    """
    Registra cada (quantidade, preço) recebido e devolve o preenchimento
    programado em `plan`. `None` no plano significa "não preencheu nada".
    """

    def __init__(self, plan):
        self.plan = plan
        self.calls: list[tuple[float, float]] = []

    async def __call__(self, qty, price=None):
        self.calls.append((qty, price))
        idx = min(len(self.calls) - 1, len(self.plan) - 1)
        item = self.plan[idx]
        if item is None:
            return None
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# Preço-limite
# ---------------------------------------------------------------------------

def test_teto_de_compra_fica_acima_do_vwap_e_piso_de_venda_abaixo():
    policy = SlippagePolicy(max_slippage_pct=1.0)
    assert limit_price_for_buy(100.0, policy, tick=0.01) == pytest.approx(101.0)
    assert limit_price_for_sell(100.0, policy, tick=0.01) == pytest.approx(99.0)


def test_arredondamento_do_limite_nunca_piora_o_preco():
    policy = SlippagePolicy(max_slippage_pct=0.3)
    # Compra: 100 * 1.003 = 100.3 -> com tick de 1, arredonda PARA BAIXO
    assert limit_price_for_buy(100.0, policy, tick=1.0) == pytest.approx(100.0)
    # Venda: 100 * 0.997 = 99.7 -> com tick de 1, arredonda PARA CIMA
    assert limit_price_for_sell(100.0, policy, tick=1.0) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Caminho feliz
# ---------------------------------------------------------------------------

async def test_preenchimento_total_na_primeira_tentativa_nao_repete_ordem():
    send = RecordingSender([{"filled_qty": 100, "notional": 1000}])

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.5,
        send_ioc=send, policy=FAST,
    )

    assert result.filled_qty == 100
    assert result.avg_price == pytest.approx(10.0)
    assert result.complete is True
    assert result.attempts == 1
    assert result.escalated is False
    assert len(send.calls) == 1


async def test_preenchimento_parcial_acumula_entre_tentativas():
    send = RecordingSender([
        {"filled_qty": 40, "notional": 400},
        {"filled_qty": 35, "notional": 353.5},
        {"filled_qty": 25, "notional": 254.0},
    ])

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.5,
        send_ioc=send, policy=FAST,
    )

    assert result.filled_qty == pytest.approx(100)
    assert result.notional == pytest.approx(1007.5)
    assert result.attempts == 3
    assert result.complete is True
    # Cada tentativa pede só o que falta, nunca a quantidade inteira de novo:
    # repedir tudo compraria em duplicidade o que já preencheu.
    assert [q for q, _ in send.calls] == pytest.approx([100, 60, 25])


async def test_o_teto_nao_se_move_entre_tentativas():
    """
    Propriedade central: todas as tentativas usam o MESMO preço absoluto.

    Se o teto fosse recalculado a partir do book novo a cada retry, um
    mercado em fuga faria cada tentativa aceitar um preço pior que a
    anterior, e a tolerância configurada seria multiplicada pelo número de
    tentativas sem ninguém perceber.
    """
    send = RecordingSender([
        {"filled_qty": 10, "notional": 105},
        {"filled_qty": 10, "notional": 105},
        {"filled_qty": 10, "notional": 105},
    ])

    await execute_bounded(
        label="teste", total_qty=100, limit_price=10.5,
        send_ioc=send, policy=FAST,
    )

    precos = {p for _, p in send.calls}
    assert precos == {10.5}


# ---------------------------------------------------------------------------
# Escalonamento
# ---------------------------------------------------------------------------

async def test_residuo_escala_para_mercado_quando_o_teto_nao_comporta():
    ioc = RecordingSender([{"filled_qty": 30, "notional": 315}, None, None])
    market = RecordingSender([{"filled_qty": 70, "notional": 770}])

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.5,
        send_ioc=ioc, send_market=market, policy=FAST,
    )

    assert result.escalated is True
    assert result.filled_qty == pytest.approx(100)
    # O mercado recebe exatamente o que faltou, nunca o total.
    assert market.calls[0][0] == pytest.approx(70)


async def test_sem_escalonamento_a_perna_termina_parcial_sem_ordem_a_mercado():
    ioc = RecordingSender([{"filled_qty": 30, "notional": 315}, None, None])
    market = RecordingSender([{"filled_qty": 70, "notional": 770}])
    policy = SlippagePolicy(max_slippage_pct=0.3, max_attempts=3,
                            escalate_to_market=False, attempt_delay_s=0)

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.5,
        send_ioc=ioc, send_market=market, policy=policy,
    )

    assert result.escalated is False
    assert result.filled_qty == pytest.approx(30)
    assert result.complete is False
    assert market.calls == []


async def test_nao_escala_quando_o_preenchimento_ja_esta_completo():
    ioc = RecordingSender([{"filled_qty": 100, "notional": 1000}])
    market = RecordingSender([{"filled_qty": 100, "notional": 1100}])

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.5,
        send_ioc=ioc, send_market=market, policy=FAST,
    )

    assert result.escalated is False
    assert market.calls == []


# ---------------------------------------------------------------------------
# Teto dinâmico de quantidade (saldo real)
# ---------------------------------------------------------------------------

async def test_saldo_real_limita_a_quantidade_pedida():
    """
    Regressão do erro `Insufficient position`: a MEXC desconta a taxa na
    própria moeda comprada, então a quantidade vendável é menor que a
    comprada. Vender a quantidade "de livro" falha.
    """
    send = RecordingSender([{"filled_qty": 99.5, "notional": 995}])

    async def saldo():
        return 99.5

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=send, policy=FAST, qty_cap_provider=saldo,
    )

    assert send.calls[0][0] == pytest.approx(99.5)
    assert result.filled_qty == pytest.approx(99.5)


async def test_falha_ao_consultar_saldo_nao_aborta_a_perna():
    send = RecordingSender([{"filled_qty": 100, "notional": 1000}])

    async def saldo():
        raise RuntimeError("rede caiu")

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=send, policy=FAST, qty_cap_provider=saldo,
    )

    # Segue com a quantidade calculada, que é o comportamento que já existia.
    assert result.filled_qty == pytest.approx(100)


async def test_saldo_zerado_encerra_sem_enviar_ordem():
    send = RecordingSender([{"filled_qty": 100, "notional": 1000}])

    async def saldo():
        return 0.0

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=send, policy=FAST, qty_cap_provider=saldo,
    )

    assert send.calls == []
    assert result.filled_qty == 0


# ---------------------------------------------------------------------------
# Erros e arredondamento
# ---------------------------------------------------------------------------

async def test_erros_sao_coletados_sem_propagar_excecao():
    # Quem chama precisa decidir o que fazer com falha parcial, e essa
    # decisão depende de ser entrada (dá para reverter) ou saída (não dá).
    send = RecordingSender([RuntimeError("amount scale is invalid")] * 3)

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=send, policy=FAST,
    )

    assert result.filled_qty == 0
    assert len(result.errors) == 3
    assert "amount scale" in result.errors[0]


async def test_erro_numa_tentativa_nao_impede_a_seguinte():
    send = RecordingSender([
        RuntimeError("falha transitória"),
        {"filled_qty": 100, "notional": 1000},
    ])

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=send, policy=FAST,
    )

    assert result.filled_qty == pytest.approx(100)
    assert len(result.errors) == 1


async def test_arredondamento_de_quantidade_e_aplicado_a_cada_tentativa():
    send = RecordingSender([{"filled_qty": 100.12, "notional": 1001.2}])

    result = await execute_bounded(
        label="teste", total_qty=100.129999, limit_price=10.0,
        send_ioc=send, policy=FAST,
        round_qty=lambda q: int(q * 100) / 100,
    )

    assert send.calls[0][0] == pytest.approx(100.12)
    assert result.filled_qty == pytest.approx(100.12)


async def test_quantidade_ou_preco_invalido_nao_envia_nada():
    send = RecordingSender([{"filled_qty": 1, "notional": 1}])

    assert (await execute_bounded(label="t", total_qty=0, limit_price=10, send_ioc=send)).filled_qty == 0
    assert (await execute_bounded(label="t", total_qty=10, limit_price=0, send_ioc=send)).filled_qty == 0
    assert send.calls == []


def test_leg_fill_vazio_tem_preco_medio_zero_sem_dividir_por_zero():
    fill = LegFill(requested_qty=100)
    assert fill.avg_price == 0.0
    assert fill.complete is False


# ---------------------------------------------------------------------------
# REGRESSÃO: rate limit da MEXC (erro 510) — incidente de 05/08/2026
# ---------------------------------------------------------------------------

class ErroMexc(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"MEXC Futures API error {code}: {message}")


RATE_LIMIT = ErroMexc(510, "Requests are too frequent, please try again later")

SEM_ESPERA = SlippagePolicy(
    max_slippage_pct=0.3, max_attempts=3, attempt_delay_s=0,
    rate_limit_backoff_s=0, rate_limit_max_retries=4,
)


def test_erro_510_da_mexc_e_reconhecido_como_rate_limit():
    from bot.execution import is_rate_limited
    assert is_rate_limited(RATE_LIMIT) is True
    assert is_rate_limited(ErroMexc(429, "Too Many Requests")) is True
    assert is_rate_limited(ErroMexc(-1003, "too many requests")) is True
    # Erros de verdade não podem ser confundidos com rate limit: repetir um
    # erro de validação não resolve nada e mascara o problema real.
    assert is_rate_limited(ErroMexc(600, "amount scale is invalid")) is False
    assert is_rate_limited(RuntimeError("insufficient position")) is False


async def test_rate_limit_nao_consome_uma_tentativa_de_preco():
    """
    Um 510 significa "pedi rápido demais", não "o book recusou meu preço".
    Tratá-lo como tentativa falha queimava tentativas sem nunca ter criado
    ordem — em 05/08 a 3ª tentativa e o escalonamento simplesmente não
    viraram ordem nenhuma na MEXC.
    """
    send = RecordingSender([RATE_LIMIT, RATE_LIMIT, {"filled_qty": 100, "notional": 1000}])

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=send, policy=SEM_ESPERA,
    )

    assert result.filled_qty == pytest.approx(100)
    assert result.complete is True
    # Três envios, mas UMA só tentativa de preço: os dois 510 foram reenvios.
    assert len(send.calls) == 3
    assert result.attempts == 1
    assert result.errors == []


async def test_escalonamento_a_mercado_insiste_apesar_do_rate_limit():
    """
    O escalonamento é a rede de segurança contra terminar com uma perna só.
    Em 05/08 ele foi abandonado por rate limit e o resultado foi um short
    descoberto. Ele precisa insistir mais que as tentativas normais.
    """
    ioc = RecordingSender([None, None, None])
    market = RecordingSender([RATE_LIMIT, RATE_LIMIT, {"filled_qty": 100, "notional": 1100}])

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=ioc, send_market=market, policy=SEM_ESPERA,
    )

    assert result.escalated is True
    assert result.filled_qty == pytest.approx(100)
    assert len(market.calls) == 3


async def test_erro_que_nao_e_rate_limit_nao_e_reenviado():
    send = RecordingSender([ErroMexc(600, "amount scale is invalid")] * 3)

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=send, policy=SEM_ESPERA,
    )

    # Uma chamada por tentativa, sem reenvio — repetir não resolveria.
    assert len(send.calls) == 3
    assert result.attempts == 3
    assert len(result.errors) == 3


async def test_rate_limit_persistente_e_registrado_como_erro():
    # Se nem a espera resolver, o erro precisa ficar visível no resultado em
    # vez de sumir — foi a ausência desse rastro que tornou o incidente de
    # 05/08 um mistério.
    ioc = RecordingSender([RATE_LIMIT] * 20)

    result = await execute_bounded(
        label="teste", total_qty=100, limit_price=10.0,
        send_ioc=ioc, policy=SEM_ESPERA,
    )

    assert result.filled_qty == 0
    assert any("510" in e for e in result.errors)


def test_cadencia_padrao_respeita_o_limite_de_ordens_da_mexc():
    """
    Medido em 05/08/2026: o endpoint de ordens de futures aceita ~2 ordens a
    cada ~2s. Com 0,15s entre tentativas (o padrão antigo) a 3ª batia em 510.
    """
    padrao = SlippagePolicy()
    assert padrao.attempt_delay_s >= 1.0
    assert padrao.rate_limit_max_retries >= 1
