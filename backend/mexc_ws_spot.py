"""
WebSocket público de Spot da MEXC (canal bookTicker, protobuf).

Este cliente inclui uma camada de VALIDAÇÃO CRUZADA automática: por
padrão, todo preço recebido via WebSocket só é aceito e repassado ao
callback depois de ter sido comparado contra uma consulta REST do mesmo
símbolo (fonte que já sabemos ser confiável, usada desde a Fase 1). Se a
diferença entre WS e REST ultrapassar uma tolerância, o WS é considerado
não confiável para aquele símbolo e o engine deve continuar usando REST
como fallback - nunca silenciosamente aceitar um preço suspeito.

Isso é uma proteção deliberada dado que o parsing de protobuf não pôde
ser validado contra o servidor real da MEXC durante o desenvolvimento
(sandbox sem acesso de rede à MEXC) - ver mexc_protobuf_decoder.py para
mais contexto sobre essa decisão.
"""
import asyncio
import json
import logging
import time

import websockets

from config import WS_RECONNECT_DELAY
from bot.mexc_protobuf_decoder import decode_book_ticker_push

logger = logging.getLogger("mexc_ws_spot")

SPOT_WS_URL = "wss://wbs-api.mexc.com/ws"

VALIDATION_SAMPLES_REQUIRED = 5
VALIDATION_TOLERANCE_PCT = 0.5
PERIODIC_REVALIDATION_INTERVAL = 60

# Limite de subscrições por conexão imposto pela MEXC (200). Usamos uma
# margem de segurança para não esbarrar exatamente no teto.
MAX_SUBSCRIPTIONS = 180


class SpotBookTickerWebSocketClient:
    def __init__(self, on_book_ticker_update, on_validation_status_change=None):
        self.on_book_ticker_update = on_book_ticker_update
        self.on_validation_status_change = on_validation_status_change
        self._stop = False
        self.connected = False
        self._subscribed_symbols: set[str] = set()
        # Símbolos prioritários (pares do bot) - vaga garantida
        self._priority_symbols: set[str] = set()
        # Demais símbolos, ocupam só as vagas restantes
        self._candidate_symbols: set[str] = set()
        # Modo foco: quando ativo, SÓ os prioritários são subscritos.
        # Reduz drasticamente o volume de mensagens processadas, deixando
        # toda a capacidade para os pares que o bot está operando.
        self._focus_mode: bool = False
        self._ws = None

        self._validation_samples: dict[str, int] = {}
        self._trusted_symbols: set[str] = set()
        self._last_revalidation: dict[str, float] = {}
        self._last_rest_price: dict[str, float] = {}

    def update_rest_reference(self, symbol: str, price: float):
        self._last_rest_price[symbol] = price

    def is_trusted(self, symbol: str) -> bool:
        return symbol in self._trusted_symbols

    async def set_priority_symbols(self, symbols: list[str]):
        """
        Define os símbolos PRIORITÁRIOS (tipicamente os configurados no bot).
        Eles sempre têm vaga garantida na subscrição do WebSocket, mesmo que
        isso signifique remover símbolos comuns para caber no limite.
        """
        self._priority_symbols = set(symbols)
        await self._resubscribe()

    async def subscribe(self, symbols: list[str]):
        """
        Registra símbolos comuns (não-prioritários) para subscrição. Eles
        só ocupam as vagas que sobrarem depois dos prioritários.
        """
        self._candidate_symbols.update(symbols)
        await self._resubscribe()

    async def set_focus_mode(self, enabled: bool):
        """
        Liga/desliga o modo foco. Com foco ativo, apenas os símbolos
        prioritários (pares do bot) ficam subscritos - todos os outros são
        removidos da conexão. Isso reduz o volume de mensagens processadas
        de centenas por segundo para apenas as dos pares operados,
        minimizando latência quando o bot está rodando.
        """
        if self._focus_mode == enabled:
            return
        self._focus_mode = enabled
        logger.info(
            "Modo foco do WS de spot %s%s",
            "ATIVADO" if enabled else "desativado",
            f" ({len(self._priority_symbols)} pares do bot)" if enabled else " (voltando a monitorar todos os pares)",
        )
        await self._resubscribe()

    def _compute_active_subscriptions(self) -> set[str]:
        """
        Decide quais símbolos ficam efetivamente subscritos, respeitando o
        limite da MEXC (200 por conexão). Prioritários primeiro; o resto das
        vagas é preenchido com os demais símbolos.

        Sem esse controle, o código subscrevia todos os pares descobertos
        (podendo passar de 500), e as subscrições excedentes eram rejeitadas
        pela MEXC - possivelmente derrubando a conexão inteira.

        Em modo foco, só os prioritários entram.
        """
        active = set(list(self._priority_symbols)[:MAX_SUBSCRIPTIONS])

        if self._focus_mode:
            return active

        remaining = MAX_SUBSCRIPTIONS - len(active)
        if remaining > 0:
            for sym in self._candidate_symbols:
                if sym in active:
                    continue
                active.add(sym)
                remaining -= 1
                if remaining <= 0:
                    break
        return active

    async def _resubscribe(self):
        """Aplica a lista de subscrições ativas na conexão atual (se houver)."""
        desired = self._compute_active_subscriptions()
        to_add = desired - self._subscribed_symbols
        to_remove = self._subscribed_symbols - desired

        if not self._ws or not self.connected:
            # Sem conexão ativa: só registra o desejado, aplicado ao conectar.
            self._subscribed_symbols = desired
            return

        try:
            if to_remove:
                params = [f"spot@public.bookTicker.v3.api.pb@{s}" for s in to_remove]
                await self._ws.send(json.dumps({"method": "UNSUBSCRIPTION", "params": params}))
            if to_add:
                params = [f"spot@public.bookTicker.v3.api.pb@{s}" for s in to_add]
                await self._ws.send(json.dumps({"method": "SUBSCRIPTION", "params": params}))
            self._subscribed_symbols = desired
        except Exception as e:
            logger.warning("Falha ao ajustar subscrições do WS de spot: %s", e)

    async def run(self):
        while not self._stop:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.warning("Spot WS (bookTicker) caiu, reconectando: %s", e)
                self.connected = False
                self._ws = None
                await asyncio.sleep(WS_RECONNECT_DELAY)

    async def stop(self):
        self._stop = True

    async def _connect_and_listen(self):
        extra_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        async with websockets.connect(
            SPOT_WS_URL, ping_interval=None, additional_headers=extra_headers
        ) as ws:
            logger.info("Conectado ao WS de Spot (bookTicker) da MEXC")
            self.connected = True
            self._ws = ws

            # Aplica a lista computada (respeita prioridade, modo foco e o
            # limite de subscrições da MEXC) em vez de mandar tudo que já
            # foi registrado alguma vez.
            desired = self._compute_active_subscriptions()
            if desired:
                params = [
                    f"spot@public.bookTicker.v3.api.pb@{sym}"
                    for sym in desired
                ]
                await ws.send(json.dumps({"method": "SUBSCRIPTION", "params": params}))
                self._subscribed_symbols = desired
                logger.info(
                    "WS de spot subscrito em %d símbolos%s",
                    len(desired), " (modo foco)" if self._focus_mode else "",
                )

            last_ping = time.time()

            while not self._stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    if time.time() - last_ping > 10:
                        await ws.send(json.dumps({"method": "PING"}))
                        last_ping = time.time()
                    continue

                if isinstance(raw, str):
                    continue

                await self._handle_binary_message(raw)

    async def _handle_binary_message(self, raw: bytes):
        decoded = decode_book_ticker_push(raw)
        if decoded is None:
            return

        symbol = decoded.get("symbol")
        bid = decoded["bid_price"]
        ask = decoded["ask_price"]

        if not symbol:
            return

        # A perna Spot da estratégia é sempre COMPRA, então o preço de
        # referência é o ASK (melhor venda do book) - é o que efetivamente
        # se paga ao comprar a mercado. É também o mesmo campo usado pelo
        # REST (fetch_spot_tickers), garantindo que a validação cruzada
        # compare a mesma grandeza dos dois lados.
        reference_price = ask

        if symbol not in self._trusted_symbols:
            await self._validate_sample(symbol, reference_price)
            if symbol not in self._trusted_symbols:
                return
        else:
            last_val = self._last_revalidation.get(symbol, 0)
            if time.time() - last_val > PERIODIC_REVALIDATION_INTERVAL:
                await self._validate_sample(symbol, reference_price, is_revalidation=True)

        await self.on_book_ticker_update(symbol, bid, ask)

    async def _validate_sample(self, symbol: str, ws_price: float, is_revalidation: bool = False):
        rest_price = self._last_rest_price.get(symbol)
        if rest_price is None or rest_price <= 0:
            return

        diff_pct = abs(ws_price - rest_price) / rest_price * 100

        if diff_pct > VALIDATION_TOLERANCE_PCT:
            logger.warning(
                "Validação do WS de Spot falhou para %s: preço WS=%.8f vs REST=%.8f "
                "(diferença %.2f%%, tolerância %.2f%%). Símbolo NÃO será considerado confiável.",
                symbol, ws_price, rest_price, diff_pct, VALIDATION_TOLERANCE_PCT,
            )
            was_trusted = symbol in self._trusted_symbols
            self._trusted_symbols.discard(symbol)
            self._validation_samples[symbol] = 0
            if was_trusted and self.on_validation_status_change:
                await self.on_validation_status_change(symbol, False)
            return

        self._last_revalidation[symbol] = time.time()

        if is_revalidation:
            return

        count = self._validation_samples.get(symbol, 0) + 1
        self._validation_samples[symbol] = count

        if count >= VALIDATION_SAMPLES_REQUIRED:
            self._trusted_symbols.add(symbol)
            logger.info(
                "WS de Spot validado com sucesso para %s (%d amostras concordantes com REST). "
                "Passará a ser usado como fonte de preço.", symbol, count,
            )
            if self.on_validation_status_change:
                await self.on_validation_status_change(symbol, True)
