"""
Cliente REST autenticado da BingX (spot e swap perpétuo USDT).

## Assinatura

    signature = HMAC_SHA256(secret, query_string)     <- hex, minúsculo
    query_string inclui `timestamp` (ms) e vai TAMBÉM no corpo/URL

O parâmetro `signature` é anexado ao final da MESMA query string que foi
assinada. A ordem dos parâmetros importa: a assinatura é do texto literal, não
de um dicionário, então reordenar depois de assinar invalida tudo. Por isso a
query é montada uma vez, em `_assinar`, e reutilizada como enviada.

Header: `X-BX-APIKEY`.

## Convenções que diferem de MEXC e Gate

**Swap usa `positionSide`.** O par (`side`, `positionSide`) é que define a
operação:

    abrir venda   -> side=SELL, positionSide=SHORT
    fechar venda  -> side=BUY,  positionSide=SHORT

Note que fechar um short é `side=BUY` com `positionSide=SHORT` — o
`positionSide` identifica QUAL posição, e o `side` diz o que fazer com ela.
Mandar `positionSide=LONG` para fechar um short abre uma posição nova no lado
oposto em vez de fechar a existente.

**Quantidade do swap é na MOEDA BASE, não em contratos.** MEXC e Gate pedem
contratos; a BingX pede a quantidade da moeda. Por isso o `contract_size` do
adaptador BingX é 1,0 (ver exchanges/bingx.py) — converter aqui daria uma
ordem `size` vezes maior ou menor.

**Toda resposta vem embrulhada em `{"code": 0, "data": ...}`** e `code != 0`
é erro mesmo com HTTP 200. Checar só o status HTTP engoliria a falha.

## Estado deste módulo

NÃO EXERCITADO CONTRA A API REAL: no momento em que foi escrito não havia
credenciais da BingX disponíveis. A suíte cobre assinatura, montagem de
payload e interpretação de resposta contra dublês construídos a partir da
documentação. Antes do primeiro uso com dinheiro, valide manualmente — os
bugs 4, 5, 6, 14 e 16 deste projeto foram comportamentos de API que só
apareceram na conta real.
"""
import hashlib
import hmac
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("bingx_client")

BASE_URL = "https://open-api.bingx.com"


class BingxAPIError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"BingX API error {code}: {message}")


class BingxClient:
    def __init__(self, api_key: str, secret_key: str, http_client: httpx.AsyncClient):
        if not api_key or not secret_key:
            raise ValueError("api_key e secret_key da BingX são obrigatórios")
        self._key = api_key
        self._secret = secret_key
        self._client = http_client

    # ---------------- Assinatura ----------------

    def _assinar(self, params: dict) -> str:
        """
        Monta a query string assinada, já com `signature` no final.

        A assinatura é do TEXTO da query, não da estrutura: montar a string
        aqui e devolvê-la pronta é o que garante que o que foi assinado é
        exatamente o que vai no wire. Remontar os parâmetros depois — mesmo
        com os mesmos valores em ordem diferente — invalida a assinatura.
        """
        limpos = {k: v for k, v in params.items() if v is not None}
        limpos["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={limpos[k]}" for k in limpos)
        assinatura = hmac.new(
            self._secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={assinatura}"

    async def _request(self, method: str, path: str, params: Optional[dict] = None,
                       timeout: float = 15.0):
        query = self._assinar(params or {})
        url = f"{BASE_URL}{path}?{query}"
        resp = await self._client.request(
            method.upper(), url,
            headers={"X-BX-APIKEY": self._key, "Accept": "application/json"},
            timeout=timeout,
        )
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response):
        try:
            data = resp.json()
        except Exception:
            raise BingxAPIError(resp.status_code, resp.text[:200])
        if resp.status_code >= 400:
            raise BingxAPIError(resp.status_code, str(data)[:200])
        # HTTP 200 com code != 0 é erro. Tratar o status como sucesso
        # engoliria a falha silenciosamente — a pior forma de erro num
        # caminho que envia ordem.
        if isinstance(data, dict) and data.get("code") not in (0, None, "0"):
            raise BingxAPIError(data.get("code"), data.get("msg", str(data)[:200]))
        return data.get("data") if isinstance(data, dict) else data

    # ---------------- Conta ----------------

    async def get_spot_balance(self, asset: str) -> dict:
        data = await self._request("GET", "/openApi/spot/v1/account/balance")
        for b in (data or {}).get("balances", []):
            if b.get("asset") == asset:
                return {"free": float(b.get("free", 0) or 0), "locked": float(b.get("locked", 0) or 0)}
        return {"free": 0.0, "locked": 0.0}

    async def get_futures_balance(self) -> dict:
        data = await self._request("GET", "/openApi/swap/v2/user/balance")
        saldo = (data or {}).get("balance", {})
        return {
            "available": float(saldo.get("availableMargin", 0) or 0),
            "total": float(saldo.get("balance", 0) or 0),
            "position_margin": float(saldo.get("usedMargin", 0) or 0),
        }

    async def get_open_positions(self, symbol: Optional[str] = None) -> list:
        data = await self._request(
            "GET", "/openApi/swap/v2/user/positions", {"symbol": symbol} if symbol else {},
        )
        return [p for p in (data or []) if float(p.get("positionAmt", 0) or 0) != 0]

    # ---------------- Ordens: Spot ----------------

    async def spot_limit_ioc(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        return await self._request("POST", "/openApi/spot/v1/trade/order", {
            "symbol": symbol,
            "side": side.upper(),        # BUY | SELL
            "type": "LIMIT",
            "quantity": _txt(quantity),
            "price": _txt(price),
            "timeInForce": "IOC",
        })

    async def spot_market(self, symbol: str, side: str, quantity: float) -> dict:
        return await self._request("POST", "/openApi/spot/v1/trade/order", {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": _txt(quantity),
        })

    async def get_spot_order(self, symbol: str, order_id: str) -> dict:
        return await self._request("GET", "/openApi/spot/v1/trade/query", {
            "symbol": symbol, "orderId": order_id,
        })

    async def cancel_spot_order(self, symbol: str, order_id: str) -> dict:
        """
        Cancela UMA ordem de spot. Note que na BingX o cancelamento de spot é
        POST (o swap é DELETE) — a assimetria é da API, não engano de digitação.

        É a peça que permite "colocar, esperar, cancelar, reler" sem derrubar
        ordens de outros pares, como `cancel_all_spot` faria.
        """
        return await self._request("POST", "/openApi/spot/v1/trade/cancel", {
            "symbol": symbol, "orderId": order_id,
        })

    async def cancel_all_spot(self, symbol: str) -> dict:
        return await self._request("POST", "/openApi/spot/v1/trade/cancelOpenOrders", {"symbol": symbol})

    # ---------------- Ordens: Swap ----------------

    async def swap_order(
        self, symbol: str, side: str, position_side: str, quantity: float,
        price: Optional[float] = None, *, tif: Optional[str] = "IOC",
    ) -> dict:
        """
        Ordem de swap.

        `quantity` é na MOEDA BASE (não em contratos, diferente de MEXC e
        Gate). `position_side` identifica QUAL posição e `side` diz o que
        fazer com ela — ver docstring do módulo.
        """
        params = {
            "symbol": symbol,
            "side": side.upper(),                    # BUY | SELL
            "positionSide": position_side.upper(),   # LONG | SHORT
            "type": "LIMIT" if price is not None else "MARKET",
            "quantity": _txt(quantity),
        }
        if price is not None:
            params["price"] = _txt(price)
            if tif:
                params["timeInForce"] = tif
        return await self._request("POST", "/openApi/swap/v2/trade/order", params)

    async def open_short(self, symbol: str, quantity: float, price: Optional[float]) -> dict:
        """Abre venda: SELL sobre a posição SHORT."""
        return await self.swap_order(symbol, "SELL", "SHORT", quantity, price)

    async def close_short(self, symbol: str, quantity: float, price: Optional[float]) -> dict:
        """
        Fecha venda: BUY sobre a posição SHORT.

        `positionSide` continua SHORT — é ele que identifica a posição a
        reduzir. Mandar LONG aqui ABRIRIA uma posição comprada nova em vez de
        fechar a vendida existente.
        """
        return await self.swap_order(symbol, "BUY", "SHORT", quantity, price)

    async def get_swap_order(self, symbol: str, order_id: str) -> dict:
        return await self._request("GET", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "orderId": order_id,
        })

    async def cancel_swap_order(self, symbol: str, order_id: str) -> dict:
        """Cancela UMA ordem de swap (DELETE aqui, POST no spot — ver acima)."""
        return await self._request("DELETE", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "orderId": order_id,
        })

    async def cancel_all_swap(self, symbol: str) -> dict:
        return await self._request("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol})


def _txt(valor: float) -> str:
    """Sem notação científica: `str(0.00000123)` daria "1.23e-06", rejeitado."""
    return f"{valor:.12f}".rstrip("0").rstrip(".") or "0"
