"""
Cliente REST autenticado para Futures (contratos perpétuos) da MEXC.

Esquema de assinatura (confirmado na doc oficial, diferente do Spot):
- Headers: ApiKey, Request-Time (timestamp ms como string), Signature, Content-Type: application/json
- Para GET/DELETE: parameterString = parâmetros em ordem alfabética, unidos por '&'
- Para POST: parameterString = corpo (JSON) serializado como string, SEM reordenar chaves
- signString = accessKey + timestamp + parameterString
- Signature = HMAC_SHA256(secretKey, signString), hex, lowercase

Convenções de campos de ordem (confirmadas em exemplos oficiais/push de ordem):
- side: 1 = abrir long, 2 = fechar short, 3 = abrir short, 4 = fechar long
- type: 1 = limite, 5 = mercado
- openType: 1 = margem isolada, 2 = margem cruzada (usamos isolada por padrão, mais segura)

Nunca loga api_key/secret_key. Nunca envia para nenhum destino além de api.mexc.com.
"""
import hashlib
import hmac
import json
import time
import logging

import httpx

logger = logging.getLogger("mexc_futures_client")

FUTURES_BASE_URL = "https://api.mexc.com"

SIDE_OPEN_LONG = 1
SIDE_CLOSE_SHORT = 2
SIDE_OPEN_SHORT = 3
SIDE_CLOSE_LONG = 4

ORDER_TYPE_LIMIT = 1
# type 3 = "transact or cancel immediately" (IOC): executa contra o book
# imediatamente até o preço-limite informado e cancela o resto. É o
# equivalente de futures da ordem LIMIT+IOC do spot, e o caminho normal de
# execução desde que o bot passou a ter teto de slippage — a ordem a
# mercado (type 5) ficou reservada ao escalonamento e ao kill switch.
ORDER_TYPE_IOC = 3
ORDER_TYPE_MARKET = 5

OPEN_TYPE_ISOLATED = 1
OPEN_TYPE_CROSS = 2


class MexcFuturesAuthError(Exception):
    pass


class MexcFuturesAPIError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"MEXC Futures API error {code}: {message}")


class MexcFuturesClient:
    def __init__(self, api_key: str, secret_key: str, http_client: httpx.AsyncClient, recv_window: int = 10000):
        if not api_key or not secret_key:
            raise MexcFuturesAuthError("api_key e secret_key do Futures são obrigatórios")
        self._api_key = api_key
        self._secret_key = secret_key
        self._client = http_client
        self._recv_window = recv_window

    def _sign(self, timestamp: str, param_string: str) -> str:
        sign_string = f"{self._api_key}{timestamp}{param_string}"
        return hmac.new(
            self._secret_key.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        timestamp = str(int(time.time() * 1000))
        # GET: parâmetros em ordem alfabética, unidos por '&'
        param_string = "&".join(f"{k}={params[k]}" for k in sorted(params.keys())) if params else ""
        signature = self._sign(timestamp, param_string)

        headers = {
            "ApiKey": self._api_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Content-Type": "application/json",
            "Recv-Window": str(self._recv_window),
        }
        url = f"{FUTURES_BASE_URL}{path}"
        resp = await self._client.get(url, params=params, headers=headers, timeout=15)
        return self._handle_response(resp)

    async def _post(self, path: str, body: dict) -> dict:
        timestamp = str(int(time.time() * 1000))
        # POST: parameterString é o JSON body como string (mesma serialização enviada)
        body_str = json.dumps(body, separators=(",", ":"))
        signature = self._sign(timestamp, body_str)

        headers = {
            "ApiKey": self._api_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Content-Type": "application/json",
            "Recv-Window": str(self._recv_window),
        }
        url = f"{FUTURES_BASE_URL}{path}"
        resp = await self._client.post(url, content=body_str, headers=headers, timeout=15)
        return self._handle_response(resp)

    async def _delete(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        timestamp = str(int(time.time() * 1000))
        param_string = "&".join(f"{k}={params[k]}" for k in sorted(params.keys())) if params else ""
        signature = self._sign(timestamp, param_string)

        headers = {
            "ApiKey": self._api_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Content-Type": "application/json",
            "Recv-Window": str(self._recv_window),
        }
        url = f"{FUTURES_BASE_URL}{path}"
        resp = await self._client.delete(url, params=params, headers=headers, timeout=15)
        return self._handle_response(resp)

    def _handle_response(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except Exception:
            raise MexcFuturesAPIError(resp.status_code, resp.text)

        if resp.status_code >= 400 or not data.get("success", True):
            code = data.get("code", resp.status_code)
            msg = data.get("message") or data.get("msg") or str(data)
            raise MexcFuturesAPIError(code, msg)

        return data

    # ---------------- Account ----------------

    async def get_assets(self) -> dict:
        """GET /api/v1/private/account/assets — todos os ativos da conta futures."""
        return await self._get("/api/v1/private/account/assets")

    async def get_asset(self, currency: str) -> dict:
        """GET /api/v1/private/account/asset/{currency}"""
        return await self._get(f"/api/v1/private/account/asset/{currency}")

    # ---------------- Contract info (público, mas útil aqui) ----------------

    async def get_contract_detail(self, symbol: str | None = None) -> dict:
        """GET /api/v1/contract/detail — metadados do contrato (contractSize, minVol, etc). Público."""
        params = {"symbol": symbol} if symbol else {}
        url = f"{FUTURES_BASE_URL}/api/v1/contract/detail"
        resp = await self._client.get(url, params=params, timeout=15)
        return resp.json()

    async def get_depth(self, symbol: str, limit: int = 20) -> dict:
        """
        GET /api/v1/contract/depth/{symbol} — livro de ofertas do contrato
        (público, sem assinatura).

        As quantidades vêm em CONTRATOS, não na moeda base — converter para
        moeda base exige multiplicar por `contractSize`. Ver bot/depth.py,
        seção "Convenção de unidades".

        Timeout curto pelo mesmo motivo do lado Spot: alimenta decisão de
        execução imediata, e book velho é pior que book ausente.
        """
        url = f"{FUTURES_BASE_URL}/api/v1/contract/depth/{symbol}"
        resp = await self._client.get(url, params={"limit": limit}, timeout=5)
        resp.raise_for_status()
        return resp.json()

    # ---------------- Positions ----------------

    async def get_open_positions(self, symbol: str | None = None) -> dict:
        """GET /api/v1/private/position/open_positions"""
        params = {"symbol": symbol} if symbol else {}
        return await self._get("/api/v1/private/position/open_positions", params)

    async def change_leverage(self, position_id: int | None, leverage: int, open_type: int, symbol: str | None = None) -> dict:
        """POST /api/v1/private/position/change_leverage"""
        body = {"leverage": leverage, "openType": open_type}
        if position_id is not None:
            body["positionId"] = position_id
        if symbol is not None:
            body["symbol"] = symbol
        return await self._post("/api/v1/private/position/change_leverage", body)

    # ---------------- Orders ----------------

    async def submit_order(
        self,
        symbol: str,
        side: int,
        vol: float,
        order_type: int = ORDER_TYPE_MARKET,
        price: float | None = None,
        leverage: int = 1,
        open_type: int = OPEN_TYPE_ISOLATED,
        external_oid: str | None = None,
        reduce_only: bool = False,
    ) -> dict:
        """
        POST /api/v1/private/order/submit

        Para ordens a MERCADO, a MEXC ainda exige um campo `price` de referência
        (não é usado como limite, mas é obrigatório no payload) — usamos o preço
        atual do mercado como referência segura.
        """
        body = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "vol": vol,
            "openType": open_type,
            "leverage": leverage,
        }
        if price is not None:
            body["price"] = price
        if external_oid:
            body["externalOid"] = external_oid
        if reduce_only:
            body["reduceOnly"] = True
        return await self._post("/api/v1/private/order/submit", body)

    async def cancel_order(self, order_ids: list[str]) -> dict:
        """POST /api/v1/private/order/cancel — aceita lista de orderIds."""
        return await self._post("/api/v1/private/order/cancel", order_ids)

    async def cancel_all_orders(self, symbol: str | None = None) -> dict:
        """POST /api/v1/private/order/cancel_all"""
        body = {"symbol": symbol} if symbol else {}
        return await self._post("/api/v1/private/order/cancel_all", body)

    async def get_order(self, order_id: str) -> dict:
        """GET /api/v1/private/order/get/{order_id}"""
        return await self._get(f"/api/v1/private/order/get/{order_id}")

    async def get_open_orders(self, symbol: str) -> dict:
        """GET /api/v1/private/order/list/open_orders/{symbol}"""
        return await self._get(f"/api/v1/private/order/list/open_orders/{symbol}")
