"""
Adaptadores da Gate.io (spot e futures perpétuo USDT).

Peculiaridades confirmadas contra a API real:
- Símbolo `BTC_USDT` nos DOIS mercados — a única das três exchanges com
  formato consistente entre spot e futures.
- Spot: `/spot/tickers` traz `highest_bid`/`lowest_ask` de 2237 pares numa
  chamada. Os nomes dos campos são invertidos em relação à intuição
  (`lowest_ask` é o melhor ask, `highest_bid` o melhor bid), mas semanticamente
  corretos.
- Futures: o book usa `[{"s": tamanho, "p": preco}]` — objetos, não listas.
  É o único dos seis venues nesse formato.
- Futures: `s` está em CONTRATOS; o multiplicador é `quanto_multiplier`
  (0.0001 para BTC), não `contractSize` como na MEXC.
- `taker_fee_rate` da Gate em futures é 0,075% — mais que o triplo da MEXC
  (0,02%). Numa estratégia com alvo de 1-2%, essa diferença decide se um par
  vale a pena; por isso a taxa é lida por venue e nunca assumida.
- Ambos os books chegam com bids decrescentes e asks crescentes.
"""
import logging
import time
from typing import Optional

from bot.depth import OrderBook
from exchanges.base import (
    ContractSpec, ExchangeAdapter, MarketType, Quote,
    build_order_book, levels_from_dicts, levels_from_pairs,
)

logger = logging.getLogger("exchanges.gate")

BASE = "https://api.gateio.ws/api/v4"


def _canonical(native: str) -> Optional[str]:
    if not native.endswith("_USDT"):
        return None
    base = native[: -len("_USDT")]
    return base or None


class GateSpotAdapter(ExchangeAdapter):
    name = "gate"
    market = MarketType.SPOT

    def to_native(self, symbol: str) -> str:
        return f"{symbol}_USDT"

    def to_canonical(self, native_symbol: str) -> Optional[str]:
        return _canonical(native_symbol)

    async def fetch_tickers(self) -> dict[str, Quote]:
        data = await self._get(f"{BASE}/spot/tickers")
        agora = time.time()
        out: dict[str, Quote] = {}
        for item in data:
            nativo = item.get("currency_pair", "")
            canonico = self.to_canonical(nativo)
            if not canonico:
                continue
            try:
                # `lowest_ask` é o MELHOR ask (menor preço de venda) e
                # `highest_bid` o melhor bid. Os nomes descrevem o extremo do
                # lado, não a qualidade da oferta.
                ask = float(item.get("lowest_ask") or 0)
                bid = float(item.get("highest_bid") or 0)
                last = float(item.get("last") or 0)
                vol = float(item.get("quote_volume") or 0)
            except (TypeError, ValueError):
                continue
            out[canonico] = Quote(
                symbol=canonico, venue=self.venue, native_symbol=nativo,
                bid=bid or None, ask=ask or None, last=last or None,
                vol_usdt=vol, book_ts=agora if (bid and ask) else 0.0,
            )
        return out

    async def fetch_depth(self, symbol: str, limit: int = 20) -> Optional[OrderBook]:
        d = await self._get(
            f"{BASE}/spot/order_book",
            {"currency_pair": self.to_native(symbol), "limit": limit}, timeout=5,
        )
        return build_order_book(
            symbol,
            levels_from_pairs(d.get("bids")),
            levels_from_pairs(d.get("asks")),
            venue=self.venue,
        )

    async def fetch_specs(self, symbols: list[str]) -> dict[str, ContractSpec]:
        try:
            pares = await self._get(f"{BASE}/spot/currency_pairs")
        except Exception as e:
            logger.warning("Metadados spot Gate indisponíveis: %s", e)
            return {}
        por_nativo = {p.get("id"): p for p in pares}
        out: dict[str, ContractSpec] = {}
        for sym in symbols:
            info = por_nativo.get(self.to_native(sym))
            if not info:
                continue
            amount_prec = int(info.get("amount_precision", 8) or 8)
            price_prec = int(info.get("precision", 8) or 8)
            out[sym] = ContractSpec(
                symbol=sym, venue=self.venue, native_symbol=self.to_native(sym),
                contract_size=1.0,
                qty_step=10 ** (-amount_prec), price_tick=10 ** (-price_prec),
                min_notional=float(info.get("min_quote_amount", 0) or 0),
                # A Gate não expõe a taxa por par sem autenticação; 0,20% é o
                # taker padrão do nível base e é conservador (se a sua conta
                # pagar menos, o bot só fica um pouco mais exigente).
                taker_fee_pct=0.20,
            )
        return out


class GateFuturesAdapter(ExchangeAdapter):
    name = "gate"
    market = MarketType.FUTURES

    def to_native(self, symbol: str) -> str:
        return f"{symbol}_USDT"

    def to_canonical(self, native_symbol: str) -> Optional[str]:
        return _canonical(native_symbol)

    async def fetch_tickers(self) -> dict[str, Quote]:
        data = await self._get(f"{BASE}/futures/usdt/tickers")
        agora = time.time()
        out: dict[str, Quote] = {}
        for item in data:
            nativo = item.get("contract", "")
            canonico = self.to_canonical(nativo)
            if not canonico:
                continue
            try:
                bid = float(item.get("highest_bid") or 0)
                ask = float(item.get("lowest_ask") or 0)
                last = float(item.get("last") or 0)
                vol = float(item.get("volume_24h_quote") or 0)
                funding = float(item.get("funding_rate") or 0)
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
        d = await self._get(
            f"{BASE}/futures/usdt/order_book",
            {"contract": self.to_native(symbol), "limit": limit}, timeout=5,
        )
        ts = None
        bruto = d.get("current")
        if bruto:
            try:
                # A Gate manda `current` em SEGUNDOS (com fração), diferente
                # dos milissegundos de MEXC e BingX.
                remoto = float(bruto)
                if abs(remoto - time.time()) < 30:
                    ts = remoto
            except (TypeError, ValueError):
                pass
        return build_order_book(
            symbol,
            # Único venue com book em objetos {s, p} em vez de listas.
            levels_from_dicts(d.get("bids")),
            levels_from_dicts(d.get("asks")),
            venue=self.venue, ts=ts,
        )

    async def fetch_all_contract_sizes(self) -> dict[str, float]:
        """Multiplicador (`quanto_multiplier`) de todos os contratos, numa chamada."""
        itens = await self._get(f"{BASE}/futures/usdt/contracts")
        out: dict[str, float] = {}
        for info in itens or []:
            canonico = self.to_canonical(info.get("name", ""))
            if not canonico:
                continue
            try:
                out[canonico] = float(info.get("quanto_multiplier") or 1)
            except (TypeError, ValueError):
                continue
        return out

    async def fetch_specs(self, symbols: list[str]) -> dict[str, ContractSpec]:
        out: dict[str, ContractSpec] = {}
        for sym in symbols:
            nativo = self.to_native(sym)
            try:
                info = await self._get(f"{BASE}/futures/usdt/contracts/{nativo}")
            except Exception as e:
                logger.warning("Metadados futures Gate de %s indisponíveis: %s", sym, e)
                continue
            if not isinstance(info, dict):
                continue
            try:
                # `quanto_multiplier` é o equivalente de `contractSize`.
                mult = float(info.get("quanto_multiplier") or 1)
            except (TypeError, ValueError):
                mult = 1.0
            try:
                tick = float(info.get("order_price_round") or 0)
            except (TypeError, ValueError):
                tick = 0.0
            out[sym] = ContractSpec(
                symbol=sym, venue=self.venue, native_symbol=nativo,
                contract_size=mult,
                qty_step=1.0,  # contratos são inteiros na Gate
                price_tick=tick,
                min_qty=float(info.get("order_size_min", 1) or 1),
                taker_fee_pct=_taker_pct(info.get("taker_fee_rate"), 0.075),
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
    logger.warning("Taxa implausível na Gate (%s -> %.4f%%); usando %.3f%%.", bruto, pct, padrao)
    return padrao
