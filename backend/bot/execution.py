"""
Execução com TETO DE SLIPPAGE: ordens limite IOC com retry e escalonamento.

## O que mudou e por quê

Antes, as quatro pernas da operação eram ordens a MERCADO puras. Ordem a
mercado não tem nenhum limite de preço: ela varre o book até completar a
quantidade, a qualquer preço. Num par ilíquido, isso significou consumir
1,82 pontos percentuais — o lucro inteiro — numa única operação de 4,30 USDT
(ver bot/depth.py para o caso medido).

O caminho normal de execução agora é uma ordem LIMITE com `IOC`
(Immediate Or Cancel), com o preço-limite ancorado no VWAP calculado sobre a
profundidade real do book no momento da decisão, mais uma tolerância
configurável. Comportamento resultante:

- executa imediatamente contra o book, como a ordem a mercado fazia;
- mas nunca a um preço pior que o teto;
- o que não couber dentro do teto é cancelado na hora, não fica pendurado
  no book (ordem pendurada = perna solta = exposição direcional).

## A âncora do teto NÃO se move entre tentativas

Esta é a decisão sutil mais importante do módulo. O teto é calculado UMA
vez, a partir do book do momento da decisão, e todas as tentativas seguintes
usam esse mesmo valor absoluto.

A alternativa — recalcular o teto a cada tentativa, a partir do book novo —
parece mais "adaptativa", mas é exatamente o comportamento que se quer
evitar: se o mercado está fugindo, cada retry perseguiria o preço para
longe, e três tentativas com 0,3% de tolerância cada acabariam executando
0,9% pior que o decidido, sem nenhum teto efetivo. Um teto que se move não é
um teto.

## Escalonamento para mercado

Se, depois de todas as tentativas, ainda faltar quantidade, a política
`escalate_to_market` decide o desfecho. Com ela ligada (padrão), o restante
vai a mercado. Isso é uma troca explícita e consciente: aceitar preço ruim
no resíduo é pior que aceitar preço bom, mas é MUITO melhor que terminar a
operação com uma perna aberta e outra não — que deixa exposição direcional
não intencional numa estratégia cuja premissa inteira é ser neutra.

Todo escalonamento é registrado no resultado (`escalated=True`) e logado em
WARNING, porque é sinal de que a tolerância configurada está apertada demais
para a liquidez daquele par.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from bot.sizing import round_price_for_buy, round_price_for_sell

logger = logging.getLogger("bot_execution")


@dataclass
class SlippagePolicy:
    """
    Quanto de piora de preço a operação aceita, e o que fazer se não couber.

    `max_slippage_pct` é uma tolerância SOBRE O VWAP já calculado com a
    profundidade real — não sobre o topo do book. Ou seja: o custo de andar
    o book para o tamanho da posição já está embutido no preço de
    referência, e esta tolerância cobre apenas o que se move entre a decisão
    e a ordem chegar na exchange. Por isso pode ser pequena (décimos de
    ponto percentual) sem travar a execução.
    """
    max_slippage_pct: float = 0.30
    max_attempts: int = 3
    escalate_to_market: bool = True
    # Pausa entre tentativas.
    #
    # Era 0,15s, o que parecia certo (quanto menor a pausa, menor o tempo com
    # a operação pela metade) e estava errado: o endpoint de ordens de
    # futures da MEXC aceita cerca de DUAS ordens a cada ~2 segundos e
    # devolve erro 510 ("Requests are too frequent") no resto.
    #
    # Medido em 05/08/2026 reproduzindo a cadência antiga:
    #     t=0.00s  tentativa 1: ACEITA
    #     t=0.56s  tentativa 2: ACEITA
    #     t=1.71s  tentativa 3: REJEITADA -> erro 510
    #     t=2.16s  tentativa 4: ACEITA
    #
    # Foi exatamente esse padrão que deixou uma posição aberta: as duas
    # primeiras tentativas não preencheram, a terceira e o escalonamento a
    # mercado nem chegaram a virar ordem. Correr mais rápido que o rate limit
    # não é ser rápido — é não enviar ordem nenhuma.
    attempt_delay_s: float = 1.0
    # Um 510 não é "o book recusou meu preço", é "eu pedi rápido demais".
    # São coisas diferentes e precisam de tratamento diferente: o rate limit
    # é reenviado após espera, sem consumir uma das tentativas de preço.
    rate_limit_backoff_s: float = 1.2
    rate_limit_max_retries: int = 4


# Marcadores de rate limit na mensagem de erro da MEXC. O código 510 é o que
# o Futures devolve; o Spot usa 429/-1003. Casar por texto além do código
# porque o formato da exceção varia entre os dois clientes.
_RATE_LIMIT_MARKERS = (
    "too frequent", "rate limit", "too many requests",
    "error 510", "429", "-1003",
)


def is_rate_limited(exc: BaseException) -> bool:
    """
    Distingue "a exchange me barrou por frequência" de qualquer outro erro.

    A distinção importa porque as duas situações pedem reações opostas: um
    erro de validação não melhora com repetição, enquanto um rate limit
    resolve sozinho com uma pausa — e desistir dele é abandonar uma ordem que
    teria funcionado.
    """
    texto = str(exc).lower()
    codigo = getattr(exc, "code", None)
    if codigo in (510, 429, -1003, "510", "429", "-1003"):
        return True
    return any(m in texto for m in _RATE_LIMIT_MARKERS)


async def _send_respeitando_rate_limit(send, args: tuple, label: str, policy: "SlippagePolicy"):
    """
    Envia uma ordem reenviando-a quando (e só quando) a recusa for por
    frequência. Qualquer outro erro sobe imediatamente para quem chamou.
    """
    ultima_excecao = None
    for tentativa in range(policy.rate_limit_max_retries + 1):
        try:
            return await send(*args)
        except Exception as e:
            if not is_rate_limited(e):
                raise
            ultima_excecao = e
            espera = policy.rate_limit_backoff_s * (tentativa + 1)
            logger.warning(
                "[%s] Rate limit da MEXC (%s). Reenviando em %.1fs (%d/%d) — "
                "esta ordem NÃO foi criada na exchange.",
                label, e, espera, tentativa + 1, policy.rate_limit_max_retries,
            )
            await asyncio.sleep(espera)
    raise ultima_excecao


@dataclass
class LegFill:
    """Resultado agregado de uma perna, somando todas as tentativas."""
    filled_qty: float = 0.0
    notional: float = 0.0
    attempts: int = 0
    escalated: bool = False
    limit_price: float = 0.0
    requested_qty: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def avg_price(self) -> float:
        return self.notional / self.filled_qty if self.filled_qty > 0 else 0.0

    @property
    def complete(self) -> bool:
        # 0,5% de tolerância no "completo": a taxa cobrada na moeda base e o
        # arredondamento de precisão fazem a quantidade preenchida quase
        # nunca bater no último decimal com a pedida.
        return self.filled_qty >= self.requested_qty * 0.995 if self.requested_qty > 0 else False


def limit_price_for_buy(reference_vwap: float, policy: SlippagePolicy, tick: float) -> float:
    """
    Teto de preço de uma COMPRA: o VWAP esperado mais a tolerância.
    Arredondado para BAIXO no tick para nunca estourar o teto (ver
    `round_price_for_buy` em sizing.py).
    """
    raw = reference_vwap * (1 + policy.max_slippage_pct / 100)
    return round_price_for_buy(raw, tick)


def limit_price_for_sell(reference_vwap: float, policy: SlippagePolicy, tick: float) -> float:
    """
    Piso de preço de uma VENDA: o VWAP esperado menos a tolerância.
    Arredondado para CIMA no tick, pelo mesmo motivo espelhado.
    """
    raw = reference_vwap * (1 - policy.max_slippage_pct / 100)
    return round_price_for_sell(raw, tick)


# Assinatura dos callbacks injetados: recebem (quantidade, preço) e devolvem
# o preenchimento confirmado, ou None se a ordem não preencheu nada.
# Injetar em vez de chamar os clientes direto mantém toda a política de
# retry/escalonamento testável sem tocar na rede.
SendIOC = Callable[[float, float], Awaitable[Optional[dict]]]
SendMarket = Callable[[float], Awaitable[Optional[dict]]]
QtyCapProvider = Callable[[], Awaitable[Optional[float]]]


async def execute_bounded(
    *,
    label: str,
    total_qty: float,
    limit_price: float,
    send_ioc: SendIOC,
    send_market: Optional[SendMarket] = None,
    policy: Optional[SlippagePolicy] = None,
    qty_cap_provider: Optional[QtyCapProvider] = None,
    round_qty: Optional[Callable[[float], float]] = None,
) -> LegFill:
    """
    Executa `total_qty` respeitando `limit_price` como teto/piso absoluto.

    - `send_ioc(qty, price)`: envia uma ordem limite IOC já confirmada
      (deve devolver `{"filled_qty": float, "notional": float}` ou None).
    - `send_market(qty)`: usado só no escalonamento final, se a política
      permitir. Sem ele, nunca há escalonamento.
    - `qty_cap_provider()`: teto dinâmico de quantidade, reconsultado a
      cada tentativa. É por onde entra o saldo real disponível no Spot — a
      MEXC desconta a taxa na própria moeda comprada, então a quantidade
      vendável é sempre um pouco menor que a comprada, e tentar vender a
      quantidade "de livro" dispara `Insufficient position`.
    - `round_qty(qty)`: arredondamento de precisão do símbolo, aplicado a
      cada tentativa. Sempre para baixo, responsabilidade de quem passa.

    Nunca levanta exceção por falha de ordem: acumula os erros em
    `LegFill.errors` e devolve o que conseguiu preencher. Quem chama decide
    o que fazer com um preenchimento parcial ou nulo, porque essa decisão
    depende de ser entrada (dá para reverter) ou saída (não dá).
    """
    policy = policy or SlippagePolicy()
    result = LegFill(limit_price=limit_price, requested_qty=total_qty)

    if total_qty <= 0 or limit_price <= 0:
        return result

    for attempt in range(1, policy.max_attempts + 1):
        remaining = total_qty - result.filled_qty
        if remaining <= 0:
            break

        if qty_cap_provider is not None:
            try:
                cap = await qty_cap_provider()
                if cap is not None:
                    remaining = min(remaining, cap)
            except Exception as e:
                # Não conseguir consultar o teto não deve abortar a perna:
                # segue com a quantidade calculada, que é o comportamento
                # que já existia antes deste módulo.
                logger.warning("[%s] Falha ao consultar teto de quantidade: %s", label, e)

        if round_qty is not None:
            remaining = round_qty(remaining)

        if remaining <= 0:
            break

        result.attempts = attempt
        try:
            fill = await _send_respeitando_rate_limit(
                send_ioc, (remaining, limit_price), label, policy,
            )
        except Exception as e:
            result.errors.append(f"tentativa {attempt}: {e}")
            logger.warning("[%s] Tentativa %d (IOC @ %.10g) falhou: %s", label, attempt, limit_price, e)
            fill = None

        if fill:
            filled = float(fill.get("filled_qty", 0) or 0)
            notional = float(fill.get("notional", 0) or 0)
            if filled > 0:
                result.filled_qty += filled
                result.notional += notional
                logger.info(
                    "[%s] Tentativa %d preencheu %.10g @ %.10g (acumulado %.10g/%.10g)",
                    label, attempt, filled, notional / filled if filled else 0,
                    result.filled_qty, total_qty,
                )

        if result.complete:
            break

        if attempt < policy.max_attempts:
            await asyncio.sleep(policy.attempt_delay_s)

    if result.complete or send_market is None or not policy.escalate_to_market:
        if not result.complete:
            logger.warning(
                "[%s] Encerrado com preenchimento PARCIAL (%.10g de %.10g) e sem escalonamento a mercado.",
                label, result.filled_qty, total_qty,
            )
        return result

    # --- Escalonamento: o resíduo vai a mercado ---
    remaining = total_qty - result.filled_qty
    if qty_cap_provider is not None:
        try:
            cap = await qty_cap_provider()
            if cap is not None:
                remaining = min(remaining, cap)
        except Exception as e:
            logger.warning("[%s] Falha ao consultar teto antes do escalonamento: %s", label, e)
    if round_qty is not None:
        remaining = round_qty(remaining)

    if remaining <= 0:
        return result

    logger.warning(
        "[%s] Teto de slippage de %.2f%% não comportou %.10g de %.10g após %d tentativas. "
        "Escalonando o resíduo para ordem a MERCADO — o preço deste pedaço não tem teto. "
        "Se isso se repetir neste par, a tolerância está apertada demais para a liquidez dele.",
        label, policy.max_slippage_pct, remaining, total_qty, policy.max_attempts,
    )

    # O escalonamento é a REDE DE SEGURANÇA da operação: é ele que impede a
    # posição de terminar com uma perna só. Por isso ele nunca pode ser
    # abandonado por rate limit — em 05/08/2026 foi exatamente o que
    # aconteceu, e o resultado foi um short descoberto que o bot nem soube
    # que existia. Aqui a política de espera é mais insistente que a das
    # tentativas normais, porque desistir aqui custa muito mais caro.
    politica_escalonamento = SlippagePolicy(
        max_slippage_pct=policy.max_slippage_pct,
        rate_limit_backoff_s=max(policy.rate_limit_backoff_s, 1.5),
        rate_limit_max_retries=max(policy.rate_limit_max_retries, 6),
    )
    try:
        fill = await _send_respeitando_rate_limit(
            send_market, (remaining,), f"{label}/escalonamento", politica_escalonamento,
        )
    except Exception as e:
        result.errors.append(f"escalonamento a mercado: {e}")
        logger.critical(
            "[%s] ESCALONAMENTO A MERCADO FALHOU: %s. Restam %.10g de %.10g sem executar — "
            "a operação pode estar com uma perna aberta AGORA.",
            label, e, remaining, total_qty,
        )
        return result

    if fill:
        filled = float(fill.get("filled_qty", 0) or 0)
        notional = float(fill.get("notional", 0) or 0)
        if filled > 0:
            result.filled_qty += filled
            result.notional += notional
            result.escalated = True

    return result
