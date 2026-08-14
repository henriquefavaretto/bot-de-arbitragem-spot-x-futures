"""
Cliente WebSocket para os canais públicos de futures da MEXC.

Usa DOIS canais complementares:

1. `sub.tickers` (agregado, todos os contratos, push a cada 2s): serve de
   base para o Dashboard mostrar todos os pares. Limitação importante: o
   payload deste canal NÃO inclui bid1/ask1 (confirmado na documentação
   oficial - só traz lastPrice, fairPrice, maxBidPrice/minAskPrice, sendo
   que estes dois últimos são limites de preço permitidos para ordens, não
   o topo do book). Por isso, para os pares que vêm apenas por aqui, o
   preço usado é o lastPrice.

2. `sub.ticker` (individual, por símbolo, push a cada 1s quando há
   negócios): este SIM inclui bid1/ask1. É usado para os pares
   "prioritários" - tipicamente os que o bot está operando - que precisam
   do preço de execução real (bid, o que se recebe ao vender) e da menor
   latência possível.

O limite de subscrições por conexão é de 200 (não 30, como versões
antigas da doc indicavam), então subscrever individualmente os pares
configurados no bot está bem dentro do orçamento.

Doc: wss://contract.mexc.com/edge
"""
import asyncio
import json
import logging
import time

import websockets

from config import MEXC_FUTURES_WS_URL, WS_RECONNECT_DELAY

logger = logging.getLogger("mexc_ws_futures")

MAX_INDIVIDUAL_SUBSCRIPTIONS = 180  # margem de segurança abaixo do limite de 200


class FuturesWebSocketClient:
    def __init__(self, on_tickers_update):
        """
        on_tickers_update: async callback(dict) chamado com
            {symbol: {"price", "bid", "ask", "last", "vol"}} a cada push.
        """
        self.on_tickers_update = on_tickers_update
        self._stop = False
        self.connected = False
        self._ws = None
        # Símbolos com subscrição individual (bid/ask real, 1s de latência)
        self._priority_symbols: set[str] = set()
        # Modo foco: ignora o canal agregado, processando só os prioritários
        self._focus_mode: bool = False

    async def set_focus_mode(self, enabled: bool):
        """
        Liga/desliga o modo foco. Com foco ativo, o canal agregado
        (`sub.tickers`, centenas de contratos a cada 2s) não é subscrito -
        só os canais individuais dos pares do bot. Reduz drasticamente o
        volume de mensagens processadas.

        A mudança exige reconexão para valer (a MEXC não tem unsubscribe
        do canal agregado), o que é feito automaticamente.
        """
        if self._focus_mode == enabled:
            return
        self._focus_mode = enabled
        logger.info(
            "Modo foco do WS de futures %s - reconectando para aplicar.",
            "ATIVADO" if enabled else "desativado",
        )
        if self._ws is not None:
            try:
                await self._ws.close()  # força reconexão com a nova configuração
            except Exception:
                pass

    async def set_priority_symbols(self, symbols: list[str]):
        """
        Define quais símbolos devem receber subscrição individual
        (`sub.ticker`), que traz bid/ask real e atualiza a cada 1s.
        Tipicamente os pares configurados no bot. Se já houver conexão
        ativa, subscreve imediatamente os novos.
        """
        new_symbols = set(symbols) - self._priority_symbols

        if len(self._priority_symbols) + len(new_symbols) > MAX_INDIVIDUAL_SUBSCRIPTIONS:
            logger.warning(
                "Limite de %d subscrições individuais de futures atingido. "
                "Símbolos excedentes continuarão usando o canal agregado (2s, sem bid/ask).",
                MAX_INDIVIDUAL_SUBSCRIPTIONS,
            )
            allowed = MAX_INDIVIDUAL_SUBSCRIPTIONS - len(self._priority_symbols)
            new_symbols = set(list(new_symbols)[:max(0, allowed)])

        if not new_symbols:
            return

        self._priority_symbols.update(new_symbols)

        if self._ws is not None and self.connected:
            for sym in new_symbols:
                try:
                    await self._ws.send(json.dumps({
                        "method": "sub.ticker", "param": {"symbol": sym},
                    }))
                except Exception as e:
                    logger.warning("Falha ao subscrever %s individualmente: %s", sym, e)

    async def run(self):
        while not self._stop:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.warning("Futures WS caiu, reconectando: %s", e)
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
            MEXC_FUTURES_WS_URL, ping_interval=None, additional_headers=extra_headers
        ) as ws:
            logger.info("Conectado ao WS de futures da MEXC")
            self.connected = True
            self._ws = ws

            # Canal agregado: base para todos os pares do Dashboard
            # Canal agregado: base para todos os pares do Dashboard.
            # Em modo foco, é PULADO - ele traz centenas de contratos a cada
            # 2s, e processar tudo isso só adiciona carga sem beneficiar os
            # pares que o bot está operando (que já vêm pelo canal individual,
            # mais rápido e com bid/ask).
            if not self._focus_mode:
                await ws.send(json.dumps({"method": "sub.tickers", "param": {}}))
            else:
                logger.info(
                    "Modo foco ativo: canal agregado de futures NÃO subscrito "
                    "(apenas os %d pares do bot, via canal individual).",
                    len(self._priority_symbols),
                )

            # Canais individuais: pares prioritários (bid/ask real, 1s)
            for sym in self._priority_symbols:
                await ws.send(json.dumps({"method": "sub.ticker", "param": {"symbol": sym}}))
            if self._priority_symbols:
                logger.info(
                    "Subscrito individualmente (bid/ask real, ~1s) em %d pares de futures: %s",
                    len(self._priority_symbols), ", ".join(sorted(self._priority_symbols)),
                )

            last_ping = time.time()

            while not self._stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    if time.time() - last_ping > 10:
                        await ws.send(json.dumps({"method": "ping"}))
                        last_ping = time.time()
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                channel = msg.get("channel")

                if channel == "push.ticker":
                    # Canal individual: TEM bid1/ask1, atualiza a cada ~1s
                    item = msg.get("data", {})
                    parsed = self._parse_ticker_item(item, has_book=True)
                    if parsed:
                        await self.on_tickers_update(parsed)

                elif channel == "push.tickers":
                    # Canal agregado: NÃO tem bid1/ask1, atualiza a cada 2s.
                    # Pula os símbolos prioritários - para eles, o canal
                    # individual já entrega dado melhor e mais recente, e
                    # sobrescrever com lastPrice seria uma regressão.
                    data = msg.get("data", [])
                    parsed = {}
                    for item in data:
                        symbol = item.get("symbol")
                        if not symbol or symbol in self._priority_symbols:
                            continue
                        one = self._parse_ticker_item(item, has_book=False)
                        if one:
                            parsed.update(one)
                    if parsed:
                        await self.on_tickers_update(parsed)

                elif channel == "pong":
                    pass  # keepalive ok

    @staticmethod
    def _parse_ticker_item(item: dict, has_book: bool) -> dict:
        """
        Converte um item de ticker da MEXC para o formato interno.

        has_book=True  -> canal individual (push.ticker), tem bid1/ask1:
                          usa o BID como preço de referência, que é o que se
                          recebe ao VENDER (abrir short) a mercado.
        has_book=False -> canal agregado (push.tickers), sem bid/ask:
                          cai para lastPrice, que é o melhor disponível ali.
        """
        symbol = item.get("symbol")
        if not symbol:
            return {}
        try:
            last = float(item["lastPrice"])
        except (KeyError, ValueError, TypeError):
            return {}

        bid = ask = 0.0
        if has_book:
            try:
                bid = float(item.get("bid1", 0) or 0)
                ask = float(item.get("ask1", 0) or 0)
            except (ValueError, TypeError):
                bid = ask = 0.0

        price = bid if bid > 0 else last

        try:
            vol = float(item.get("volume24", 0) or 0)
        except (ValueError, TypeError):
            vol = 0.0

        return {
            symbol: {
                "price": price,
                "bid": bid,
                "ask": ask,
                "last": last,
                "vol": vol,
                "has_book": has_book,
            }
        }
