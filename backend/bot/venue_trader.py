"""
Execução por VENUE: a camada que traduz a estratégia para cada exchange.

## O problema

O `bot_engine` foi escrito quando existia um único caminho: comprar spot na
MEXC e vender futures na MEXC. Os códigos de lado (`side=3` para abrir short,
`side=2` para fechar), as unidades (contratos) e os formatos de símbolo
estavam embutidos na máquina de estados.

Com seis venues, cada um tem convenções próprias e incompatíveis:

    MEXC futures    side=3 abre short, side=2 fecha; quantidade em CONTRATOS
    Gate futures    sem campo side: o SINAL de `size` é a direção;
                    size<0 vende, size>0 compra; quantidade em CONTRATOS
    BingX swap      par (side, positionSide): SELL/SHORT abre, BUY/SHORT
                    fecha; quantidade na MOEDA BASE, não em contratos

Espalhar esses três dialetos pela máquina de estados garantiria que um deles
ficasse errado em algum caminho — e "errado" aqui significa uma ordem
executada na direção contrária, que DOBRA a exposição em vez de zerá-la.

## A abstração

Quatro operações, na linguagem da ESTRATÉGIA e não da exchange:

    open_buy_leg    entra na ponta comprada  (spot BUY ou futures LONG)
    close_buy_leg   desfaz a ponta comprada  (spot SELL ou futures CLOSE LONG)
    open_sell_leg   entra na ponta vendida   (futures SHORT — spot não pode)
    close_sell_leg  desfaz a ponta vendida   (futures CLOSE SHORT)

**Toda quantidade na interface é na MOEDA BASE.** A conversão para a unidade
nativa (contratos, quando for o caso) acontece dentro de cada implementação,
usando o `contract_size` do `ContractSpec`. Isso é o que permite ao bot casar
as pernas por quantidade — a decisão que corrigiu o descasamento direcional
de 1,4% do bug 11 — sem se perguntar em que unidade cada lado está.

## Nenhuma ordem sobrevive a esta camada

`run_leg` é o único ponto de entrada usado pelo motor, e ele fecha o ciclo de
vida da ordem antes de devolver um número:

    1. envia
    2. faz POLLING do status real até um estado TERMINAL (bug 6: a resposta
       síncrona do POST pode dizer `executedQty=0` numa ordem que executou)
    3. se não terminou sozinha, CANCELA e RELÊ (bug 17: a MEXC spot aceita e
       ignora `timeInForce=IOC`, e a ordem "IOC" fica viva no book)
    4. estado terminal COM quantidade preenchida é fill legítimo (bug 14: uma
       IOC parcial termina CANCELED com dinheiro real dentro)

O passo 3 vale para TODOS os venues, inclusive os que documentam IOC de
verdade (Gate e BingX aceitam `ioc` no spot; MEXC não). A sub-lição do bug 17
é que um parâmetro aceito não é um parâmetro honrado — a MEXC nunca rejeitou
o `timeInForce`, apenas o ignorou em silêncio. Cancelar antes de ler custa uma
chamada REST e remove a categoria inteira de erro.

## Unidades de ordem a MERCADO no spot: a assimetria que engana

Ordem a mercado só aparece no escalonamento e na emergência, mas é onde as
três exchanges mais divergem:

    MEXC spot   compra e venda em quantidade da MOEDA BASE
    BingX spot  compra e venda em quantidade da MOEDA BASE
    Gate spot   VENDA em moeda base, COMPRA em USDT  <-- assimétrico

Numa memecoin a 0,004 USDT, mandar "1000 unidades" para uma compra a mercado
da Gate gastaria 1000 USDT em vez de 4. Por isso `run_leg` exige `ref_price`
quando a ordem é a mercado num venue que precisa converter, e RECUSA em vez de
adivinhar o preço.

## Estado de validação

Só o caminho da MEXC foi exercitado contra a API real. Gate e BingX são
implementações a partir da documentação, cobertas por testes contra dublês.
`VALIDATED_VENUES` (em bot_engine) é a trava que impede um venue não validado
de virar ordem — ver os cabeçalhos de gate_client.py e bingx_client.py.
"""
import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from exchanges.base import ContractSpec, MarketType, Venue

logger = logging.getLogger("venue_trader")

# Quanto tempo uma ordem com preço pode ficar viva antes de ser cancelada à
# força. Curto o bastante para não virar uma ordem passiva esquecida (foi uma
# LIMIT esquecida que preencheu 33s depois e dobrou a posição em 09/08/2026),
# longo o bastante para o motor de matching processar o que dava para pegar.
DEFAULT_SETTLE_WAIT_S = 1.5

# Intervalo entre leituras de status durante a espera.
DEFAULT_POLL_INTERVAL_S = 0.3


class UnknownOrderStateError(RuntimeError):
    """
    Levantado quando o destino de uma ordem NÃO pôde ser determinado.

    É deliberadamente uma exceção, e não um "preencheu zero": tratar
    desconhecido como zero é exatamente o que produziu o incidente 17 (o bot
    concluiu que a compra não tinha acontecido, reverteu o hedge, e a compra
    aconteceu 33 segundos depois). Enquanto o estado for desconhecido, a única
    ação segura é parar e chamar o operador.
    """


@dataclass
class OrderRef:
    """Referência de uma ordem enviada, suficiente para consultar o fill."""
    venue: str
    symbol: str
    order_id: str
    raw: Optional[dict] = None


@dataclass
class OrderStatus:
    """
    Leitura do estado de uma ordem, sempre com quantidade na MOEDA BASE.

    `terminal` separa "a exchange já decidiu o destino desta ordem" de "ainda
    pode preencher mais". Sem essa distinção não há como saber se um
    `filled_qty` é um número final ou uma foto de um filme em andamento — e
    decidir em cima de uma foto foi o bug 17.
    """
    filled_qty: float
    notional: float
    terminal: bool


@dataclass
class Fill:
    """Preenchimento confirmado e DEFINITIVO, sempre na MOEDA BASE."""
    filled_qty: float
    notional: float

    @property
    def avg_price(self) -> float:
        return self.notional / self.filled_qty if self.filled_qty > 0 else 0.0


class VenueTrader(ABC):
    """
    Contrato de execução de um venue. Quem chama fala de pernas compradas e
    vendidas; a implementação traduz para os códigos da exchange.
    """

    #: venues em que a ordem a MERCADO de compra no spot é cobrada em USDT
    #: (e não na moeda base). Ver docstring do módulo.
    market_buy_uses_quote = False

    def __init__(self, venue: Venue, spec: ContractSpec, client,
                 *, settle_wait_s: float = DEFAULT_SETTLE_WAIT_S):
        self.venue = venue
        self.spec = spec
        self.client = client
        self.settle_wait_s = settle_wait_s
        # Ordem cujo destino não pôde ser lido. Enquanto existir, este trader
        # RECUSA enviar qualquer ordem nova: mandar outra ordem sem saber o
        # que a anterior fez é literalmente o mecanismo do bug 17.
        self._unknown: Optional[OrderRef] = None

    @property
    def supports_sell_leg(self) -> bool:
        """
        Só futures pode abrir a ponta vendida: não se vende spot a descoberto
        sem margem, e este projeto não usa conta de margem.
        """
        return self.venue.market == MarketType.FUTURES

    # -- conversões comuns --

    def to_native_qty(self, base_qty: float) -> float:
        """Moeda base -> unidade nativa do venue (contratos quando aplicável)."""
        if self.spec.contract_size and self.spec.contract_size > 0:
            return base_qty / self.spec.contract_size
        return base_qty

    def to_base_qty(self, native_qty: float) -> float:
        return native_qty * (self.spec.contract_size or 1.0)

    def round_qty(self, base_qty: float) -> float:
        """
        Arredonda SEMPRE para baixo, na unidade nativa.

        Para baixo porque arredondar para cima pode exceder o saldo (venda) ou
        a exposição decidida (compra); e na unidade NATIVA porque é ela que a
        exchange valida — arredondar em moeda base e converter depois
        reintroduz a imprecisão que o arredondamento existia para remover.
        """
        nativa = self.to_native_qty(base_qty)
        passo = self.spec.qty_step or 0
        if passo > 0:
            nativa = math.floor(nativa / passo + 1e-9) * passo
        else:
            nativa = math.floor(nativa)
        if self.spec.min_qty and nativa < self.spec.min_qty:
            return 0.0
        return self.to_base_qty(nativa)

    def round_price(self, price: float, *, up: bool) -> float:
        tick = self.spec.price_tick or 0
        if tick <= 0 or price <= 0:
            return price
        ticks = price / tick
        ticks = math.ceil(ticks - 1e-9) if up else math.floor(ticks + 1e-9)
        casas = max(0, min(12, -math.floor(math.log10(tick)) if tick < 1 else 0))
        return round(ticks * tick, casas)

    # -- operações da estratégia (dialeto de cada exchange) --

    @abstractmethod
    async def open_buy_leg(self, base_qty: float, price: Optional[float],
                           ref_price: Optional[float] = None) -> OrderRef: ...

    @abstractmethod
    async def close_buy_leg(self, base_qty: float, price: Optional[float],
                            ref_price: Optional[float] = None) -> OrderRef: ...

    @abstractmethod
    async def open_sell_leg(self, base_qty: float, price: Optional[float],
                            ref_price: Optional[float] = None) -> OrderRef: ...

    @abstractmethod
    async def close_sell_leg(self, base_qty: float, price: Optional[float],
                             ref_price: Optional[float] = None) -> OrderRef: ...

    @abstractmethod
    async def fetch_status(self, ref: OrderRef) -> Optional[OrderStatus]:
        """Estado atual da ordem, ou None se não foi possível ler."""

    @abstractmethod
    async def cancel_order(self, ref: OrderRef) -> None: ...

    @abstractmethod
    async def free_balance(self, asset: str) -> Optional[float]: ...

    @abstractmethod
    async def describe_position(self) -> str: ...

    @abstractmethod
    async def cancel_all(self) -> None: ...

    # -- ciclo de vida: o que o motor realmente chama --

    async def run_leg(
        self, operation: str, base_qty: float, price: Optional[float],
        *, ref_price: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Envia UMA ordem e devolve o preenchimento DEFINITIVO no formato que
        `execute_bounded` espera (`{"filled_qty", "notional"}` na moeda base),
        ou None se comprovadamente não preencheu nada.

        `operation` é uma das quatro da estratégia. `price=None` significa
        ordem a mercado — reservada ao escalonamento, ao kill switch e à
        reversão de perna órfã, nunca ao caminho normal.

        Levanta `UnknownOrderStateError` quando o destino da ordem não pôde
        ser determinado. Isso é intencional: sem saber o que a ordem fez, tanto
        "assumir que preencheu" quanto "assumir que não preencheu" podem
        descasar as pernas, e a segunda opção já custou dinheiro real.
        """
        if self._unknown is not None:
            raise UnknownOrderStateError(
                f"{self.venue.key}: há uma ordem de destino desconhecido "
                f"({self._unknown.order_id}) em {self._unknown.symbol}. Nenhuma ordem nova "
                "será enviada até que ela seja conferida MANUALMENTE na exchange."
            )

        if operation not in ("open_buy_leg", "close_buy_leg", "open_sell_leg", "close_sell_leg"):
            raise ValueError(f"Operação desconhecida: {operation}")

        if price is None and self.market_buy_uses_quote and operation == "open_buy_leg" \
                and not ref_price:
            # Recusar é a única saída correta: sem preço de referência não há
            # como converter moeda base -> USDT, e chutar o valor de uma ordem
            # a mercado é o oposto de tudo que este projeto faz.
            raise ValueError(
                f"{self.venue.key}: compra a mercado exige `ref_price` para converter "
                "a quantidade em valor (esta exchange cobra a compra a mercado em USDT)."
            )

        ref = await getattr(self, operation)(base_qty, price, ref_price)
        if not ref.order_id or ref.order_id in ("None", ""):
            # Sem id não há como confirmar nem cancelar. Tratar como "não
            # preencheu" seria uma suposição sobre uma ordem que pode ter sido
            # criada — a mesma classe de erro do bug 17.
            self._unknown = OrderRef(self.venue.key, self.spec.native_symbol, "<sem id>", ref.raw)
            raise UnknownOrderStateError(
                f"{self.venue.key}: a exchange não devolveu id da ordem em "
                f"{self.spec.native_symbol}. Resposta: {ref.raw}"
            )

        fill = await self.settle(ref)
        if fill.filled_qty <= 0:
            return None
        return {"filled_qty": fill.filled_qty, "notional": fill.notional}

    async def settle(self, ref: OrderRef, *, wait_s: Optional[float] = None) -> Fill:
        """
        Fecha o ciclo de vida da ordem e devolve um número definitivo.

        O cancelamento é PARTE da primitiva, não limpeza: enquanto a ordem
        existir, a quantidade preenchida ainda pode crescer, e qualquer decisão
        tomada com o valor lido antes disso pode ser invalidada segundos depois.
        """
        limite = time.time() + (self.settle_wait_s if wait_s is None else wait_s)
        ultimo: Optional[OrderStatus] = None

        while time.time() < limite:
            ultimo = await self._status_tolerante(ref)
            # Estado terminal COM quantidade é fill legítimo: uma IOC parcial
            # termina CANCELED/state=4 com dinheiro real dentro (bug 14).
            if ultimo is not None and ultimo.terminal:
                return Fill(ultimo.filled_qty, ultimo.notional)
            await asyncio.sleep(DEFAULT_POLL_INTERVAL_S)

        # Não terminou sozinha. Cancelar CONGELA o número; sem isso, ler é ler
        # um filme em andamento.
        try:
            await self.cancel_order(ref)
        except Exception as e:
            # Cancelar ordem já finalizada dá erro, e isso é um desfecho
            # normal — a releitura abaixo é que decide.
            logger.info("[%s] Cancelamento de %s: %s", self.venue.key, ref.order_id, e)

        final = await self._status_tolerante(ref)
        if final is not None:
            if not final.terminal:
                logger.warning(
                    "[%s] Ordem %s segue NÃO-terminal após o cancelamento (preenchido %.10g). "
                    "Usando o valor lido, mas confira na exchange.",
                    self.venue.key, ref.order_id, final.filled_qty,
                )
            return Fill(final.filled_qty, final.notional)

        # Não foi possível ler o estado final. Nunca devolver zero aqui.
        self._unknown = ref
        logger.critical(
            "[%s] CRÍTICO: não foi possível determinar o destino da ordem %s em %s. "
            "Pode haver posição não contabilizada. VERIFIQUE MANUALMENTE.",
            self.venue.key, ref.order_id, ref.symbol,
        )
        raise UnknownOrderStateError(
            f"{self.venue.key}: estado final da ordem {ref.order_id} em {ref.symbol} "
            "não pôde ser lido após o cancelamento."
        )

    async def _status_tolerante(self, ref: OrderRef) -> Optional[OrderStatus]:
        """Lê o status engolindo falha de rede — quem decide é `settle`."""
        try:
            return await self.fetch_status(ref)
        except Exception as e:
            logger.warning("[%s] Falha ao ler status de %s: %s", self.venue.key, ref.order_id, e)
            return None

    @property
    def has_unknown_order(self) -> bool:
        return self._unknown is not None

    def clear_unknown_order(self) -> None:
        """Liberação MANUAL, depois de o operador conferir a exchange."""
        self._unknown = None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class GateSpotTrader(VenueTrader):
    # A Gate cobra a COMPRA a mercado em USDT e a VENDA em moeda base.
    market_buy_uses_quote = True

    async def open_buy_leg(self, base_qty, price, ref_price=None):
        if price is not None:
            r = await self.client.spot_limit_ioc(self.spec.native_symbol, "buy", base_qty, price)
        else:
            # `ref_price` já foi exigido em `run_leg`. Uma folga de 1% cobre o
            # book andar entre a decisão e a ordem: faltar saldo cancelaria a
            # compra inteira, e sobrar USDT não custa nada.
            r = await self.client.spot_market_buy(self.spec.native_symbol, base_qty * ref_price * 1.01)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("id") or ""), r)

    async def close_buy_leg(self, base_qty, price, ref_price=None):
        r = await (self.client.spot_limit_ioc(self.spec.native_symbol, "sell", base_qty, price)
                   if price is not None
                   else self.client.spot_market_sell(self.spec.native_symbol, base_qty))
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("id") or ""), r)

    async def open_sell_leg(self, base_qty, price, ref_price=None):
        raise NotImplementedError("Spot não abre posição vendida — use um venue de futures.")

    async def close_sell_leg(self, base_qty, price, ref_price=None):
        raise NotImplementedError("Spot não tem posição vendida a fechar.")

    async def fetch_status(self, ref):
        d = await self.client.get_spot_order(ref.order_id, ref.symbol)
        preenchido = float(d.get("amount", 0) or 0) - float(d.get("left", 0) or 0)
        # `filled_total` é o valor em quote já executado; é o que dá o preço
        # médio real sem depender do preço-limite pedido.
        notional = float(d.get("filled_total", 0) or 0)
        if notional <= 0 and preenchido > 0:
            notional = preenchido * float(d.get("price", 0) or 0)
        estado = str(d.get("status", "")).lower()
        return OrderStatus(max(preenchido, 0.0), notional, estado != "open")

    async def cancel_order(self, ref):
        await self.client.cancel_spot_order(ref.order_id, ref.symbol)

    async def free_balance(self, asset):
        return (await self.client.get_spot_balance(asset))["free"]

    async def describe_position(self):
        return "spot não mantém posição; confira o saldo do ativo."

    async def cancel_all(self):
        await self.client.cancel_all_spot(self.spec.native_symbol)


class GateFuturesTrader(VenueTrader):
    async def open_buy_leg(self, base_qty, price, ref_price=None):
        contratos = int(self.to_native_qty(base_qty))
        r = await self.client.futures_order(self.spec.native_symbol, abs(contratos), price)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("id") or ""), r)

    async def close_buy_leg(self, base_qty, price, ref_price=None):
        contratos = int(self.to_native_qty(base_qty))
        r = await self.client.futures_order(
            self.spec.native_symbol, -abs(contratos), price, reduce_only=True)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("id") or ""), r)

    async def open_sell_leg(self, base_qty, price, ref_price=None):
        contratos = int(self.to_native_qty(base_qty))
        r = await self.client.open_short(self.spec.native_symbol, contratos, price)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("id") or ""), r)

    async def close_sell_leg(self, base_qty, price, ref_price=None):
        contratos = int(self.to_native_qty(base_qty))
        r = await self.client.close_short(self.spec.native_symbol, contratos, price)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("id") or ""), r)

    async def fetch_status(self, ref):
        d = await self.client.get_futures_order(ref.order_id)
        # `size` é o pedido (com sinal) e `left` o que restou; a diferença em
        # valor absoluto é o preenchido, independente da direção.
        preenchido = abs(float(d.get("size", 0) or 0)) - abs(float(d.get("left", 0) or 0))
        preco = float(d.get("fill_price", 0) or d.get("avg_deal_price", 0) or 0)
        base = self.to_base_qty(max(preenchido, 0.0))
        # Na Gate o status de futures é "open" ou "finished"; `finish_as` diz
        # o motivo (filled, cancelled, ioc...), que aqui não muda a decisão.
        terminal = str(d.get("status", "")).lower() != "open"
        return OrderStatus(base, base * preco, terminal)

    async def cancel_order(self, ref):
        await self.client.cancel_futures_order(ref.order_id)

    async def free_balance(self, asset):
        return (await self.client.get_futures_balance())["available"]

    async def describe_position(self):
        try:
            posicoes = await self.client.get_open_positions(self.spec.native_symbol)
        except Exception as e:
            return f"não foi possível confirmar na Gate ({e})"
        if not posicoes:
            return "a Gate não reporta posição aberta neste contrato."
        return "a Gate reporta POSIÇÃO ABERTA: " + "; ".join(
            f"{p.get('size')} contratos (entrada {p.get('entry_price')})" for p in posicoes
        )

    async def cancel_all(self):
        await self.client.cancel_all_futures(self.spec.native_symbol)


# ---------------------------------------------------------------------------
# BingX
# ---------------------------------------------------------------------------

class BingxSpotTrader(VenueTrader):
    async def open_buy_leg(self, base_qty, price, ref_price=None):
        r = await (self.client.spot_limit_ioc(self.spec.native_symbol, "BUY", base_qty, price)
                   if price is not None
                   else self.client.spot_market(self.spec.native_symbol, "BUY", base_qty))
        return OrderRef(self.venue.key, self.spec.native_symbol, str(_bingx_id(r) or ""), r)

    async def close_buy_leg(self, base_qty, price, ref_price=None):
        r = await (self.client.spot_limit_ioc(self.spec.native_symbol, "SELL", base_qty, price)
                   if price is not None
                   else self.client.spot_market(self.spec.native_symbol, "SELL", base_qty))
        return OrderRef(self.venue.key, self.spec.native_symbol, str(_bingx_id(r) or ""), r)

    async def open_sell_leg(self, base_qty, price, ref_price=None):
        raise NotImplementedError("Spot não abre posição vendida — use um venue de futures.")

    async def close_sell_leg(self, base_qty, price, ref_price=None):
        raise NotImplementedError("Spot não tem posição vendida a fechar.")

    async def fetch_status(self, ref):
        d = await self.client.get_spot_order(ref.symbol, ref.order_id)
        preenchido = float(d.get("executedQty", 0) or 0)
        notional = float(d.get("cummulativeQuoteQty", 0) or 0)
        if notional <= 0 and preenchido > 0:
            notional = preenchido * float(d.get("price", 0) or 0)
        estado = str(d.get("status", "")).upper()
        return OrderStatus(preenchido, notional, estado in _ESTADOS_TERMINAIS_BINANCE_LIKE)

    async def cancel_order(self, ref):
        await self.client.cancel_spot_order(ref.symbol, ref.order_id)

    async def free_balance(self, asset):
        return (await self.client.get_spot_balance(asset))["free"]

    async def describe_position(self):
        return "spot não mantém posição; confira o saldo do ativo."

    async def cancel_all(self):
        await self.client.cancel_all_spot(self.spec.native_symbol)


class BingxFuturesTrader(VenueTrader):
    """
    Na BingX a quantidade das ordens de swap já é na MOEDA BASE, então
    `contract_size` é 1,0 e não há conversão — mas as conversões da classe
    base continuam sendo usadas para o arredondamento de passo.
    """

    async def open_buy_leg(self, base_qty, price, ref_price=None):
        r = await self.client.swap_order(self.spec.native_symbol, "BUY", "LONG", base_qty, price)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(_bingx_id(r) or ""), r)

    async def close_buy_leg(self, base_qty, price, ref_price=None):
        r = await self.client.swap_order(self.spec.native_symbol, "SELL", "LONG", base_qty, price)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(_bingx_id(r) or ""), r)

    async def open_sell_leg(self, base_qty, price, ref_price=None):
        r = await self.client.open_short(self.spec.native_symbol, base_qty, price)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(_bingx_id(r) or ""), r)

    async def close_sell_leg(self, base_qty, price, ref_price=None):
        r = await self.client.close_short(self.spec.native_symbol, base_qty, price)
        return OrderRef(self.venue.key, self.spec.native_symbol, str(_bingx_id(r) or ""), r)

    async def fetch_status(self, ref):
        d = await self.client.get_swap_order(ref.symbol, ref.order_id)
        ordem = d.get("order", d) if isinstance(d, dict) else {}
        preenchido = float(ordem.get("executedQty", 0) or 0)
        preco = float(ordem.get("avgPrice", 0) or 0)
        estado = str(ordem.get("status", "")).upper()
        return OrderStatus(preenchido, preenchido * preco,
                           estado in _ESTADOS_TERMINAIS_BINANCE_LIKE)

    async def cancel_order(self, ref):
        await self.client.cancel_swap_order(ref.symbol, ref.order_id)

    async def free_balance(self, asset):
        return (await self.client.get_futures_balance())["available"]

    async def describe_position(self):
        try:
            posicoes = await self.client.get_open_positions(self.spec.native_symbol)
        except Exception as e:
            return f"não foi possível confirmar na BingX ({e})"
        if not posicoes:
            return "a BingX não reporta posição aberta neste contrato."
        return "a BingX reporta POSIÇÃO ABERTA: " + "; ".join(
            f"{p.get('positionSide')} de {p.get('positionAmt')} (entrada {p.get('avgPrice')})"
            for p in posicoes
        )

    async def cancel_all(self):
        await self.client.cancel_all_swap(self.spec.native_symbol)


# Estados finais no vocabulário herdado da Binance, que BingX e MEXC spot
# usam. `CANCELED` entra na lista COM preenchimento possível dentro: é assim
# que termina uma IOC parcial (bug 14).
_ESTADOS_TERMINAIS_BINANCE_LIKE = frozenset({
    "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "PARTIALLY_CANCELED",
})


def _bingx_id(resposta) -> str:
    """A BingX aninha o id em `data.order.orderId` em algumas respostas de swap."""
    if not isinstance(resposta, dict):
        return ""
    ordem = resposta.get("order")
    if isinstance(ordem, dict) and ordem.get("orderId"):
        return str(ordem["orderId"])
    return str(resposta.get("orderId", "") or "")


# ---------------------------------------------------------------------------
# MEXC (embrulha os clientes já existentes e testados em produção)
# ---------------------------------------------------------------------------

class MexcSpotTrader(VenueTrader):
    """
    ATENÇÃO: a MEXC spot NÃO tem IOC. O `orderTypes` do exchangeInfo lista
    apenas LIMIT, MARKET e LIMIT_MAKER, e `timeInForce` é aceito e IGNORADO.

    Por isso o caminho com preço usa `new_order_limit` (GTC honesta) e depende
    do `settle` da classe base para cancelar e reler — que é exatamente o
    `limit_then_cancel` do cliente, agora generalizado para todos os venues.
    """

    async def open_buy_leg(self, base_qty, price, ref_price=None):
        r = await (self.client.new_order_limit(self.spec.native_symbol, "BUY", base_qty, price)
                   if price is not None
                   else self.client.new_order_market_by_qty(self.spec.native_symbol, "BUY", base_qty))
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("orderId") or ""), r)

    async def close_buy_leg(self, base_qty, price, ref_price=None):
        r = await (self.client.new_order_limit(self.spec.native_symbol, "SELL", base_qty, price)
                   if price is not None
                   else self.client.new_order_market_by_qty(self.spec.native_symbol, "SELL", base_qty))
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("orderId") or ""), r)

    async def open_sell_leg(self, base_qty, price, ref_price=None):
        raise NotImplementedError("Spot não abre posição vendida — use um venue de futures.")

    async def close_sell_leg(self, base_qty, price, ref_price=None):
        raise NotImplementedError("Spot não tem posição vendida a fechar.")

    async def fetch_status(self, ref):
        d = await self.client.get_order(ref.symbol, ref.order_id)
        preenchido = float(d.get("executedQty", 0) or 0)
        quote = float(d.get("cummulativeQuoteQty", 0) or 0)
        estado = str(d.get("status", "")).upper()
        return OrderStatus(preenchido, quote, estado in _ESTADOS_TERMINAIS_BINANCE_LIKE)

    async def cancel_order(self, ref):
        await self.client.cancel_order(ref.symbol, ref.order_id)

    async def free_balance(self, asset):
        return (await self.client.get_balance(asset))["free"]

    async def describe_position(self):
        return "spot não mantém posição; confira o saldo do ativo."

    async def cancel_all(self):
        await self.client.cancel_all_open_orders(self.spec.native_symbol)


class MexcFuturesTrader(VenueTrader):
    # side: 1 abre long, 2 fecha short, 3 abre short, 4 fecha long
    async def open_buy_leg(self, base_qty, price, ref_price=None):
        return await self._enviar(1, base_qty, price)

    async def close_buy_leg(self, base_qty, price, ref_price=None):
        return await self._enviar(4, base_qty, price, reduce_only=True)

    async def open_sell_leg(self, base_qty, price, ref_price=None):
        return await self._enviar(3, base_qty, price)

    async def close_sell_leg(self, base_qty, price, ref_price=None):
        return await self._enviar(2, base_qty, price, reduce_only=True)

    async def _enviar(self, side, base_qty, price, reduce_only=False):
        contratos = int(self.to_native_qty(base_qty))
        r = await self.client.submit_order(
            symbol=self.spec.native_symbol, side=side, vol=contratos,
            order_type=3 if price is not None else 5,   # 3 = IOC, 5 = mercado
            price=price, leverage=1, open_type=1, reduce_only=reduce_only,
        )
        return OrderRef(self.venue.key, self.spec.native_symbol, str(r.get("data") or ""), r)

    async def fetch_status(self, ref):
        s = await self.client.get_order(ref.order_id)
        d = s.get("data", {}) or {}
        vol = float(d.get("dealVol", 0) or 0)
        preco = float(d.get("dealAvgPrice", 0) or 0)
        base = self.to_base_qty(vol)
        # state 1/2 = viva; 3 = concluída, 4 = cancelada, 5 = inválida.
        # Cancelada COM dealVol > 0 é uma IOC parcial: fill real (bug 14).
        return OrderStatus(base, base * preco, d.get("state") in (3, 4, 5))

    async def cancel_order(self, ref):
        await self.client.cancel_order([ref.order_id])

    async def free_balance(self, asset):
        ativos = await self.client.get_assets()
        for a in ativos.get("data", []):
            if a.get("currency") == asset:
                return float(a.get("availableBalance", 0) or 0)
        return None

    async def describe_position(self):
        try:
            r = await self.client.get_open_positions(self.spec.native_symbol)
        except Exception as e:
            return f"não foi possível confirmar na MEXC ({e})"
        dados = r.get("data") or []
        if not dados:
            return "a MEXC não reporta posição aberta neste contrato."
        return "a MEXC reporta POSIÇÃO ABERTA: " + "; ".join(
            f"{'SHORT' if p.get('positionType') == 2 else 'LONG'} de {p.get('holdVol')} contratos"
            for p in dados
        )

    async def cancel_all(self):
        await self.client.cancel_all_orders(self.spec.native_symbol)


TRADER_CLASSES = {
    "mexc:spot": MexcSpotTrader,
    "mexc:futures": MexcFuturesTrader,
    "gate:spot": GateSpotTrader,
    "gate:futures": GateFuturesTrader,
    "bingx:spot": BingxSpotTrader,
    "bingx:futures": BingxFuturesTrader,
}


def build_trader(venue: Venue, spec: ContractSpec, client, **kwargs) -> VenueTrader:
    """
    Instancia o executor do venue. Adicionar uma exchange é acrescentar a
    classe aqui — nada mais no bot precisa saber o nome dela.
    """
    cls = TRADER_CLASSES.get(venue.key)
    if cls is None:
        raise ValueError(f"Sem executor implementado para o venue {venue.key}")
    return cls(venue, spec, client, **kwargs)
