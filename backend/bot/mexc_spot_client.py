"""
Cliente REST autenticado para o Spot da MEXC.

Esquema de assinatura (confirmado na doc oficial):
- Header: X-MEXC-APIKEY: <api_key>
- totalParams = query string (ordenada como enviada, sem urlencode extra na assinatura)
- signature = HMAC_SHA256(secret_key, totalParams), hex, lowercase
- timestamp obrigatório em todo endpoint SIGNED
- recvWindow opcional (default 5000ms no lado da MEXC)

Nunca loga api_key/secret_key. Nunca envia para nenhum destino além de api.mexc.com.
"""
import asyncio
import hashlib
import hmac
import time
import logging
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("mexc_spot_client")

SPOT_BASE_URL = "https://api.mexc.com"


class MexcSpotAuthError(Exception):
    pass


class MexcSpotAPIError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"MEXC Spot API error {code}: {message}")


class MexcSpotClient:
    def __init__(self, api_key: str, secret_key: str, http_client: httpx.AsyncClient, recv_window: int = 10000):
        if not api_key or not secret_key:
            raise MexcSpotAuthError("api_key e secret_key do Spot são obrigatórios")
        self._api_key = api_key
        self._secret_key = secret_key
        self._client = http_client
        self._recv_window = recv_window

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        signature = hmac.new(
            self._secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return signature

    async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self._recv_window
        params["signature"] = self._sign(params)

        headers = {"X-MEXC-APIKEY": self._api_key}
        url = f"{SPOT_BASE_URL}{path}"

        if method == "GET":
            resp = await self._client.get(url, params=params, headers=headers, timeout=15)
        elif method == "POST":
            resp = await self._client.post(url, params=params, headers=headers, timeout=15)
        elif method == "DELETE":
            resp = await self._client.delete(url, params=params, headers=headers, timeout=15)
        else:
            raise ValueError(f"Método HTTP não suportado: {method}")

        data = resp.json()
        if resp.status_code >= 400 or (isinstance(data, dict) and "code" in data and data.get("code") not in (0, 200)):
            code = data.get("code") if isinstance(data, dict) else resp.status_code
            msg = data.get("msg") if isinstance(data, dict) else str(data)
            raise MexcSpotAPIError(code, msg)

        return data

    # ---------------- Metadados públicos ----------------

    async def get_exchange_info(self, symbol: str) -> dict:
        """
        GET /api/v3/exchangeInfo — metadados públicos do símbolo (não exige
        assinatura). Usado para obter baseAssetPrecision e filtros de
        quantidade mínima, necessários para arredondar corretamente as
        quantidades de venda e evitar o erro "amount scale is invalid".
        """
        url = f"{SPOT_BASE_URL}/api/v3/exchangeInfo"
        resp = await self._client.get(url, params={"symbol": symbol}, timeout=15)
        return resp.json()

    async def get_depth(self, symbol: str, limit: int = 20) -> dict:
        """
        GET /api/v3/depth — livro de ofertas completo (público, sem assinatura).

        Diferente do bookTicker (que só traz a MELHOR oferta de cada lado),
        este endpoint traz as N melhores camadas com as respectivas
        quantidades. É o que permite calcular o preço médio real de executar
        um tamanho específico, em vez de assumir que a posição inteira cabe
        no topo do book — que é a premissa errada que vinha custando mais de
        1 ponto percentual por operação em pares ilíquidos.

        Timeout curto de propósito: este dado alimenta uma decisão de
        execução imediata. Um book que demora 5s para chegar já não descreve
        o mercado em que a ordem vai cair, então é melhor falhar rápido e
        deixar o bot pular o ciclo do que operar com dado velho.
        """
        url = f"{SPOT_BASE_URL}/api/v3/depth"
        resp = await self._client.get(url, params={"symbol": symbol, "limit": limit}, timeout=5)
        resp.raise_for_status()
        return resp.json()

    # ---------------- Account ----------------

    async def get_account(self) -> dict:
        """GET /api/v3/account — saldo e permissões da conta spot."""
        return await self._signed_request("GET", "/api/v3/account")

    async def get_balance(self, asset: str) -> dict:
        """Retorna {'free': float, 'locked': float} para um ativo específico (0 se não encontrado)."""
        account = await self.get_account()
        for b in account.get("balances", []):
            if b["asset"] == asset:
                return {"free": float(b["free"]), "locked": float(b["locked"])}
        return {"free": 0.0, "locked": 0.0}

    # ---------------- Orders ----------------

    async def new_order_market_by_quote(self, symbol: str, side: str, quote_order_qty: float) -> dict:
        """
        Ordem a MERCADO especificando o valor em moeda de cotação (USDT) a gastar/receber,
        via parâmetro quoteOrderQty. side: 'BUY' ou 'SELL'.
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quoteOrderQty": quote_order_qty,
        }
        return await self._signed_request("POST", "/api/v3/order", params)

    async def new_order_market_by_qty(self, symbol: str, side: str, quantity: float) -> dict:
        """Ordem a MERCADO especificando a quantidade do ativo base."""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        return await self._signed_request("POST", "/api/v3/order", params)

    async def new_order_limit(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        """
        Ordem LIMITE simples (GTC — fica no book até preencher ou ser
        cancelada).

        ATENÇÃO: **a MEXC spot NÃO suporta IOC.** O `orderTypes` do
        `exchangeInfo` lista apenas LIMIT, MARKET e LIMIT_MAKER, e o
        parâmetro `timeInForce` é ACEITO E IGNORADO — a ordem volta com
        `timeInForce: null` e se comporta como GTC.

        Isso custou dinheiro em 09/08/2026: o bot mandava LIMIT com
        `timeInForce=IOC` acreditando que o resto seria cancelado, a ordem
        ficava pendurada no book, o bot desistia dela por timeout e seguia
        adiante — e 33 segundos depois ela preenchia sozinha, como MAKER,
        comprando 1100,55 unidades que ninguém mais esperava. Ver
        `limit_then_cancel`, que é o caminho que deve ser usado.
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
        }
        return await self._signed_request("POST", "/api/v3/order", params)

    async def limit_then_cancel(
        self, symbol: str, side: str, quantity: float, price: float,
        *, wait_s: float = 1.5,
    ) -> dict:
        """
        Emula IOC na MEXC spot: coloca a LIMITE, espera brevemente, e
        CANCELA o que sobrou.

        É a única forma de ter teto de preço sem deixar ordem viva na MEXC
        spot, já que ela não tem IOC (ver `new_order_limit`). O cancelamento
        não é uma tentativa de limpeza best-effort — é parte da primitiva:
        enquanto a ordem existir, ela pode preencher depois, e "depois" é
        exatamente quando o bot já decidiu outra coisa.

        Devolve SEMPRE o estado FINAL da ordem, lido depois do cancelamento.
        A quantidade preenchida só é definitiva quando a ordem não pode mais
        preencher — ler antes de cancelar devolve um número que ainda pode
        crescer.
        """
        ordem = await self.new_order_limit(symbol, side, quantity, price)
        order_id = ordem.get("orderId")
        if not order_id:
            return ordem

        await asyncio.sleep(wait_s)

        try:
            await self.cancel_order(symbol, str(order_id))
        except MexcSpotAPIError as e:
            # Cancelar uma ordem já totalmente preenchida devolve erro, e
            # isso é um desfecho NORMAL e bom - significa que ela executou
            # inteira. Qualquer outro erro precisa aparecer.
            if e.code not in (-2011, 30005, "-2011", "30005"):
                logger.warning("Falha ao cancelar ordem %s de %s: %s", order_id, symbol, e)

        # O estado depois do cancelamento é o único definitivo.
        try:
            return await self.get_order(symbol, str(order_id))
        except Exception as e:
            logger.warning("Não foi possível ler o estado final da ordem %s: %s", order_id, e)
            return ordem

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        params = {"symbol": symbol, "orderId": order_id}
        return await self._signed_request("DELETE", "/api/v3/order", params)

    async def cancel_all_open_orders(self, symbol: str) -> dict:
        params = {"symbol": symbol}
        return await self._signed_request("DELETE", "/api/v3/openOrders", params)

    async def get_order(self, symbol: str, order_id: str) -> dict:
        params = {"symbol": symbol, "orderId": order_id}
        return await self._signed_request("GET", "/api/v3/order", params)

    async def get_open_orders(self, symbol: str) -> list:
        params = {"symbol": symbol}
        return await self._signed_request("GET", "/api/v3/openOrders", params)
