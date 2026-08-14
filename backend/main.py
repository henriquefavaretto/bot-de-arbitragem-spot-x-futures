import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import ArbitrageEngine
from multi_engine import MultiVenueEngine, evaluate_consensus
from multi_storage import MultiStorage
from exchanges.registry import build_adapters, parse_venue_filter
from storage import Storage
from bot.bot_storage import BotStorage
from bot.bot_engine import ArbitrageBotEngine, ExecutionMode
from bot.mexc_spot_client import MexcSpotClient
from bot.mexc_futures_client import MexcFuturesClient
from bot.execution import SlippagePolicy
from bot.market_data import PublicSpotMarketClient, PublicFuturesMarketClient
from bot.gate_client import GateClient
from bot.bingx_client import BingxClient
from bot.sizing import ContractSpec, SpotSymbolSpec
from bot.log_buffer import install_in_memory_log_handler

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("main")

_log_buffer = install_in_memory_log_handler()

# ---------------------------------------------------------------------------
# TRAVA DE SEGURANÇA: o modo LIVE só é possível com a variável de ambiente
# MEXC_BOT_LIVE_MODE=true explicitamente definida no .env. Isso é proposital -
# é um passo manual extra, separado de qualquer toggle da interface, para que
# ativar execução real exija uma ação deliberada de edição de arquivo, não um
# clique acidental num botão.
# ---------------------------------------------------------------------------
LIVE_MODE_ENABLED = os.getenv("MEXC_BOT_LIVE_MODE", "false").strip().lower() == "true"
MAX_TOTAL_EXPOSURE_USDT = float(os.getenv("MEXC_BOT_MAX_TOTAL_EXPOSURE_USDT", "500"))

# ---------------------------------------------------------------------------
# Controle de qualidade de execução. Todos os valores abaixo existem por causa
# do episódio de 03/08/2026, em que 1,82 ponto percentual - o lucro inteiro de
# uma operação - foi consumido por slippage que nem a tela nem o log
# enxergavam. Ver bot/depth.py para os números medidos.
# ---------------------------------------------------------------------------

# Tolerância de preço das ordens IOC, SOBRE o VWAP já calculado com a
# profundidade real do book. Como o custo de andar o book já está embutido no
# preço de referência, esta tolerância cobre apenas o que o mercado se move
# entre a decisão e a ordem chegar na MEXC - por isso pode ser pequena.
MAX_SLIPPAGE_PCT = float(os.getenv("MEXC_BOT_MAX_SLIPPAGE_PCT", "0.30"))
EXEC_MAX_ATTEMPTS = int(os.getenv("MEXC_BOT_EXEC_MAX_ATTEMPTS", "3"))
# Escalonar o resíduo para ordem a mercado quando o teto não comportar tudo.
# Ligado por padrão: numa estratégia neutra, terminar com uma perna aberta é
# pior do que pagar caro no pedaço que faltou.
EXEC_ESCALATE_TO_MARKET = os.getenv("MEXC_BOT_ESCALATE_TO_MARKET", "true").strip().lower() == "true"

# Idade máxima de cada lado do book para uma decisão de execução ser aceita.
MAX_BOOK_AGE_S = float(os.getenv("MEXC_BOT_MAX_BOOK_AGE_S", "3.0"))
# Camadas de book buscadas na confirmação de profundidade.
DEPTH_LIMIT = int(os.getenv("MEXC_BOT_DEPTH_LIMIT", "20"))
# Permanência estimada da posição, usada só para estimar o custo de funding.
EXPECTED_HOLD_HOURS = float(os.getenv("MEXC_BOT_EXPECTED_HOLD_HOURS", "1.0"))
# Margem líquida mínima exigida (em pp) para autorizar uma entrada, depois de
# descontar o alvo de saída, as taxas e o funding.
MIN_NET_PCT = float(os.getenv("MEXC_BOT_MIN_NET_PCT", "0.0"))

# Pausa entre tentativas de ordem. O endpoint de ordens de futures da MEXC
# aceita ~2 ordens a cada ~2s e devolve erro 510 no resto; com pausas curtas
# demais, a 3ª tentativa e o escalonamento a mercado nem chegam a virar ordem.
EXEC_ATTEMPT_DELAY_S = float(os.getenv("MEXC_BOT_ATTEMPT_DELAY_S", "1.0"))

SLIPPAGE_POLICY = SlippagePolicy(
    max_slippage_pct=MAX_SLIPPAGE_PCT,
    max_attempts=EXEC_MAX_ATTEMPTS,
    escalate_to_market=EXEC_ESCALATE_TO_MARKET,
    attempt_delay_s=EXEC_ATTEMPT_DELAY_S,
)

storage = Storage()
bot_storage = BotStorage()

_bot_http_client = httpx.AsyncClient()

_spot_key = os.getenv("MEXC_SPOT_API_KEY", "")
_spot_secret = os.getenv("MEXC_SPOT_SECRET_KEY", "")
_fut_key = os.getenv("MEXC_FUTURES_API_KEY", "")
_fut_secret = os.getenv("MEXC_FUTURES_SECRET_KEY", "")
_has_credentials = all([_spot_key, _spot_secret, _fut_key, _fut_secret])

# Clientes de CONSULTA (saldo, posições) - disponíveis sempre que houver
# credenciais no .env, independente do modo de execução (SIMULATION ou LIVE).
# São os mesmos clientes usados para enviar ordens, mas em modo SIMULATION o
# bot_engine nunca invoca métodos de ordem neles - só os endpoints de saldo
# abaixo (bot_balance) os utilizam nesse modo.
_query_spot_client = None
_query_futures_client = None
if _has_credentials:
    _query_spot_client = MexcSpotClient(_spot_key, _spot_secret, _bot_http_client)
    _query_futures_client = MexcFuturesClient(_fut_key, _fut_secret, _bot_http_client)
else:
    logger.info("Credenciais da MEXC não configuradas no .env - saldo Spot/Futures não estará disponível no dashboard.")

# Clientes de EXECUÇÃO (enviar ordens reais) - só existem em modo LIVE.
_spot_client = None
_futures_client = None
_execution_mode = ExecutionMode.SIMULATION

if LIVE_MODE_ENABLED:
    if not _has_credentials:
        logger.critical(
            "MEXC_BOT_LIVE_MODE=true mas as credenciais no .env estão incompletas. "
            "Iniciando em modo SIMULAÇÃO por segurança até isso ser corrigido."
        )
    else:
        _spot_client = _query_spot_client
        _futures_client = _query_futures_client
        _execution_mode = ExecutionMode.LIVE
        logger.warning(
            "=" * 70 + "\n"
            "MODO LIVE ATIVADO. O bot enviará ordens REAIS na sua conta MEXC.\n"
            "Teto de exposição global: %.2f USDT.\n" + "=" * 70,
            MAX_TOTAL_EXPOSURE_USDT,
        )

# ---------------------------------------------------------------------------
# Credenciais de Gate e BingX (opcionais).
#
# Ausentes, o dashboard continua completo: monitoramento e confirmação por
# profundidade usam só endpoints públicos. O que fica indisponível é execução
# e leitura de saldo naqueles venues — e o bot recusa explicitamente qualquer
# combinação cujo venue não tenha cliente, em vez de tentar e falhar no meio
# da operação com uma perna aberta.
# ---------------------------------------------------------------------------
_gate_key = os.getenv("GATE_API_KEY", "")
_gate_secret = os.getenv("GATE_SECRET_KEY", "")
_bingx_key = os.getenv("BINGX_API_KEY", "")
_bingx_secret = os.getenv("BINGX_SECRET_KEY", "")

_gate_client = None
_bingx_client = None
if _gate_key and _gate_secret:
    _gate_client = GateClient(_gate_key, _gate_secret, _bot_http_client)
    logger.info("Credenciais da Gate configuradas: execução e saldo disponíveis.")
if _bingx_key and _bingx_secret:
    _bingx_client = BingxClient(_bingx_key, _bingx_secret, _bot_http_client)
    logger.info("Credenciais da BingX configuradas: execução e saldo disponíveis.")

# Cliente autenticado por VENUE. É o mapa que o bot consulta antes de aceitar
# operar uma combinação: sem cliente dos DOIS lados, a combinação é recusada
# na decisão, nunca no meio da execução.
VENUE_CLIENTS = {
    "mexc:spot": _query_spot_client,
    "mexc:futures": _query_futures_client,
    "gate:spot": _gate_client,
    "gate:futures": _gate_client,
    "bingx:spot": _bingx_client,
    "bingx:futures": _bingx_client,
}

TRADABLE_VENUES = sorted(k for k, v in VENUE_CLIENTS.items() if v is not None)
logger.info(
    "Venues com execução habilitada: %s",
    ", ".join(TRADABLE_VENUES) if TRADABLE_VENUES else "nenhum (só monitoramento)",
)

# Clientes de leitura de book (endpoints públicos de profundidade). Existem
# sempre, com ou sem credenciais, para que a confirmação por profundidade
# funcione também em modo SIMULAÇÃO - uma simulação que decidisse pelo topo do
# book mentiria do mesmo jeito que a tela mentia, e deixaria de servir para
# validar a estratégia antes do dinheiro real.
_market_spot_client = PublicSpotMarketClient(_bot_http_client)
_market_futures_client = PublicFuturesMarketClient(_bot_http_client)

# Operacao ENTRE exchanges diferentes fica desligada por padrao. Exige saldo
# pre-posicionado nos dois lados e, quando uma perna falha, nao da para
# reverter na mesma conta -- que e o que `_revert_futures_leg` faz hoje.
# Ligar isso antes de o caminho de mesma-exchange estar validado repetiria o
# bug 15 num cenario sem volta.
ALLOW_CROSS_EXCHANGE = os.getenv("BOT_ALLOW_CROSS_EXCHANGE", "false").strip().lower() == "true"

# Adaptadores publicos por venue: e por eles que o bot le profundidade e
# metadados do venue CONFIGURADO em cada par, em vez de assumir MEXC.
_venue_adapters = build_adapters(_bot_http_client)

bot_engine = ArbitrageBotEngine(
    bot_storage,
    execution_mode=_execution_mode,
    spot_client=_spot_client,
    futures_client=_futures_client,
    max_total_exposure_usdt=MAX_TOTAL_EXPOSURE_USDT,
    slippage_policy=SLIPPAGE_POLICY,
    market_spot_client=_market_spot_client,
    market_futures_client=_market_futures_client,
    max_book_age_s=MAX_BOOK_AGE_S,
    depth_limit=DEPTH_LIMIT,
    expected_hold_hours=EXPECTED_HOLD_HOURS,
    min_net_pct=MIN_NET_PCT,
    venue_clients=VENUE_CLIENTS,
    venue_adapters=_venue_adapters,
    allow_cross_exchange=ALLOW_CROSS_EXCHANGE,
)

logger.info(
    "Qualidade de execução: teto de slippage %.2f%% sobre o VWAP de profundidade, "
    "%d tentativas IOC, escalonamento a mercado %s, book aceito até %.1fs de idade, "
    "%d camadas por consulta, margem líquida mínima %.2f%%.",
    MAX_SLIPPAGE_PCT, EXEC_MAX_ATTEMPTS,
    "LIGADO" if EXEC_ESCALATE_TO_MARKET else "DESLIGADO",
    MAX_BOOK_AGE_S, DEPTH_LIMIT, MIN_NET_PCT,
)


async def _on_price_update_for_bot(
    symbol: str, spot_price: float, futures_price: float, spread_pct: float,
    prices_from_book: bool = True,
    exit_spread_pct: float | None = None,
    spot_bid: float | None = None, futures_ask: float | None = None,
    spot_book_age_s: float | None = None, futures_book_age_s: float | None = None,
    funding_rate: float = 0.0,
):
    await bot_engine.on_price_update(
        symbol, spot_price, futures_price, spread_pct,
        prices_from_book=prices_from_book,
        exit_spread_pct=exit_spread_pct,
        spot_bid=spot_bid, futures_ask=futures_ask,
        spot_book_age_s=spot_book_age_s, futures_book_age_s=futures_book_age_s,
        funding_rate=funding_rate,
    )


engine = ArbitrageEngine(storage, on_price_update=_on_price_update_for_bot)

# Motor multi-exchange: MEXC + Gate + BingX, spot e futures. Alimenta o
# dashboard de combinacoes. Usa o mesmo http client dos clientes de consulta,
# e so endpoints publicos - nenhuma credencial e necessaria para monitorar.
multi_storage = MultiStorage()
multi_engine = MultiVenueEngine(_bot_http_client)
multi_engine.storage = multi_storage


async def _load_contract_specs_for_configured_pairs():
    """
    Carrega os metadados de contrato Futures (contractSize, minVol) e de
    símbolo Spot (baseAssetPrecision) de cada par que o bot tem configurado.
    Necessário para calcular corretamente o volume de contratos e para
    arredondar a quantidade de venda no Spot (evita o erro "amount scale
    is invalid" da MEXC). Roda na inicialização e sempre que uma nova
    config de par é criada (ver endpoint bot_set_pair_config).
    """
    # Usa o cliente de CONSULTA, não o de execução: os dois endpoints são
    # públicos, e em modo SIMULAÇÃO o cliente de execução é None. Sem isso, a
    # simulação nunca carregava contractSize nem as taxas reais, e a
    # confirmação por profundidade (que precisa de contractSize para ler o
    # book de contratos) recusaria toda decisão silenciosamente.
    if _query_futures_client is None:
        return
    for symbol in list(bot_engine.configs.keys()):
        if symbol not in bot_engine.contract_specs:
            try:
                futures_symbol = f"{symbol}_USDT"
                resp = await _query_futures_client.get_contract_detail(futures_symbol)
                data = resp.get("data")
                if isinstance(data, list):
                    data = data[0] if data else None
                if data:
                    spec = ContractSpec.from_api_response(data)
                    bot_engine.contract_specs[symbol] = spec
                    logger.info(
                        "Metadados de contrato (futures) carregados para %s: contractSize=%s, "
                        "tick de preço=%s, taker=%.4f%%.",
                        symbol, spec.contract_size, spec.price_unit, spec.taker_fee_pct,
                    )
            except Exception as e:
                logger.warning("Não foi possível carregar metadados de contrato para %s: %s", symbol, e)

        if symbol not in bot_engine.spot_specs and _query_spot_client is not None:
            try:
                spot_symbol = f"{symbol}USDT"
                resp = await _query_spot_client.get_exchange_info(spot_symbol)
                symbols_data = resp.get("symbols", [])
                if symbols_data:
                    sspec = SpotSymbolSpec.from_exchange_info(symbols_data[0])
                    bot_engine.spot_specs[symbol] = sspec
                    logger.info(
                        "Metadados de símbolo (spot) carregados para %s: quantidade com %d casas, "
                        "preço com %d casas, taker=%.4f%%.",
                        symbol, sspec.base_asset_precision, sspec.price_precision, sspec.taker_fee_pct,
                    )
                else:
                    logger.warning("Nenhum símbolo retornado por exchangeInfo para %s.", spot_symbol)
            except Exception as e:
                logger.warning("Não foi possível carregar metadados de símbolo spot para %s: %s", symbol, e)


async def _sync_futures_priority_symbols():
    """
    Marca os pares configurados no bot como "prioritários" no WebSocket de
    futures, fazendo com que recebam subscrição individual (`sub.ticker`):
    atualização a cada ~1s e, principalmente, com bid1/ask1 reais - o canal
    agregado (`sub.tickers`) não traz bid/ask e só atualiza a cada 2s.

    Isso importa porque a perna Futures da estratégia é sempre VENDA, então
    o preço relevante é o BID (o que se recebe ao vender), não o último
    negociado.
    """
    futures_symbols = [f"{sym}_USDT" for sym in bot_engine.configs.keys()]
    spot_symbols = [f"{sym}USDT" for sym in bot_engine.configs.keys()]

    if futures_symbols:
        await engine.futures_ws.set_priority_symbols(futures_symbols)
    if spot_symbols:
        await engine.spot_ws.set_priority_symbols(spot_symbols)

    await _sync_focus_mode()


async def _sync_focus_mode():
    """
    Ativa o modo foco quando houver ao menos um par LIGADO no bot.

    Com foco ativo, todo o processamento (polling REST, WebSockets) se
    concentra apenas nos pares que o bot está operando - os demais pares
    do Dashboard param de ser atualizados. É uma troca deliberada:
    responsividade máxima onde há dinheiro em jogo, em detrimento do
    monitoramento amplo.

    Assim que o último par é desligado, o modo foco sai automaticamente e
    o Dashboard volta a monitorar todos os pares.
    """
    enabled_symbols = [sym for sym, cfg in bot_engine.configs.items() if cfg.enabled]
    await engine.set_focus_mode(bool(enabled_symbols), enabled_symbols)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.connect()
    await multi_storage.connect()
    await bot_storage.connect()
    await bot_engine.load_configs()
    await _load_contract_specs_for_configured_pairs()
    await engine.start()
    # Referência forte guardada em `app.state`: uma task sem referência forte
    # pode ser coletada pelo garbage collector no meio da execução, parando o
    # motor sem nenhum erro visível.
    app.state.multi_engine_task = asyncio.create_task(multi_engine.run())
    await _sync_futures_priority_symbols()
    logger.info(
        "Backend iniciado (dashboard + bot em modo %s). Aguardando dados da MEXC...",
        bot_engine.execution_mode.value.upper(),
    )
    yield
    await multi_engine.stop()
    await multi_storage.close()
    await engine.shutdown()
    await _bot_http_client.aclose()
    await storage.close()
    await bot_storage.close()


app = FastAPI(title="MEXC Spot x Futures Arbitrage Dashboard + Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "pairs_tracked": len(engine.pairs),
        "connection_status": engine.compute_connection_status(),
        "bot_execution_mode": bot_engine.execution_mode.value,
    }


@app.get("/api/pairs")
async def get_pairs():
    """Snapshot pontual via REST (útil para debug ou primeira carga sem WS)."""
    return engine.get_snapshot()


# =====================================================================
# Dashboard multi-exchange (MEXC + Gate + BingX, Spot×Futures e Futures×Futures)
# =====================================================================

@app.get("/api/venues")
async def get_venues():
    """
    Os venues disponíveis e o estado de cada um. Alimenta o filtro da
    interface — a lista nunca é escrita à mão no frontend, para que
    adicionar uma exchange no backend a faça aparecer sozinha.
    """
    return {
        "venues": multi_engine.venue_summary(),
        "enrichment": multi_engine.enrichment_summary(),
    }


@app.get("/api/venues/trading")
async def get_trading_venues():
    """
    Quais venues têm execução habilitada e quais combinações o bot pode
    operar.

    Uma combinação só é operável com cliente autenticado nos DOIS lados. A
    interface usa isso para marcar linhas não-operáveis ANTES de o usuário
    tentar configurar o bot nelas — o alternativo seria descobrir isso no meio
    de uma operação, com uma perna já aberta.
    """
    from exchanges.registry import venue_pairs
    from exchanges.base import Venue as _V

    validados = bot_engine.validated_venues

    operaveis, bloqueadas = [], []
    for compra, venda in venue_pairs():
        item = {
            "buy_venue": compra.key, "sell_venue": venda.key,
            "cross_exchange": compra.exchange != venda.exchange,
        }
        faltando = [v.key for v in (compra, venda) if VENUE_CLIENTS.get(v.key) is None]
        # Credencial e validação são bloqueios DIFERENTES e a distinção importa
        # para o operador: "falta chave" ele resolve na exchange em 2 minutos;
        # "falta validar" exige enviar uma ordem real mínima e conferir. Juntar
        # os dois num "indisponível" genérico mandaria ele procurar no lugar
        # errado.
        nao_validados = [v.key for v in (compra, venda) if v.key not in validados]
        if faltando:
            item["missing_credentials"] = faltando
        if nao_validados:
            item["unvalidated_venues"] = nao_validados

        if faltando or nao_validados:
            bloqueadas.append(item)
        else:
            operaveis.append(item)

    return {
        "tradable_venues": TRADABLE_VENUES,
        "validated_venues": sorted(validados),
        "tradable_combinations": operaveis,
        "blocked_combinations": bloqueadas,
        "live_mode": LIVE_MODE_ENABLED,
    }


@app.get("/api/combinations")
async def get_combinations(
    venues: str | None = None,
    kinds: str | None = None,
    min_net_spread_pct: float | None = None,
    min_vol_usdt: float = 0.0,
    min_max_entry_pct: float | None = None,
    symbol: str | None = None,
    limit: int = 400,
    include_suspect: bool = False,
):
    """
    Todas as combinações de arbitragem entre os venues, já filtradas e
    ordenadas pelo spread LÍQUIDO de taxas.

    Filtros (todos opcionais, todos aplicados no SERVIDOR):
    - `venues`: lista separada por vírgula, ex "mexc:spot,gate:futures".
      Vazio = todos.
    - `kinds`: "spot_futures" e/ou "futures_futures".
    - `min_net_spread_pct`: piso de spread líquido.
    - `min_vol_usdt`: volume 24h mínimo da perna MENOS líquida.
    - `include_suspect`: inclui as linhas barradas pelo consenso de preço
      (ver `suspect_reason`). Fora por padrão porque elas têm os maiores
      spreads do sistema e ocupariam o topo da ordenação — foram medidas
      linhas de 128% que eram colisão de símbolo, não oportunidade.

    A filtragem é no servidor de propósito: são ~5900 combinações
    monitoráveis, e mandar todas a cada ciclo para o navegador filtrar
    desperdiçaria banda e travaria a interface.
    """
    return multi_engine.get_snapshot(
        enabled_venues=parse_venue_filter(venues),
        kinds=[k.strip() for k in kinds.split(",")] if kinds else None,
        min_net_spread_pct=min_net_spread_pct,
        min_vol_usdt=min_vol_usdt,
        min_max_entry_pct=min_max_entry_pct,
        symbol=symbol.upper() if symbol else None,
        limit=min(limit, 2000),
        include_suspect=include_suspect,
    )


@app.get("/api/combinations/{symbol}/quotes")
async def get_symbol_quotes(symbol: str):
    """
    Cotação de um símbolo em CADA venue, com o veredito do consenso de preço.

    É a tela de diagnóstico para quando uma combinação aparece barrada: mostra
    exatamente qual venue destoa e quanto, em vez de só informar que a linha
    foi recusada.
    """
    symbol = symbol.upper()
    por_venue = multi_engine.quotes.get(symbol, {})
    consenso = evaluate_consensus(por_venue)
    return {
        "symbol": symbol,
        "reference_price": consenso.reference_price,
        "venues_considered": consenso.venues_considered,
        "has_consensus": consenso.has_consensus,
        "quotes": [
            {
                "venue": chave,
                "bid": q.bid, "ask": q.ask, "last": q.last,
                "vol_usdt": q.vol_usdt, "funding_rate": q.funding_rate,
                "age_s": q.age_s(), "has_book": q.has_book,
                "deviation_pct": consenso.deviations_pct.get(chave),
                "is_outlier": chave in consenso.outliers,
            }
            for chave, q in sorted(por_venue.items())
        ],
    }


@app.delete("/api/combinations/extremes")
async def clear_combination_extremes(symbol: str | None = None):
    """
    Reseta os mín/máx históricos das combinações. Sem `symbol`, reseta tudo.

    Útil pelo mesmo motivo do endpoint equivalente do dashboard: um recorde
    registrado antes de uma correção (ou durante uma colisão de símbolo que o
    consenso ainda não pegava) fica pendurado para sempre e distorce a
    leitura de todas as outras linhas.
    """
    alvo = symbol.upper() if symbol else None
    removidos = await multi_storage.clear(alvo)
    if alvo:
        multi_engine.extremes = {
            k: v for k, v in multi_engine.extremes.items() if v.get("symbol") != alvo
        }
    else:
        multi_engine.extremes.clear()
    return {"status": "ok", "removed": removidos}


@app.websocket("/ws/combinations")
async def combinations_websocket(websocket: WebSocket):
    """
    Push do snapshot multi-exchange. Os filtros chegam como query params na
    conexão e valem para toda a sessão do socket.
    """
    await websocket.accept()
    params = websocket.query_params
    filtros = dict(
        enabled_venues=parse_venue_filter(params.get("venues")),
        kinds=[k.strip() for k in params["kinds"].split(",")] if params.get("kinds") else None,
        min_net_spread_pct=float(params["min_net_spread_pct"]) if params.get("min_net_spread_pct") else None,
        min_vol_usdt=float(params.get("min_vol_usdt", 0) or 0),
        min_max_entry_pct=float(params["min_max_entry_pct"]) if params.get("min_max_entry_pct") else None,
        limit=min(int(params.get("limit", 400) or 400), 2000),
        include_suspect=params.get("include_suspect", "").lower() == "true",
    )
    queue = multi_engine.subscribe()
    try:
        await websocket.send_json(multi_engine.get_snapshot(**filtros))

        async def sender():
            while True:
                await queue.get()
                await websocket.send_json(multi_engine.get_snapshot(**filtros))

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
    finally:
        multi_engine.unsubscribe(queue)


@app.get("/api/spread-history/{symbol}")
async def get_spread_history(symbol: str):
    history = await engine.get_spread_history(symbol.upper())
    return {"symbol": symbol.upper(), "history": history}


@app.delete("/api/spread-extremes")
async def clear_spread_extremes(symbol: str | None = None):
    """
    Reseta os extremos (mín/máx histórico) de spread. Sem `symbol`, reseta
    todos os pares.

    Útil para descartar recordes registrados a partir de preços
    não-executáveis (último negociado em vez do book) - a partir da
    correção que passou a exigir preços do book, os novos extremos
    refletem apenas oportunidades que eram de fato executáveis.
    """
    removed = await engine.clear_spread_extremes(symbol.upper() if symbol else None)
    return {"status": "ok", "removed": removed}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue = engine.subscribe()
    try:
        # Envia snapshot inicial imediatamente
        await websocket.send_json(engine.get_snapshot())

        async def sender():
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                await websocket.send_json({"type": "heartbeat", "connection_status": engine.compute_connection_status()})

        sender_task = asyncio.create_task(sender())
        heartbeat_task = asyncio.create_task(heartbeat())

        try:
            while True:
                # Mantém a conexão viva e escuta desconexão do cliente
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
            heartbeat_task.cancel()
    finally:
        engine.unsubscribe(queue)


# =====================================================================
# Endpoints do Bot de Arbitragem (modo SIMULATION ou LIVE, ver bot_engine.py)
# =====================================================================

class PairConfigRequest(BaseModel):
    enabled: bool
    entry_spread_pct: float
    exit_spread_pct: float
    position_size_usdt: float
    # Onde operar. Os padroes preservam o comportamento anterior (MEXC), para
    # que um cliente antigo que nao mande esses campos continue funcionando
    # exatamente como funcionava.
    buy_venue: str = "mexc:spot"
    sell_venue: str = "mexc:futures"


# Cache simples em memória para o saldo - evita bater na MEXC a cada poll
# do frontend (o balanço não muda a cada segundo, um cache de alguns
# segundos é imperceptível para o usuário e reduz risco de rate limit).
_balance_cache = {"data": None, "ts": 0.0}
_BALANCE_CACHE_TTL = 8.0


@app.get("/api/bot/balance")
async def bot_balance():
    """
    Saldo real das contas Spot e Futures na MEXC. Disponível sempre que
    houver credenciais no .env, independente do modo de execução do bot
    (funciona em SIMULATION e em LIVE) - usa apenas endpoints de LEITURA,
    nunca envia ordens.
    """
    if not _has_credentials:
        return {
            "available": False,
            "reason": "Credenciais da MEXC não configuradas no .env (MEXC_SPOT_API_KEY, etc).",
        }

    now = asyncio.get_event_loop().time()
    if _balance_cache["data"] is not None and (now - _balance_cache["ts"]) < _BALANCE_CACHE_TTL:
        return _balance_cache["data"]

    result = {"available": True, "spot": None, "futures": None, "errors": []}

    try:
        spot_balance = await _query_spot_client.get_balance("USDT")
        result["spot"] = {"asset": "USDT", "free": spot_balance["free"], "locked": spot_balance["locked"]}
    except Exception as e:
        result["errors"].append(f"Spot: {e}")

    try:
        assets = await _query_futures_client.get_assets()
        if assets.get("success"):
            usdt_assets = [a for a in assets.get("data", []) if a.get("currency") == "USDT"]
            if usdt_assets:
                a = usdt_assets[0]
                result["futures"] = {
                    "asset": "USDT",
                    "available_balance": float(a.get("availableBalance", 0) or 0),
                    "position_margin": float(a.get("positionMargin", 0) or 0),
                    "equity": float(a.get("equity", 0) or 0),
                }
            else:
                result["futures"] = {"asset": "USDT", "available_balance": 0.0, "position_margin": 0.0, "equity": 0.0}
        else:
            result["errors"].append(f"Futures: resposta inesperada ({assets})")
    except Exception as e:
        result["errors"].append(f"Futures: {e}")

    _balance_cache["data"] = result
    _balance_cache["ts"] = now
    return result


@app.get("/api/bot/health")
async def bot_health():
    return {
        "execution_mode": bot_engine.execution_mode.value,
        "connection_degraded": bot_engine.connection_degraded,
        "pairs_configured": len(bot_engine.configs),
        "max_total_exposure_usdt": bot_engine.max_total_exposure_usdt,
        "current_total_exposure_usdt": bot_engine.get_current_total_exposure_usdt(),
        "focus_mode": engine._focus_mode,
        "focus_symbols": sorted(engine._focus_symbols),
        "execution": {
            "max_slippage_pct": bot_engine.slippage_policy.max_slippage_pct,
            "max_attempts": bot_engine.slippage_policy.max_attempts,
            "escalate_to_market": bot_engine.slippage_policy.escalate_to_market,
            "max_book_age_s": bot_engine.max_book_age_s,
            "depth_limit": bot_engine.depth_limit,
            "min_net_pct": bot_engine.min_net_pct,
        },
        # Quantas vezes o topo do book prometeu um spread que a profundidade
        # real não confirmou. É a métrica direta do problema que motivou toda
        # a camada de confirmação: se este número for alto num par, o topo do
        # book daquele par é sistematicamente enganoso para o seu tamanho de
        # posição.
        "depth_rejections": bot_engine.depth_rejections,
        "fees_by_pair": {
            sym: {
                "spot_taker_pct": bot_engine.fees_for(sym).spot_taker_pct,
                "futures_taker_pct": bot_engine.fees_for(sym).futures_taker_pct,
                "round_trip_pct": bot_engine.fees_for(sym).round_trip_pct,
            }
            for sym in bot_engine.configs
        },
    }


@app.get("/api/bot/pairs")
async def bot_get_pairs():
    return {"pairs": bot_engine.get_snapshot()}


@app.post("/api/bot/pairs/{symbol}")
async def bot_set_pair_config(symbol: str, body: PairConfigRequest):
    symbol = symbol.upper()
    try:
        await bot_engine.set_pair_config(
            symbol, body.enabled, body.entry_spread_pct, body.exit_spread_pct,
            body.position_size_usdt, body.buy_venue, body.sell_venue,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _load_contract_specs_for_configured_pairs()
    await _sync_futures_priority_symbols()
    return {"status": "ok", "symbol": symbol}


@app.delete("/api/bot/pairs/{symbol}")
async def bot_remove_pair_config(symbol: str):
    symbol = symbol.upper()
    await bot_engine.remove_pair_config(symbol)
    await _sync_focus_mode()
    return {"status": "ok", "symbol": symbol}


@app.post("/api/bot/pairs/{symbol}/resume")
async def bot_resume_pair(symbol: str):
    symbol = symbol.upper()
    await bot_engine.resume_from_halt(symbol)
    return {"status": "ok", "symbol": symbol}


@app.post("/api/bot/kill-switch")
async def bot_kill_switch():
    """
    Fecha imediatamente todas as posições abertas e pausa todos os pares
    do bot. Em modo LIVE, fecha as pernas reais a mercado na MEXC. Ação
    irreversível de emergência.
    """
    await bot_engine.kill_switch()
    # Kill switch desliga todos os pares - sai do modo foco automaticamente
    await _sync_focus_mode()
    return {"status": "ok", "message": "Kill switch acionado. Todos os pares pausados."}


@app.get("/api/bot/events")
async def bot_get_events(symbol: str | None = None, limit: int = 200):
    events = await bot_storage.get_recent_events(symbol.upper() if symbol else None, limit)
    return {"events": events}


@app.delete("/api/bot/events")
async def bot_clear_events(symbol: str | None = None):
    """Apaga o histórico de eventos/operações. Sem `symbol`, apaga tudo. Ação irreversível."""
    removed = await bot_storage.clear_events(symbol.upper() if symbol else None)
    return {"status": "ok", "removed": removed}


@app.get("/api/bot/logs")
async def bot_get_logs(level: str | None = None, limit: int = 500):
    """
    Log técnico em tempo real do que o bot está fazendo internamente:
    conexões, decisões de entrada/saída, erros, reconexões, etc. Diferente
    de /api/bot/events (que é o histórico de operações/trades); isso aqui é
    o log bruto de execução, útil para depurar comportamento do bot.
    """
    return {"logs": _log_buffer.get_logs(level=level, limit=limit)}


@app.delete("/api/bot/logs")
async def bot_clear_logs():
    removed = _log_buffer.clear()
    return {"status": "ok", "removed": removed}


@app.websocket("/ws/bot")
async def bot_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue = bot_engine.subscribe()
    try:
        await websocket.send_json({"type": "bot_snapshot", "pairs": bot_engine.get_snapshot()})

        async def sender():
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
    finally:
        bot_engine.unsubscribe(queue)


# ---------------------------------------------------------------------------
# Frontend servido pelo proprio backend (deploy em VPS).
#
# Montado por ULTIMO de proposito: um mount em "/" captura tudo que nao casou
# com nenhuma rota anterior. Registrado antes, engoliria /api e /ws.
#
# Serve para o deploy ficar em UMA porta so: com o frontend e a API na mesma
# origem, o tunel SSH precisa encaminhar apenas 8000, e as URLs padrao do
# frontend (localhost:8000) continuam valendo sem rebuild.
#
# Se `frontend/dist` nao existir (ambiente de desenvolvimento, onde o Vite
# serve na 5173), o mount e simplesmente pulado.
# ---------------------------------------------------------------------------
_FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "dist")
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
    logger.info("Frontend estatico servido de %s", _FRONTEND_DIST)
else:
    logger.info("frontend/dist nao encontrado - servindo apenas a API (use o Vite em dev).")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

