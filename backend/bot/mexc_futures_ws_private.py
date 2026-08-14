"""
WebSocket privado de Futures da MEXC.

Autenticação via mensagem "login" dentro do próprio WS (não usa listenKey,
diferente do Spot). Esquema de assinatura é o MESMO do REST de futures:
    signString = accessKey + reqTime + requestParam
    signature  = HMAC_SHA256(secretKey, signString)
Como o login não tem parâmetros de negócio, requestParam = "" (string vazia).

Canais privados relevantes:
- push.personal.order  -> atualizações de ordem (fills, cancelamentos)
- push.personal.asset  -> atualizações de saldo
- push.personal.position -> atualizações de posição

Documentação oficial confirma: sem assinatura válida de "login", nenhum canal
pessoal é entregue. O canal público (push.tickers, já usado no dashboard)
continua funcionando sem login.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time

import websockets

from config import MEXC_FUTURES_WS_URL, WS_RECONNECT_DELAY

logger = logging.getLogger("mexc_futures_ws_private")


class FuturesPrivateWebSocketClient:
    def __init__(self, api_key: str, secret_key: str, on_order_update, on_asset_update=None, on_position_update=None):
        """
        on_order_update: async callback(dict) chamado a cada atualização de ordem.
        on_asset_update / on_position_update: opcionais, mesma assinatura.
        """
        self._api_key = api_key
        self._secret_key = secret_key
        self.on_order_update = on_order_update
        self.on_asset_update = on_asset_update
        self.on_position_update = on_position_update
        self._stop = False
        self.connected = False
        self.logged_in = False
        self._ws = None

    def _sign_login(self, req_time: str) -> str:
        sign_string = f"{self._api_key}{req_time}"
        return hmac.new(
            self._secret_key.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def run(self):
        while not self._stop:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.warning("Futures WS privado caiu, reconectando: %s", e)
                self.connected = False
                self.logged_in = False
                await asyncio.sleep(WS_RECONNECT_DELAY)

    async def stop(self):
        self._stop = True
        if self._ws:
            await self._ws.close()

    async def _connect_and_listen(self):
        extra_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        async with websockets.connect(
            MEXC_FUTURES_WS_URL, ping_interval=None, additional_headers=extra_headers
        ) as ws:
            self._ws = ws
            self.connected = True
            logger.info("Conectado ao WS privado de futures. Autenticando...")

            req_time = str(int(time.time() * 1000))
            signature = self._sign_login(req_time)
            await ws.send(json.dumps({
                "method": "login",
                "param": {
                    "apiKey": self._api_key,
                    "signature": signature,
                    "reqTime": req_time,
                },
            }))

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

                if channel == "rs.login":
                    if msg.get("data") == "success":
                        self.logged_in = True
                        logger.info("Login no WS privado de futures confirmado.")
                    else:
                        logger.error("Falha no login do WS privado de futures: %s", msg)
                elif channel == "rs.error":
                    logger.error("Erro reportado pelo WS privado de futures: %s", msg)
                elif channel == "push.personal.order":
                    await self.on_order_update(msg.get("data", {}))
                elif channel == "push.personal.asset" and self.on_asset_update:
                    await self.on_asset_update(msg.get("data", {}))
                elif channel == "push.personal.position" and self.on_position_update:
                    await self.on_position_update(msg.get("data", {}))
                elif channel == "pong":
                    pass
