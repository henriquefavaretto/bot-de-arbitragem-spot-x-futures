"""Configurações centrais do backend."""

# --- MEXC REST endpoints ---
MEXC_SPOT_REST_BASE = "https://api.mexc.com"
MEXC_SPOT_TICKER_24H = f"{MEXC_SPOT_REST_BASE}/api/v3/ticker/24hr"

# Domínio de futures mudou para api.mexc.com em 2026-01-19 (antes: contract.mexc.com)
MEXC_FUTURES_REST_BASE = "https://api.mexc.com"
MEXC_FUTURES_TICKER_ALL = f"{MEXC_FUTURES_REST_BASE}/api/v1/contract/ticker"

# --- MEXC WebSocket endpoints ---
MEXC_SPOT_WS_URL = "wss://wbs-api.mexc.com/ws"
# WS de futures continua no domínio antigo (só o REST mudou)
MEXC_FUTURES_WS_URL = "wss://contract.mexc.com/edge"

# --- Links para o frontend ---
MEXC_SPOT_TRADE_URL = "https://www.mexc.com/exchange/{symbol}_USDT"
MEXC_FUTURES_TRADE_URL = "https://futures.mexc.com/exchange/{symbol}_USDT"

# --- Polling / refresh intervals (segundos) ---
FUNDING_RATE_POLL_INTERVAL = 30      # (legado) mantido para compatibilidade
# Polling REST do ticker de futures: traz bid1/ask1 (preço de execução) para
# TODOS os pares, além do funding rate. Mais frequente que o antigo intervalo
# de funding porque agora também alimenta preço - especialmente importante
# para os pares que não têm subscrição WebSocket individual.
FUTURES_REST_POLL_INTERVAL = 5
REST_FALLBACK_POLL_INTERVAL = 5      # fallback caso o websocket caia
PAIR_DISCOVERY_INTERVAL = 300        # re-descobrir pares novos listados (spot ∩ futures) a cada 5 min
CROSSING_WINDOWS_REFRESH_INTERVAL = 10  # recalcular contagem de cruzamentos (1h/12h/24h) a cada 10s
WS_RECONNECT_DELAY = 3               # segundos entre tentativas de reconexão
SPOT_WS_MAX_LIFETIME = 23 * 3600     # reconectar preventivamente antes do limite de 24h da MEXC

# --- Histórico / cruzamentos ---
SPREAD_HISTORY_MAX_POINTS = 300      # pontos mantidos em memória para o sparkline (por par)
DB_PATH = "arb_dashboard.db"

# --- Filtro inicial (equivalente ao Apps Script: |spread| > 0.5%) ---
DEFAULT_MIN_SPREAD_ABS_PCT = 0.0  # 0 = não filtra no backend; filtro fica a cargo do frontend
