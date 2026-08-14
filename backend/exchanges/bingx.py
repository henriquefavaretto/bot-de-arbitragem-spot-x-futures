"""
Adaptadores da BingX (spot e swap perpétuo USDT).

Peculiaridades confirmadas contra a API real:

- Símbolo `BTC-USDT` (hífen) nos dois mercados.
- Toda resposta vem embrulhada em `{"code": 0, "msg": ..., "data": ...}`;
  `code != 0` é erro mesmo com HTTP 200, então checar só o status não basta.
- Os tickers em massa trazem `bidPrice`/`askPrice` E as quantidades do topo
  (`bidQty`/`askQty`) — informação que MEXC e Gate não dão no ticker.

- **O book de SPOT devolve os asks em ordem DECRESCENTE.** Medição de
  05/08/2026 nos seis venues do sistema:

        gate spot     bids=DECR   asks=CRESC
        gate futures  bids=DECR   asks=CRESC
        bingx spot    bids=DECR   asks=DECR    <-- aqui
        bingx swap    bids=DECR   asks=CRESC
        mexc spot     bids=DECR   asks=CRESC
        mexc futures  bids=DECR   asks=CRESC

  Exemplo real de `asks` recebido para BTC-USDT:

        64107.80, 64107.79, 64107.46, 64107.44, 64105.09

  O MELHOR ask é o último, não o primeiro. Ler `asks[0]` como topo do book
  entregaria um preço 0,004% pior como se fosse o melhor, e — muito pior — o
  VWAP por profundidade sairia consumindo o book de trás para frente,
  reportando um preço executável melhor do que a realidade justamente na
  ponta que decide a ordem. Nada disso levanta exceção nem aparece em log.

  Por isso `declared_ask_order="desc"` é passado explicitamente aqui: o
  `build_order_book` normaliza sempre, e avisa se a BingX voltar a mudar.
"""
import logging
import time
from typing import Optional

from bot.depth import OrderBook
from exchanges.base import (
    ContractSpec, ExchangeAdapter, MarketType, Quote,
    build_order_book, levels_from_pairs,
)

logger = logging.getLogger("exchanges.bingx")

BASE = "https://open-api.bingx.com"


def _canonical(native: str) -> Optional[str]:
    if not native.endswith("-USDT"):
        return None
    base = native[: -len("-USDT")]
    return base or None


def _unwrap(payload: dict, contexto: str):
    """
    A BingX devolve HTTP 200 com `code != 0` em caso de erro. Tratar o status
    HTTP como sinal de sucesso engoliria a falha silenciosamente.
    """
    if not isinstance(payload, dict):
        return None
    codigo = payload.get("code")
    if codigo not in (0, None, "0"):
        logger.warning("BingX %s retornou code=%s msg=%s", contexto, codigo, payload.get("msg"))
        return None
    return payload.get("data")


class BingxSpotAdapter(ExchangeAdapter):
    name = "bingx"
    market = MarketType.SPOT

    def to_native(self, symbol: str) -> str:
        return f"{symbol}-USDT"

    def to_canonical(self, native_symbol: str) -> Optional[str]:
        return _canonical(native_symbol)

    async def fetch_tickers(self) -> dict[str, Quote]:
        payload = await self._get(f"{BASE}/openApi/spot/v1/ticker/24hr")
        data = _unwrap(payload, "ticker spot")
        if not data:
            return {}
        agora = time.time()
        out: dict[str, Quote] = {}
        for item in data:
            nativo = item.get("symbol", "")
            canonico = self.to_canonical(nativo)
            if not canonico:
                continue
            try:
                bid = float(item.get("bidPrice", 0) or 0)
                ask = float(item.get("askPrice", 0) or 0)
                last = float(item.get("lastPrice", 0) or 0)
                vol = float(item.get("quoteVolume", 0) or 0)
            except (TypeError, ValueError):
                continue
            # A BingX é a única das três que devolve a quantidade do topo no
            # ticker em massa. Aproveitar aqui sai de graça e poupa uma
            # consulta de profundidade por símbolo.
            try:
                bid_qty = float(item.get("bidQty", 0) or 0) or None
                ask_qty = float(item.get("askQty", 0) or 0) or None
            except (TypeError, ValueError):
                bid_qty = ask_qty = None

            out[canonico] = Quote(
                symbol=canonico, venue=self.venue, native_symbol=nativo,
                bid=bid or None, ask=ask or None, last=last or None,
                vol_usdt=vol, book_ts=agora if (bid and ask) else 0.0,
                bid_qty=bid_qty, ask_qty=ask_qty,
                top_ts=agora if (bid_qty or ask_qty) else 0.0,
            )
        return out

    async def fetch_depth(self, symbol: str, limit: int = 20) -> Optional[OrderBook]:
        payload = await self._get(
            f"{BASE}/openApi/spot/v1/market/depth",
            {"symbol": self.to_native(symbol), "limit": limit}, timeout=5,
        )
        d = _unwrap(payload, f"depth spot {symbol}")
        if not d:
            return None
        return build_order_book(
            symbol,
            levels_from_pairs(d.get("bids")),
            levels_from_pairs(d.get("asks")),
            venue=self.venue,
            # Ver docstring do módulo: este é o único venue cujos asks vêm do
            # pior para o melhor. Declarar aqui faz a normalização acontecer
            # sem gerar aviso falso, e faz um aviso REAL aparecer se mudar.
            declared_ask_order="desc",
        )

    async def fetch_specs(self, symbols: list[str]) -> dict[str, ContractSpec]:
        try:
            payload = await self._get(f"{BASE}/openApi/spot/v1/common/symbols")
        except Exception as e:
            logger.warning("Metadados spot BingX indisponíveis: %s", e)
            return {}
        data = _unwrap(payload, "symbols spot") or {}
        itens = data.get("symbols") if isinstance(data, dict) else data
        por_nativo = {i.get("symbol"): i for i in (itens or [])}
        out: dict[str, ContractSpec] = {}
        for sym in symbols:
            info = por_nativo.get(self.to_native(sym))
            if not info:
                continue
            out[sym] = ContractSpec(
                symbol=sym, venue=self.venue, native_symbol=self.to_native(sym),
                contract_size=1.0,
                qty_step=float(info.get("stepSize", 0) or 0),
                price_tick=float(info.get("tickSize", 0) or 0),
                min_qty=float(info.get("minQty", 0) or 0),
                min_notional=float(info.get("minNotional", 0) or 0),
                taker_fee_pct=0.10,  # taker padrão do spot da BingX
            )
        return out


class BingxFuturesAdapter(ExchangeAdapter):
    name = "bingx"
    market = MarketType.FUTURES

    def to_native(self, symbol: str) -> str:
        return f"{symbol}-USDT"

    def to_canonical(self, native_symbol: str) -> Optional[str]:
        return _canonical(native_symbol)

    async def fetch_tickers(self) -> dict[str, Quote]:
        payload = await self._get(f"{BASE}/openApi/swap/v2/quote/ticker")
        data = _unwrap(payload, "ticker swap")
        if not data:
            return {}
        agora = time.time()
        out: dict[str, Quote] = {}
        for item in data:
            nativo = item.get("symbol", "")
            canonico = self.to_canonical(nativo)
            if not canonico:
                continue
            try:
                bid = float(item.get("bidPrice", 0) or 0)
                ask = float(item.get("askPrice", 0) or 0)
                last = float(item.get("lastPrice", 0) or 0)
                vol = float(item.get("quoteVolume", 0) or 0)
            except (TypeError, ValueError):
                continue
            try:
                bid_qty = float(item.get("bidQty", 0) or 0) or None
                ask_qty = float(item.get("askQty", 0) or 0) or None
            except (TypeError, ValueError):
                bid_qty = ask_qty = None

            out[canonico] = Quote(
                symbol=canonico, venue=self.venue, native_symbol=nativo,
                bid=bid or None, ask=ask or None, last=last or None,
                vol_usdt=vol, book_ts=agora if (bid and ask) else 0.0,
                bid_qty=bid_qty, ask_qty=ask_qty,
                top_ts=agora if (bid_qty or ask_qty) else 0.0,
            )
        return out

    async def fetch_funding_rates(self) -> dict[str, float]:
        """
        O funding NÃO vem no ticker de swap da BingX (diferente de MEXC e
        Gate), então precisa de uma chamada própria. Sem ele, o custo de
        manter a posição fica invisível na decisão — o mesmo tipo de custo
        oculto que o módulo costs.py existe para eliminar.
        """
        try:
            payload = await self._get(f"{BASE}/openApi/swap/v2/quote/premiumIndex")
        except Exception as e:
            logger.warning("Funding da BingX indisponível: %s", e)
            return {}
        data = _unwrap(payload, "premiumIndex") or []
        out: dict[str, float] = {}
        for item in data:
            canonico = self.to_canonical(item.get("symbol", ""))
            if not canonico:
                continue
            try:
                out[canonico] = float(item.get("lastFundingRate", 0) or 0)
            except (TypeError, ValueError):
                continue
        return out

    async def fetch_depth(self, symbol: str, limit: int = 20) -> Optional[OrderBook]:
        payload = await self._get(
            f"{BASE}/openApi/swap/v2/quote/depth",
            {"symbol": self.to_native(symbol), "limit": limit}, timeout=5,
        )
        d = _unwrap(payload, f"depth swap {symbol}")
        if not d:
            return None
        ts = None
        bruto = d.get("T")
        if bruto:
            try:
                remoto = float(bruto) / 1000.0
                if abs(remoto - time.time()) < 30:
                    ts = remoto
            except (TypeError, ValueError):
                pass
        return build_order_book(
            symbol,
            levels_from_pairs(d.get("bids")),
            levels_from_pairs(d.get("asks")),
            venue=self.venue, ts=ts,
        )

    async def fetch_all_contract_sizes(self) -> dict[str, float]:
        """
        Multiplicador (`size`) de todos os contratos, numa chamada.

        Na BingX a quantidade das ordens é expressa na MOEDA BASE, não em
        contratos — mas o book de profundidade segue a mesma convenção, então
        o fator aplicável aqui é 1. O campo `size` existe e é lido para
        registro, sem ser aplicado como multiplicador.
        """
        payload = await self._get(f"{BASE}/openApi/swap/v2/quote/contracts")
        data = _unwrap(payload, "contracts") or []
        out: dict[str, float] = {}
        for info in data:
            canonico = self.to_canonical(info.get("symbol", ""))
            if canonico:
                out[canonico] = 1.0
        return out

    async def fetch_specs(self, symbols: list[str]) -> dict[str, ContractSpec]:
        try:
            payload = await self._get(f"{BASE}/openApi/swap/v2/quote/contracts")
        except Exception as e:
            logger.warning("Metadados swap BingX indisponíveis: %s", e)
            return {}
        data = _unwrap(payload, "contracts") or []
        por_nativo = {i.get("symbol"): i for i in data}
        out: dict[str, ContractSpec] = {}
        for sym in symbols:
            info = por_nativo.get(self.to_native(sym))
            if not info:
                continue
            try:
                # Na BingX o campo `size` é o multiplicador do contrato, e a
                # quantidade das ordens é expressa na MOEDA BASE (não em
                # contratos), diferente de MEXC e Gate.
                tamanho = float(info.get("size") or 1)
            except (TypeError, ValueError):
                tamanho = 1.0
            qprec = int(info.get("quantityPrecision", 4) or 4)
            pprec = int(info.get("pricePrecision", 4) or 4)
            out[sym] = ContractSpec(
                symbol=sym, venue=self.venue, native_symbol=self.to_native(sym),
                contract_size=tamanho,
                qty_step=10 ** (-qprec), price_tick=10 ** (-pprec),
                min_qty=float(info.get("tradeMinQuantity", 0) or 0),
                min_notional=float(info.get("tradeMinUSDT", 0) or 0),
                taker_fee_pct=_taker_pct(info.get("takerFeeRate"), 0.05),
            )
        return out


def _taker_pct(bruto, padrao: float) -> float:
    if bruto is None:
        return padrao
    try:
        pct = float(bruto) * 100
    except (TypeError, ValueError):
        return padrao
    if 0 <= pct <= 1.0:
        return pct
    logger.warning("Taxa implausível na BingX (%s -> %.4f%%); usando %.3f%%.", bruto, pct, padrao)
    return padrao
