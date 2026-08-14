"""
Cliente REST autenticado da Gate.io (spot e futures perpétuo USDT).

## Assinatura (diferente da MEXC em tudo)

    SIGN = HMAC_SHA512(secret, signature_string)     <- 512, não 256

    signature_string = f"{METHOD}\\n{path}\\n{query}\\n{sha512(body)}\\n{timestamp}"

Cinco componentes separados por `\\n`, e o CORPO entra como hash SHA-512 dele
mesmo — não como texto. Corpo vazio vira o hash da string vazia, nunca uma
linha em branco. Errar isso devolve 401 sem dizer qual das cinco partes está
errada, então cada uma tem um teste próprio em tests/test_gate_client.py.

Headers: `KEY`, `Timestamp` (segundos, string), `SIGN`.

## Convenções de ordem que diferem da MEXC

**Futures — o sinal de `size` é a direção.** Não existe campo `side`:
`size=10` abre/aumenta comprado, `size=-10` abre/aumenta vendido. Fechar é
mandar o sinal oposto com `reduce_only=true`. Um sinal trocado aqui não é uma
ordem rejeitada — é uma ordem executada na direção contrária, dobrando a
exposição em vez de zerá-la.

**Futures — `size` é em CONTRATOS**, e o multiplicador é `quanto_multiplier`
(ver exchanges/gate.py). O book também vem em contratos.

**IOC com preço "0" = ordem a mercado.** A Gate não tem um tipo separado:
`tif="ioc"` com `price="0"` é o equivalente de mercado. Com preço explícito,
é a ordem limite IOC que este projeto usa no caminho normal.

**Spot — `account` é obrigatório.** Sem `account="spot"` a ordem pode ser
roteada para a conta de margem, que tem regras e saldo diferentes.

## Estado deste módulo

NÃO EXERCITADO CONTRA A API REAL: no momento em que foi escrito não havia
credenciais da Gate disponíveis. A suíte cobre a assinatura, a montagem do
payload e a interpretação das respostas contra dublês construídos a partir da
documentação. Antes do primeiro uso com dinheiro, rode `test_credentials.py`
e faça uma ordem manual mínima — os bugs 4, 5, 6, 14 e 16 deste projeto foram
todos comportamentos de API que só apareceram na conta real.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("gate_client")

BASE_URL = "https://api.gateio.ws"
PREFIX = "/api/v4"


class GateAPIError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"Gate API error {code}: {message}")


class GateClient:
    """
    Um único cliente cobre spot e futures: a Gate usa a mesma autenticação e
    o mesmo host para os dois, diferente da MEXC (que tem dois esquemas de
    assinatura distintos e por isso exige duas classes).
    """

    def __init__(self, api_key: str, secret_key: str, http_client: httpx.AsyncClient):
        if not api_key or not secret_key:
            raise ValueError("api_key e secret_key da Gate são obrigatórios")
        self._key = api_key
        self._secret = secret_key
        self._client = http_client

    # ---------------- Assinatura ----------------

    def _sign(self, method: str, path: str, query: str, body: str) -> dict:
        """
        Monta os headers assinados.

        `path` precisa incluir o prefixo `/api/v4` — a Gate assina o caminho
        COMPLETO da URL, não o sufixo do recurso.
        """
        timestamp = str(int(time.time()))
        # Corpo vazio vira o hash da string vazia, NÃO uma linha em branco.
        body_hash = hashlib.sha512((body or "").encode("utf-8")).hexdigest()
        payload = f"{method.upper()}\n{path}\n{query}\n{body_hash}\n{timestamp}"
        assinatura = hmac.new(
            self._secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512,
        ).hexdigest()
        return {
            "KEY": self._key,
            "Timestamp": timestamp,
            "SIGN": assinatura,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, resource: str, *, params: Optional[dict] = None,
                       body: Optional[dict] = None, timeout: float = 15.0):
        path = f"{PREFIX}{resource}"
        query = urlencode(params or {})
        # A serialização do corpo precisa ser IDÊNTICA à que vai no wire: se o
        # JSON assinado e o JSON enviado diferirem em um espaço, a assinatura
        # não confere e o erro não diz por quê.
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = self._sign(method, path, query, body_str)

        url = f"{BASE_URL}{path}"
        resp = await self._client.request(
            method.upper(), url, params=params,
            content=body_str if body is not None else None,
            headers=headers, timeout=timeout,
        )
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response):
        try:
            data = resp.json()
        except Exception:
            raise GateAPIError(resp.status_code, resp.text[:200])
        if resp.status_code >= 400:
            if isinstance(data, dict):
                raise GateAPIError(data.get("label", resp.status_code), data.get("message", str(data)))
            raise GateAPIError(resp.status_code, str(data)[:200])
        return data

    # ---------------- Conta ----------------

    async def get_spot_balance(self, asset: str) -> dict:
        contas = await self._request("GET", "/spot/accounts", params={"currency": asset})
        for c in contas or []:
            if c.get("currency") == asset:
                return {"free": float(c.get("available", 0) or 0), "locked": float(c.get("locked", 0) or 0)}
        return {"free": 0.0, "locked": 0.0}

    async def get_futures_balance(self) -> dict:
        conta = await self._request("GET", "/futures/usdt/accounts")
        return {
            "available": float(conta.get("available", 0) or 0),
            "total": float(conta.get("total", 0) or 0),
            "position_margin": float(conta.get("position_margin", 0) or 0),
        }

    async def get_open_positions(self, contract: Optional[str] = None) -> list:
        if contract:
            pos = await self._request("GET", f"/futures/usdt/positions/{contract}")
            posicoes = [pos] if isinstance(pos, dict) else (pos or [])
        else:
            posicoes = await self._request("GET", "/futures/usdt/positions") or []
        # Posição com size 0 não é posição; a Gate devolve o registro mesmo
        # depois de fechada, e tratá-lo como aberto faria o kill switch tentar
        # fechar o que não existe.
        return [p for p in posicoes if float(p.get("size", 0) or 0) != 0]

    # ---------------- Ordens: Spot ----------------

    async def spot_limit_ioc(self, currency_pair: str, side: str, amount: float, price: float) -> dict:
        """
        Ordem limite IOC no spot — o caminho normal de execução deste projeto.

        `account="spot"` é explícito de propósito: sem ele a ordem pode ser
        roteada para a conta de margem, que tem saldo e regras próprias.
        """
        return await self._request("POST", "/spot/orders", body={
            "currency_pair": currency_pair,
            "type": "limit",
            "account": "spot",
            "side": side.lower(),          # "buy" | "sell"
            "amount": _txt(amount),
            "price": _txt(price),
            "time_in_force": "ioc",
        })

    async def spot_market_sell(self, currency_pair: str, amount_base: float) -> dict:
        """
        VENDA a mercado no spot. `amount_base` é a quantidade da MOEDA BASE.

        Na Gate market exige `tif="ioc"` — market com GTC é rejeitada.
        """
        return await self._request("POST", "/spot/orders", body={
            "currency_pair": currency_pair,
            "type": "market",
            "account": "spot",
            "side": "sell",
            "amount": _txt(amount_base),
            "time_in_force": "ioc",
        })

    async def spot_market_buy(self, currency_pair: str, amount_quote: float) -> dict:
        """
        COMPRA a mercado no spot. **`amount_quote` é o valor em USDT a gastar,
        NÃO a quantidade da moeda base.**

        Esta assimetria é da Gate e é a armadilha mais cara deste arquivo: numa
        compra a mercado o campo `amount` significa quote; numa venda significa
        base. Passar base numa compra não é rejeitado — é aceito e executa uma
        ordem do tamanho errado. Numa memecoin a 0,004 USDT, pedir "1000
        unidades" gastaria 1000 USDT em vez de 4: 250x a posição decidida.

        Por isso as duas direções são funções SEPARADAS com o nome da unidade
        no parâmetro, em vez de um `spot_market(side, amount)` que aceitaria o
        número errado sem reclamar. O mesmo motivo de `open_short`/`close_short`
        existirem no lugar de o chamador montar o sinal na mão.
        """
        return await self._request("POST", "/spot/orders", body={
            "currency_pair": currency_pair,
            "type": "market",
            "account": "spot",
            "side": "buy",
            "amount": _txt(amount_quote),
            "time_in_force": "ioc",
        })

    async def get_spot_order(self, order_id: str, currency_pair: str) -> dict:
        return await self._request(
            "GET", f"/spot/orders/{order_id}",
            params={"currency_pair": currency_pair, "account": "spot"},
        )

    async def cancel_spot_order(self, order_id: str, currency_pair: str) -> dict:
        """
        Cancela UMA ordem de spot.

        É a peça que falta para "colocar, esperar, cancelar, reler" — a
        primitiva que impede uma ordem de sobreviver à decisão do bot (bug 17).
        `cancel_all_spot` não serve: numa conta que opera vários pares, cancelar
        tudo derrubaria ordens de outra operação em andamento.
        """
        return await self._request(
            "DELETE", f"/spot/orders/{order_id}",
            params={"currency_pair": currency_pair, "account": "spot"},
        )

    async def cancel_all_spot(self, currency_pair: str) -> list:
        return await self._request("DELETE", "/spot/orders", params={"currency_pair": currency_pair})

    # ---------------- Ordens: Futures ----------------

    async def futures_order(
        self, contract: str, size: int, price: Optional[float],
        *, reduce_only: bool = False, tif: str = "ioc",
    ) -> dict:
        """
        Ordem de futures. **O SINAL DE `size` É A DIREÇÃO.**

            size > 0  -> comprado (abre long, ou fecha short)
            size < 0  -> vendido  (abre short, ou fecha long)

        Não existe campo `side` na Gate. Um sinal trocado aqui não gera erro:
        gera uma ordem executada na direção oposta, que DOBRA a exposição em
        vez de zerá-la. É o erro mais caro possível neste arquivo, e o motivo
        de `close_short`/`open_short` existirem como funções nomeadas em vez
        de o chamador montar o sinal na mão.

        `price=None` vira `"0"`, que na Gate significa ordem a mercado quando
        combinada com `tif="ioc"`.
        """
        corpo = {
            "contract": contract,
            "size": int(size),
            "price": _txt(price) if price is not None else "0",
            "tif": tif,
        }
        if reduce_only:
            corpo["reduce_only"] = True
        return await self._request("POST", "/futures/usdt/orders", body=corpo)

    async def open_short(self, contract: str, contracts: int, price: Optional[float]) -> dict:
        """Abre venda: size NEGATIVO."""
        return await self.futures_order(contract, -abs(int(contracts)), price)

    async def close_short(self, contract: str, contracts: int, price: Optional[float]) -> dict:
        """Fecha venda: size POSITIVO e reduce_only."""
        return await self.futures_order(contract, abs(int(contracts)), price, reduce_only=True)

    async def get_futures_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/futures/usdt/orders/{order_id}")

    async def cancel_futures_order(self, order_id: str) -> dict:
        """Cancela UMA ordem de futures (ver `cancel_spot_order`)."""
        return await self._request("DELETE", f"/futures/usdt/orders/{order_id}")

    async def cancel_all_futures(self, contract: str) -> list:
        return await self._request("DELETE", "/futures/usdt/orders", params={"contract": contract})


def _txt(valor: float) -> str:
    """
    Converte para string sem notação científica.

    `str(0.00000123)` em Python dá "1.23e-06", que a Gate rejeita. Preços de
    memecoin caem nessa faixa com frequência, então a formatação explícita
    não é preciosismo.
    """
    return f"{valor:.12f}".rstrip("0").rstrip(".") or "0"
