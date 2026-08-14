"""
Custos reais de uma operação de arbitragem — taxas e funding.

## Por que isso existe

Os limiares `entry_spread_pct` / `exit_spread_pct` sempre foram spread
BRUTO de preço. Mas o resultado de uma operação completa é:

    lucro% = spread_entrada - spread_saída - taxas - funding

Um round-trip completo tem QUATRO pernas cobradas:

    compra Spot (taker) + abre short Futures (taker)     <- entrada
    vende Spot (taker)  + fecha short Futures (taker)    <- saída

Com as taxas padrão da MEXC (Spot taker 0,05%, Futures taker 0,02%) isso dá
~0,14% de custo fixo por operação. Numa estratégia cujo lucro-alvo é da
ordem de 1-2%, ignorar 0,14% já é relevante; em alvos menores, é a
diferença entre lucro e prejuízo — e era invisível na tela.

## Funding

A perna de futures fica VENDIDA (short). Quando o funding rate é positivo,
quem está short RECEBE (é receita, reduz o custo). Quando é negativo, quem
está short PAGA. Como o tempo de permanência não é conhecido no momento da
decisão, usamos uma estimativa conservadora configurável de horas de
permanência e só contamos o funding quando ele é CUSTO — nunca somamos
funding esperado como receita para justificar uma entrada. Contar receita
incerta para liberar uma operação é exatamente o tipo de otimismo que este
projeto evita em código de decisão.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger("bot_costs")

# Taxas padrão da MEXC, usadas quando a API não devolve a taxa real da conta.
# Conservadoras de propósito: se a taxa real for menor, o bot só é um pouco
# mais exigente do que precisaria; se fosse ao contrário, ele entraria em
# operações que não pagam o próprio custo.
DEFAULT_SPOT_TAKER_PCT = 0.05      # 0,05%
DEFAULT_FUTURES_TAKER_PCT = 0.02   # 0,02%

# Intervalo padrão de cobrança de funding na MEXC (8 horas).
FUNDING_INTERVAL_HOURS = 8.0


@dataclass
class FeeModel:
    """
    Taxas efetivas por perna, em PERCENTUAL (0.05 = 0,05%), não em fração.

    A unidade é percentual para bater com a unidade de todo o resto do
    projeto (`entry_spread_pct`, `spread_pct`...). Misturar fração e
    percentual no mesmo cálculo é um erro de fator 100 que passa
    despercebido em revisão — por isso a convenção é única e explícita.
    """
    spot_taker_pct: float = DEFAULT_SPOT_TAKER_PCT
    futures_taker_pct: float = DEFAULT_FUTURES_TAKER_PCT

    @property
    def entry_cost_pct(self) -> float:
        """Custo das duas pernas de entrada (compra Spot + abre short Futures)."""
        return self.spot_taker_pct + self.futures_taker_pct

    @property
    def exit_cost_pct(self) -> float:
        """Custo das duas pernas de saída (vende Spot + fecha short Futures)."""
        return self.spot_taker_pct + self.futures_taker_pct

    @property
    def round_trip_pct(self) -> float:
        """Custo total das quatro pernas de uma operação completa."""
        return self.entry_cost_pct + self.exit_cost_pct


def funding_cost_pct(funding_rate: float, expected_hold_hours: float) -> float:
    """
    Custo estimado de funding para uma posição SHORT em futures, em
    percentual do notional, ao longo de `expected_hold_hours`.

    `funding_rate` é a fração por período de 8h, como a MEXC reporta
    (ex: -0.0003 = -0,03% por período).

    Convenção de sinal: rate POSITIVO = quem está short recebe. Como só
    contamos custo (nunca receita, ver docstring do módulo), retorna 0 nesse
    caso e retorna o valor absoluto quando o rate é negativo.
    """
    if funding_rate >= 0 or expected_hold_hours <= 0:
        return 0.0
    periods = expected_hold_hours / FUNDING_INTERVAL_HOURS
    return abs(funding_rate) * 100 * periods


@dataclass
class TradeEconomics:
    """
    Resultado da avaliação econômica de uma entrada candidata: o spread
    executável já descontado de tudo que vai ser cobrado.
    """
    entry_spread_pct: float        # spread de entrada EXECUTÁVEL (não o de topo de book)
    assumed_exit_spread_pct: float  # o alvo de saída configurado pelo usuário
    fee_cost_pct: float
    funding_cost_pct: float
    net_pct: float                  # margem líquida esperada, em pp do notional
    viable: bool

    @property
    def total_cost_pct(self) -> float:
        return self.fee_cost_pct + self.funding_cost_pct


def evaluate_entry(
    entry_spread_pct: float,
    target_exit_spread_pct: float,
    fees: FeeModel,
    funding_rate: float = 0.0,
    expected_hold_hours: float = 1.0,
    min_net_pct: float = 0.0,
) -> TradeEconomics:
    """
    Decide se uma entrada faz sentido econômico, no PIOR caso admitido pela
    própria configuração do usuário.

    A pergunta que isto responde é: "se eu entrar neste spread executável e
    sair exatamente no meu alvo de saída configurado, ainda sobra dinheiro
    depois das taxas e do funding?"

    Usar o alvo de saída configurado (e não um alvo otimista) é deliberado:
    o alvo é o pior spread de saída que o usuário aceitou de antemão, então
    é o cenário realista de encerramento. Se nem esse cenário paga os
    custos, a operação é estruturalmente perdedora e não deve ser aberta,
    por mais atraente que o spread bruto pareça na tela.
    """
    fee_cost = fees.round_trip_pct
    funding = funding_cost_pct(funding_rate, expected_hold_hours)
    net = entry_spread_pct - target_exit_spread_pct - fee_cost - funding

    return TradeEconomics(
        entry_spread_pct=entry_spread_pct,
        assumed_exit_spread_pct=target_exit_spread_pct,
        fee_cost_pct=fee_cost,
        funding_cost_pct=funding,
        net_pct=net,
        viable=net > min_net_pct,
    )


def realized_pnl_pct(entry_spread_pct: float, exit_spread_pct: float, fees: FeeModel) -> float:
    """
    Resultado líquido de uma operação completa, em percentual do notional,
    a partir dos spreads REALIZADOS (calculados dos preços de fill).

    Identidade central da estratégia — vale a pena deixar explícita porque é
    o que fecha a conta do caso real de JIMOTHY:

        lucro% = spread_entrada_realizado - spread_saída_realizado - taxas

        1,4146% - 1,4395% = -0,025%  (antes das taxas)  ->  PnL medido: -0,0010 USDT
                                                            sobre 4,30 USDT de notional
    """
    return entry_spread_pct - exit_spread_pct - fees.round_trip_pct


def parse_spot_taker_pct(exchange_info_symbol: dict) -> float:
    """
    Extrai a taxa de taker real do símbolo a partir do `exchangeInfo` do
    Spot, com validação de plausibilidade.

    A MEXC reporta esse campo como fração (0.0005 = 0,05%), mas o formato
    já variou entre versões da API e entre contas. Em vez de confiar cego,
    só aceitamos o valor se ele cair numa faixa fisicamente plausível para
    uma taxa de exchange (0% a 1%); fora disso, caímos no padrão
    conservador e registramos aviso. Um erro de fator 100 aqui inverteria
    completamente a decisão de entrada.
    """
    raw = exchange_info_symbol.get("takerCommission")
    if raw is None:
        return DEFAULT_SPOT_TAKER_PCT
    try:
        pct = float(raw) * 100
    except (TypeError, ValueError):
        return DEFAULT_SPOT_TAKER_PCT

    if 0 <= pct <= 1.0:
        return pct

    logger.warning(
        "takerCommission implausível (%s -> %.4f%%) para %s; usando o padrão de %.2f%%.",
        raw, pct, exchange_info_symbol.get("symbol"), DEFAULT_SPOT_TAKER_PCT,
    )
    return DEFAULT_SPOT_TAKER_PCT


def parse_futures_taker_pct(contract_detail: dict) -> float:
    """
    Mesma ideia para o Futures, a partir de `GET /api/v1/contract/detail`
    (campo `takerFeeRate`, fração por contrato).
    """
    raw = contract_detail.get("takerFeeRate")
    if raw is None:
        return DEFAULT_FUTURES_TAKER_PCT
    try:
        pct = float(raw) * 100
    except (TypeError, ValueError):
        return DEFAULT_FUTURES_TAKER_PCT

    if 0 <= pct <= 1.0:
        return pct

    logger.warning(
        "takerFeeRate implausível (%s -> %.4f%%) para %s; usando o padrão de %.2f%%.",
        raw, pct, contract_detail.get("symbol"), DEFAULT_FUTURES_TAKER_PCT,
    )
    return DEFAULT_FUTURES_TAKER_PCT
