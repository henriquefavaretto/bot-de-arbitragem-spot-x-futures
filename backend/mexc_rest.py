"""Cliente REST para os endpoints públicos da MEXC (spot + futures)."""
import httpx
import logging

from config import MEXC_SPOT_TICKER_24H, MEXC_FUTURES_TICKER_ALL

logger = logging.getLogger("mexc_rest")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


async def fetch_spot_tickers(client: httpx.AsyncClient) -> dict:
    """
    Busca todos os tickers spot 24h.

    O campo "price" retornado é o ASK (melhor preço de VENDA do book), que é
    o preço que efetivamente se paga ao comprar a mercado - a perna Spot da
    estratégia é sempre de COMPRA. Usar o ask em vez do lastPrice faz o
    spread calculado refletir o resultado real de executar a operação agora,
    em vez de um preço teórico de um negócio que já aconteceu.

    Retorna {symbol_spot: {"price", "bid", "ask", "last", "vol"}} apenas para
    pares *USDT (bid/last mantidos para referência e diagnóstico).
    """
    resp = await client.get(MEXC_SPOT_TICKER_24H, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    result = {}
    for item in data:
        symbol = item.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        try:
            last = float(item["lastPrice"])
            vol = float(item["quoteVolume"])
        except (KeyError, ValueError, TypeError):
            continue

        try:
            bid = float(item.get("bidPrice", 0) or 0)
            ask = float(item.get("askPrice", 0) or 0)
        except (ValueError, TypeError):
            bid, ask = 0.0, 0.0

        # Quantidade no TOPO do book. A MEXC devolve isso de graça no ticker
        # 24h do spot, então a profundidade do primeiro nível não custa uma
        # chamada a mais. É o número que responde "quanto cabe NESTE preço" -
        # a informação que faltava quando 1,82 ponto percentual sumiu em
        # execução (ver bot/depth.py).
        try:
            bid_qty = float(item.get("bidQty", 0) or 0)
            ask_qty = float(item.get("askQty", 0) or 0)
        except (ValueError, TypeError):
            bid_qty, ask_qty = 0.0, 0.0

        # Máxima e mínima de 24h, para a volatilidade. Também de graça aqui.
        try:
            high = float(item.get("highPrice", 0) or 0)
            low = float(item.get("lowPrice", 0) or 0)
        except (ValueError, TypeError):
            high, low = 0.0, 0.0

        # Fallback para lastPrice se o book vier vazio (par sem liquidez no
        # momento) - melhor um preço aproximado do que nenhum.
        price = ask if ask > 0 else last

        result[symbol] = {
            "price": price,
            "bid": bid,
            "ask": ask,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "high_24h": high,
            "low_24h": low,
            "last": last,
            "vol": vol,
        }
    return result


async def fetch_futures_tickers(client: httpx.AsyncClient) -> dict:
    """
    Busca todos os tickers de futures (contratos perpétuos).

    O campo "price" retornado é o BID (melhor preço de COMPRA do book, campo
    `bid1` da API), que é o preço que efetivamente se recebe ao VENDER
    (abrir short) a mercado - a perna Futures da estratégia é sempre de
    venda. Mesma lógica do lado Spot: reflete a execução real, não um
    negócio passado.

    Retorna {symbol_futures ("EWT_USDT"): {"price", "bid", "ask", "last", "vol", "funding_rate"}}
    """
    resp = await client.get(MEXC_FUTURES_TICKER_ALL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if not payload.get("success"):
        logger.warning("Futures ticker response success=False: %s", payload)
        return {}

    data = payload.get("data", [])
    result = {}
    for item in data:
        symbol = item.get("symbol", "")
        if not symbol.endswith("_USDT"):
            continue
        try:
            last = float(item["lastPrice"])
            vol = float(item.get("amount24", 0) or 0)
            funding_rate = float(item.get("fundingRate", 0) or 0)
        except (ValueError, TypeError):
            continue

        try:
            bid = float(item.get("bid1", 0) or 0)
            ask = float(item.get("ask1", 0) or 0)
        except (ValueError, TypeError):
            bid, ask = 0.0, 0.0

        # O ticker de futures NÃO traz quantidade do topo (diferente do spot),
        # só preço. A profundidade do futures precisa de consulta separada -
        # ver `futures_depth_enrichment_loop` em engine.py.
        try:
            high = float(item.get("high24Price", 0) or 0)
            low = float(item.get("lower24Price", 0) or 0)
        except (ValueError, TypeError):
            high, low = 0.0, 0.0

        price = bid if bid > 0 else last

        result[symbol] = {
            "price": price,
            "bid": bid,
            "ask": ask,
            "high_24h": high,
            "low_24h": low,
            "last": last,
            "vol": vol,
            "funding_rate": funding_rate,
        }
    return result


async def fetch_funding_rates(client: httpx.AsyncClient) -> dict:
    """Busca só funding rates atuais via o mesmo endpoint de ticker (mais simples e já cacheado)."""
    futures = await fetch_futures_tickers(client)
    return {sym: v["funding_rate"] for sym, v in futures.items()}


def build_pair_universe(spot_tickers: dict, futures_tickers: dict) -> dict:
    """
    Casa pares que existem tanto em spot quanto em futures.
    Ex: spot "EWTUSDT" <-> futures "EWT_USDT" => display "EWT"

    Retorna {display_symbol: {"spot_symbol": ..., "futures_symbol": ...}}
    """
    universe = {}
    for fut_symbol in futures_tickers:
        base = fut_symbol[:-len("_USDT")]
        spot_symbol = f"{base}USDT"
        if spot_symbol in spot_tickers:
            universe[base] = {"spot_symbol": spot_symbol, "futures_symbol": fut_symbol}
    return universe
