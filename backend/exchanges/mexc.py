"""
Adaptadores da MEXC (spot e futures perpétuo).

Peculiaridades confirmadas contra a API real:
- Spot usa `BTCUSDT`, Futures usa `BTC_USDT`. Formatos diferentes na MESMA
  exchange — foi essa divergência que causou o bug 2 do projeto.
- O ticker 24h do spot já traz `bidPrice`/`askPrice`, então não é preciso uma
  segunda chamada para ter o topo do book de todos os pares.
- O ticker de futures traz `bid1`/`ask1` para TODOS os contratos numa
  chamada — é a fonte mais completa, e a razão de o polling REST continuar
  existindo mesmo com WebSocket.
- Os dois books chegam com bids decrescentes e asks crescentes.
"""
import logging
from typing import Optional

from bot.depth import OrderBook
from exchanges.base import (
    ContractSpec, ExchangeAdapter, MarketType, Quote,
    build_order_book, levels_from_pairs,
)

logger = logging.getLogger("exchanges.mexc")

BASE = "https://api.mexc.com"


class MexcSpotAdapter(ExchangeAdapter):
    name = "mexc"
    market = MarketType.SPOT

    def to_native(self, symbol: str) -> str:
        return f"{symbol}USDT"

    def to_canonical(self, native_symbol: str) -> Optional[str]:
        if not native_symbol.endswith("USDT") or len(native_symbol) <= 4:
            return None
        return native_symbol[:-4]

    async def fetch_tickers(self) -> dict[str, Quote]:
        data = await self._get(f"{BASE}/api/v3/ticker/24hr")
        import time
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
            out[canonico] = Quote(
                symbol=canonico, venue=self.venue, native_symbol=nativo,
                bid=bid or None, ask=ask or None, last=last or None,
                vol_usdt=vol, book_ts=agora if (bid and ask) else 0.0,
            )
        return out

    async def fetch_depth(self, symbol: str, limit: int = 20) -> Optional[OrderBook]:
        nativo = self.to_native(symbol)
        d = await self._get(f"{BASE}/api/v3/depth", {"symbol": nativo, "limit": limit}, timeout=5)
        return build_order_book(
            symbol,
            levels_from_pairs(d.get("bids")),
            levels_from_pairs(d.get("asks")),
            venue=self.venue,
        )

    async def fetch_specs(self, symbols: list[str]) -> dict[str, ContractSpec]:
        out: dict[str, ContractSpec] = {}
        for sym in symbols:
            nativo = self.to_native(sym)
            try:
                d = await self._get(f"{BASE}/api/v3/exchangeInfo", {"symbol": nativo})
            except Exception as e:
                logger.warning("Metadados spot MEXC de %s indisponíveis: %s", sym, e)
                continue
            infos = d.get("symbols") or []
            if not infos:
                continue
            info = infos[0]
            base_prec = int(info.get("baseAssetPrecision", 8))
            price_prec = int(info.get("quoteAssetPrecision", info.get("quotePrecision", 8)) or 8)
            taker = _pct_plausivel(info.get("takerCommission"), 0.05)
            min_notional = 0.0
            for f in info.get("filters", []):
                if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("minNotional", f.get("minNotionalValue", 0)) or 0)
            out[sym] = ContractSpec(
                symbol=sym, venue=self.venue, native_symbol=nativo,
                contract_size=1.0,
                qty_step=10 ** (-base_prec), price_tick=10 ** (-price_prec),
                min_notional=min_notional, taker_fee_pct=taker,
            )
        return out


class MexcFuturesAdapter(ExchangeAdapter):
    name = "mexc"
    market = MarketType.FUTURES

    def to_native(self, symbol: str) -> str:
        return f"{symbol}_USDT"

    def to_canonical(self, native_symbol: str) -> Optional[str]:
        if not native_symbol.endswith("_USDT"):
            return None
        return native_symbol[: -len("_USDT")]

    async def fetch_tickers(self) -> dict[str, Quote]:
        payload = await self._get(f"{BASE}/api/v1/contract/ticker")
        if not payload.get("success"):
            logger.warning("Ticker de futures MEXC retornou success=False")
            return {}
        import time
        agora = time.time()
        out: dict[str, Quote] = {}
        for item in payload.get("data", []):
            nativo = item.get("symbol", "")
            canonico = self.to_canonical(nativo)
            if not canonico:
                continue
            try:
                bid = float(item.get("bid1", 0) or 0)
                ask = float(item.get("ask1", 0) or 0)
                last = float(item.get("lastPrice", 0) or 0)
                vol = float(item.get("amount24", 0) or 0)
                funding = float(item.get("fundingRate", 0) or 0)
            except (TypeError, ValueError):
                continue
            out[canonico] = Quote(
                symbol=canonico, venue=self.venue, native_symbol=nativo,
                bid=bid or None, ask=ask or None, last=last or None,
                vol_usdt=vol, funding_rate=funding,
                book_ts=agora if (bid and ask) else 0.0,
            )
        return out

    async def fetch_depth(self, symbol: str, limit: int = 20) -> Optional[OrderBook]:
        nativo = self.to_native(symbol)
        payload = await self._get(f"{BASE}/api/v1/contract/depth/{nativo}", {"limit": limit}, timeout=5)
        if not payload.get("success", True):
            return None
        d = payload.get("data") or {}
        ts = None
        raw_ts = d.get("timestamp")
        if raw_ts:
            import time
            try:
                remoto = float(raw_ts) / 1000.0
                # Só aceita o relógio do servidor se ele for plausível: um ts
                # "do futuro" faria o book parecer eternamente fresco e
                # derrubaria a proteção de staleness.
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
        Multiplicador de TODOS os contratos numa chamada.

        Sem ele, a quantidade do book de futures (que vem em CONTRATOS) não
        pode virar valor em USDT: o contrato de JIMOTHY vale 100 moedas e o
        de BTC vale 0,0001, então errar aqui produz números fora por ordens
        de grandeza — e a coluna de profundidade viraria ficção.
        """
        d = await self._get(f"{BASE}/api/v1/contract/detail")
        itens = d.get("data") or []
        out: dict[str, float] = {}
        for info in itens:
            canonico = self.to_canonical(info.get("symbol", ""))
            if not canonico:
                continue
            try:
                out[canonico] = float(info["contractSize"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def fetch_specs(self, symbols: list[str]) -> dict[str, ContractSpec]:
        out: dict[str, ContractSpec] = {}
        for sym in symbols:
            nativo = self.to_native(sym)
            try:
                d = await self._get(f"{BASE}/api/v1/contract/detail", {"symbol": nativo})
            except Exception as e:
                logger.warning("Metadados futures MEXC de %s indisponíveis: %s", sym, e)
                continue
            info = d.get("data")
            if isinstance(info, list):
                info = info[0] if info else None
            if not info:
                continue
            price_scale = int(info.get("priceScale", 8) or 8)
            try:
                tick = float(info.get("priceUnit", 0) or 0)
            except (TypeError, ValueError):
                tick = 0.0
            out[sym] = ContractSpec(
                symbol=sym, venue=self.venue, native_symbol=nativo,
                contract_size=float(info["contractSize"]),
                qty_step=10 ** (-int(info.get("volScale", 0) or 0)),
                price_tick=tick if tick > 0 else 10 ** (-price_scale),
                min_qty=float(info.get("minVol", 1) or 1),
                taker_fee_pct=_pct_plausivel(info.get("takerFeeRate"), 0.02),
            )
        return out


def _pct_plausivel(bruto, padrao: float) -> float:
    """
    Converte uma taxa em fração (0.0005) para percentual (0.05), aceitando
    apenas valores fisicamente plausíveis para uma taxa de exchange.

    Um erro de fator 100 aqui inverteria a decisão de entrada sem nenhum
    sintoma visível, então fora da faixa preferimos o padrão conservador.
    """
    if bruto is None:
        return padrao
    try:
        pct = float(bruto) * 100
    except (TypeError, ValueError):
        return padrao
    if 0 <= pct <= 1.0:
        return pct
    logger.warning("Taxa implausível (%s -> %.4f%%); usando o padrão %.3f%%.", bruto, pct, padrao)
    return padrao
