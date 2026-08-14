"""
Cliente de leitura de book PÚBLICO (sem credenciais).

Os endpoints de profundidade da MEXC (`/api/v3/depth` no Spot e
`/api/v1/contract/depth/{symbol}` no Futures) não exigem assinatura. Ter um
cliente próprio para eles, separado dos clientes autenticados, resolve dois
problemas de uma vez:

1. A confirmação por profundidade passa a funcionar em modo SIMULAÇÃO mesmo
   sem chaves de API no `.env`. Isso importa: uma simulação que decide pelo
   topo do book mentiria exatamente do mesmo jeito que a tela mentia, e
   deixaria de servir para o que ela existe — validar a estratégia antes de
   arriscar dinheiro.

2. Nenhuma chamada de leitura de mercado carrega credencial junto. Reduz a
   superfície de exposição das chaves e o consumo do rate limit autenticado,
   que é o que a execução das ordens realmente precisa ter livre.

Deliberadamente sem lógica: só busca e devolve o JSON cru. Toda a
interpretação vive em bot/depth.py, que é código puro e testável sem rede.
"""
import logging

import httpx

logger = logging.getLogger("bot_market_data")

SPOT_BASE_URL = "https://api.mexc.com"
FUTURES_BASE_URL = "https://api.mexc.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# Timeout curto de propósito: este dado alimenta uma decisão de execução
# imediata. Um book que leva 5s para chegar já não descreve o mercado em que
# a ordem vai cair — melhor falhar rápido e o bot pular o ciclo do que
# decidir com dado velho.
DEPTH_TIMEOUT_S = 5.0


class PublicSpotMarketClient:
    def __init__(self, http_client: httpx.AsyncClient):
        self._client = http_client

    async def get_depth(self, symbol: str, limit: int = 20) -> dict:
        resp = await self._client.get(
            f"{SPOT_BASE_URL}/api/v3/depth",
            params={"symbol": symbol, "limit": limit},
            headers=HEADERS, timeout=DEPTH_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()


class PublicFuturesMarketClient:
    def __init__(self, http_client: httpx.AsyncClient):
        self._client = http_client

    async def get_depth(self, symbol: str, limit: int = 20) -> dict:
        resp = await self._client.get(
            f"{FUTURES_BASE_URL}/api/v1/contract/depth/{symbol}",
            params={"limit": limit},
            headers=HEADERS, timeout=DEPTH_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
