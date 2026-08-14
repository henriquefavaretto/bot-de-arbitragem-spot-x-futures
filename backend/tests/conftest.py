"""
Dublês de teste para o motor do bot.

O ambiente de desenvolvimento deste projeto nunca teve acesso de rede à MEXC
(ver CLAUDE.md), então TODO teste automatizado aqui roda contra dublês. A
consequência prática é que os dublês precisam imitar não só o formato feliz
da API, mas também os comportamentos torcidos que já causaram prejuízo real:
`executedQty=0` numa ordem que executou, ordem IOC terminando CANCELADA com
preenchimento parcial dentro, saldo vindo com imprecisão de ponto flutuante.

Cada um desses comportamentos tem um teste dedicado. Se um dublê aqui for
"limpo demais", o teste passa e a produção quebra — que foi exatamente como
os bugs 4, 5 e 6 do CLAUDE.md chegaram até a conta real.
"""
import sys
from pathlib import Path

import pytest

# Os módulos do backend são importados como `bot.x` / `engine`, sem pacote
# raiz, então a pasta backend/ precisa estar no sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.bot_engine import ArbitrageBotEngine, ExecutionMode, PairConfig, PairRuntime, PairState
from bot.execution import SlippagePolicy
from bot.sizing import ContractSpec, SpotSymbolSpec


class FakeBotStorage:
    """BotStorage em memória — registra tudo para inspeção nos testes."""

    def __init__(self):
        self.events: list[dict] = []
        self.positions: dict[str, dict] = {}
        self.configs: dict[str, dict] = {}

    async def log_event(self, symbol, event, detail, simulated):
        self.events.append({"symbol": symbol, "event": event, "detail": detail, "simulated": simulated})

    async def upsert_position(self, symbol, state, simulated, **fields):
        self.positions[symbol] = {"state": state, "simulated": simulated, **fields}

    async def clear_position(self, symbol):
        self.positions.pop(symbol, None)

    async def upsert_pair_config(self, symbol, enabled, entry, exit_, size,
                                 buy_venue="mexc:spot", sell_venue="mexc:futures"):
        self.configs[symbol] = {
            "enabled": enabled, "entry_spread_pct": entry,
            "exit_spread_pct": exit_, "position_size_usdt": size,
            "buy_venue": buy_venue, "sell_venue": sell_venue,
        }

    async def delete_pair_config(self, symbol):
        self.configs.pop(symbol, None)

    async def get_all_pair_configs(self):
        return dict(self.configs)

    async def get_all_positions(self):
        return dict(self.positions)

    def events_of(self, name: str) -> list[dict]:
        return [e for e in self.events if e["event"] == name]


class FakeMarketClient:
    """
    Devolve um payload de profundidade fixo (ou uma sequência deles, para
    testar mudança de book entre chamadas). Conta as chamadas, o que permite
    verificar a trava de throttle.

    Expõe `get_depth` (payload cru, como o cliente público) E `fetch_depth`
    (OrderBook já normalizado, como um adaptador de venue), porque o motor
    passou a consumir pela segunda forma quando ficou multi-exchange. Manter
    as duas deixa os testes antigos válidos sem duplicar dublê.
    """

    def __init__(self, payloads, kind="spot"):
        self.payloads = payloads if isinstance(payloads, list) else [payloads]
        self.calls = 0
        self.raise_error = None
        self.kind = kind

    async def get_depth(self, symbol, limit=20):
        self.calls += 1
        if self.raise_error:
            raise self.raise_error
        idx = min(self.calls - 1, len(self.payloads) - 1)
        return self.payloads[idx]

    async def fetch_depth(self, symbol, limit=20):
        from bot.depth import parse_futures_depth, parse_spot_depth
        bruto = await self.get_depth(symbol, limit)
        if self.kind == "spot":
            return parse_spot_depth(bruto, symbol)
        return parse_futures_depth(bruto, symbol)


def spot_depth_payload(bids, asks):
    """Formato do `GET /api/v3/depth`: preço e quantidade como STRING."""
    return {
        "lastUpdateId": 1,
        "bids": [[str(p), str(q)] for p, q in bids],
        "asks": [[str(p), str(q)] for p, q in asks],
    }


def futures_depth_payload(bids, asks, timestamp_ms=None):
    """Formato do `GET /api/v1/contract/depth/{symbol}`: números, com contagem de ordens."""
    return {
        "success": True,
        "code": 0,
        "data": {
            "bids": [[p, q, 1] for p, q in bids],
            "asks": [[p, q, 1] for p, q in asks],
            "version": 1,
            **({"timestamp": timestamp_ms} if timestamp_ms else {}),
        },
    }


class FakeSpotClient:
    """
    Cliente Spot autenticado. Cada ordem recebida é registrada em
    `self.orders`; o preenchimento devolvido é controlado por `fill_plan`.

    `fill_plan` é uma lista de dicionários com o que a ordem N deve devolver:
        {"executedQty": ..., "cummulativeQuoteQty": ..., "status": ...}
    Quando `immediate_zero=True`, a resposta do POST vem com executedQty=0 e
    só a consulta posterior de status revela o preenchimento — reproduzindo
    o comportamento real que já causou venda duplicada em produção.
    """

    def __init__(self, fill_plan=None, balance=1e9, immediate_zero=False):
        self.orders: list[dict] = []
        self.fill_plan = fill_plan or []
        self.balance = balance
        self.immediate_zero = immediate_zero
        self._status_by_id: dict[str, dict] = {}
        self.raise_on_order = None
        # Ordens que foram canceladas. Um teste que verifique "nenhuma ordem
        # ficou viva" olha aqui.
        self.cancelamentos: list = []

    def _next_fill(self, qty, price):
        if self.fill_plan:
            idx = min(len(self.orders) - 1, len(self.fill_plan) - 1)
            return dict(self.fill_plan[idx])
        return {"executedQty": qty, "cummulativeQuoteQty": qty * price, "status": "FILLED"}

    async def _record(self, params, qty, price):
        self.orders.append(params)
        if self.raise_on_order:
            raise self.raise_on_order
        fill = self._next_fill(qty, price)
        order_id = str(len(self.orders))
        self._status_by_id[order_id] = {
            "status": fill.get("status", "FILLED"),
            "executedQty": str(fill["executedQty"]),
            "cummulativeQuoteQty": str(fill["cummulativeQuoteQty"]),
        }
        response = {"orderId": order_id, "symbol": params["symbol"]}
        if not self.immediate_zero:
            response["executedQty"] = str(fill["executedQty"])
            response["cummulativeQuoteQty"] = str(fill["cummulativeQuoteQty"])
        else:
            response["executedQty"] = "0"
            response["cummulativeQuoteQty"] = "0"
        return response

    async def new_order_limit(self, symbol, side, quantity, price):
        return await self._record(
            {"symbol": symbol, "side": side, "type": "LIMIT", "quantity": quantity, "price": price},
            quantity, price,
        )

    async def limit_then_cancel(self, symbol, side, quantity, price, *, wait_s=1.5):
        """
        Emula o caminho real: coloca a LIMITE, cancela, devolve o estado
        FINAL lido depois do cancelamento.

        A MEXC spot nao tem IOC -- uma LIMIT fica no book ate ser cancelada.
        O dublê registra o tipo como "LIMIT" (nao "LIMIT_IOC") justamente
        porque foi acreditar num IOC inexistente que custou dinheiro em
        09/08/2026.
        """
        resposta = await self.new_order_limit(symbol, side, quantity, price)
        self.cancelamentos.append(resposta.get("orderId"))
        return await self.get_order(symbol, resposta.get("orderId"))

    async def new_order_market_by_qty(self, symbol, side, quantity):
        return await self._record(
            {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity},
            quantity, 1.0,
        )

    async def new_order_market_by_quote(self, symbol, side, quote_order_qty):
        return await self._record(
            {"symbol": symbol, "side": side, "type": "MARKET_QUOTE", "quoteOrderQty": quote_order_qty},
            quote_order_qty, 1.0,
        )

    async def get_order(self, symbol, order_id):
        return self._status_by_id.get(str(order_id), {"status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"})

    async def get_balance(self, asset):
        return {"free": self.balance, "locked": 0.0}

    async def cancel_order(self, symbol, order_id):
        self.cancelamentos.append(order_id)
        return {"orderId": order_id, "status": "CANCELED"}

    async def cancel_all_open_orders(self, symbol):
        return {}


class FakeFuturesClient:
    """
    Cliente Futures autenticado. `fill_plan` controla o resultado de cada
    ordem no formato {"dealVol": ..., "dealAvgPrice": ..., "state": ...}.

    O `state` importa: 3 = concluída, 4 = cancelada. Uma ordem IOC
    parcialmente preenchida termina em 4 COM dealVol > 0, e o motor precisa
    contar isso como preenchimento real.
    """

    def __init__(self, fill_plan=None):
        self.orders: list[dict] = []
        self.fill_plan = fill_plan or []
        self._status_by_id: dict[str, dict] = {}
        self.raise_on_order = None
        self.cancelamentos: list = []

    async def submit_order(self, symbol, side, vol, order_type=5, price=None,
                           leverage=1, open_type=1, external_oid=None, reduce_only=False):
        self.orders.append({
            "symbol": symbol, "side": side, "vol": vol, "type": order_type,
            "price": price, "reduceOnly": reduce_only,
        })
        if self.raise_on_order:
            raise self.raise_on_order

        if self.fill_plan:
            idx = min(len(self.orders) - 1, len(self.fill_plan) - 1)
            plan = dict(self.fill_plan[idx])
        else:
            plan = {"dealVol": vol, "dealAvgPrice": price or 1.0, "state": 3}

        order_id = str(len(self.orders))
        self._status_by_id[order_id] = plan
        return {"success": True, "code": 0, "data": order_id}

    async def get_order(self, order_id):
        return {"success": True, "data": self._status_by_id.get(str(order_id), {"state": 2})}

    async def cancel_order(self, order_ids):
        self.cancelamentos.extend(order_ids)
        return {"success": True}

    async def cancel_all_orders(self, symbol=None):
        return {"success": True}

    async def get_open_positions(self, symbol=None):
        """
        Por padrão, nenhuma posição aberta. Subclasses sobrescrevem para
        simular a divergência entre o que o bot acha e o que a MEXC reporta —
        que é a situação que este dublê precisa saber representar.
        """
        return {"success": True, "data": []}


def make_engine(
    symbol="JIMOTHY",
    entry_spread_pct=1.0,
    exit_spread_pct=0.2,
    position_size_usdt=100.0,
    contract_size=100.0,
    mode=ExecutionMode.SIMULATION,
    spot_client=None,
    futures_client=None,
    market_spot=None,
    market_futures=None,
    slippage_policy=None,
    spot_taker_pct=0.05,
    futures_taker_pct=0.02,
    buy_venue="mexc:spot",
    sell_venue="mexc:futures",
    **kwargs,
):
    """
    Monta um ArbitrageBotEngine pronto para decidir, com um par configurado e
    os metadados de contrato/símbolo já carregados.

    Os specs são injetados direto (em vez de passar por main.py) porque o que
    está sendo testado é a lógica de decisão e execução, não o carregamento
    de metadados.
    """
    storage = FakeBotStorage()
    # Os dublês de mercado entram como ADAPTADORES do venue configurado: é
    # por eles que o motor lê profundidade desde que virou multi-exchange.
    if market_spot is not None and market_spot.kind != "spot":
        market_spot.kind = "spot"
    if market_futures is not None and market_futures.kind != "futures":
        market_futures.kind = "futures"
    adapters = {}
    if market_spot is not None:
        adapters[buy_venue] = market_spot
    if market_futures is not None:
        adapters[sell_venue] = market_futures

    engine = ArbitrageBotEngine(
        storage,
        execution_mode=mode,
        spot_client=spot_client,
        futures_client=futures_client,
        market_spot_client=market_spot,
        market_futures_client=market_futures,
        venue_adapters=adapters,
        slippage_policy=slippage_policy or SlippagePolicy(max_slippage_pct=0.3, attempt_delay_s=0),
        # Sem throttle nos testes: cada teste controla explicitamente quantas
        # decisões dispara, e um intervalo real só tornaria os testes lentos e
        # dependentes de relógio.
        depth_confirm_interval_s=0.0,
        **kwargs,
    )
    engine.configs[symbol] = PairConfig(
        symbol=symbol, enabled=True,
        entry_spread_pct=entry_spread_pct,
        exit_spread_pct=exit_spread_pct,
        position_size_usdt=position_size_usdt,
        buy_venue=buy_venue, sell_venue=sell_venue,
    )
    engine.runtimes[symbol] = PairRuntime(symbol=symbol)
    engine.contract_specs[symbol] = ContractSpec(
        symbol=f"{symbol}_USDT", contract_size=contract_size, min_vol=1,
        vol_scale=0, price_unit=1e-6, price_scale=6, taker_fee_pct=futures_taker_pct,
    )
    engine.spot_specs[symbol] = SpotSymbolSpec(
        symbol=f"{symbol}USDT", base_asset_precision=2, quote_precision=6,
        min_notional=0.0, price_precision=8, taker_fee_pct=spot_taker_pct,
    )
    return engine, storage


@pytest.fixture
def engine_factory():
    return make_engine
