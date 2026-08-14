"""
Motor central do dashboard de arbitragem.

Responsabilidades:
- Descobrir e manter o universo de pares (spot ∩ futures).
- Fazer polling REST do spot (rápido, poucos segundos) - fonte primária de preço spot.
- Consumir o WebSocket de futures (push.tickers) - fonte primária de preço futures.
- Fazer polling REST de funding rate (não vem no ws.tickers).
- Calcular spread % por par e registrar histórico/cruzamentos via Storage.
- Manter um snapshot em memória pronto para servir via WS próprio ao frontend.
- Fallback automático: se o WS de futures cair, cai para polling REST de futures também.
"""
import asyncio
import logging
import time
from collections import deque
from typing import Optional

import httpx

from config import (
    FUTURES_REST_POLL_INTERVAL,
    REST_FALLBACK_POLL_INTERVAL,
    PAIR_DISCOVERY_INTERVAL,
    CROSSING_WINDOWS_REFRESH_INTERVAL,
    MEXC_SPOT_TRADE_URL,
    MEXC_FUTURES_TRADE_URL,
    MEXC_FUTURES_REST_BASE,
)
import mexc_rest
from mexc_ws_futures import FuturesWebSocketClient
from mexc_ws_spot import SpotBookTickerWebSocketClient
from storage import Storage

logger = logging.getLogger("engine")

SPOT_POLL_INTERVAL = 3  # segundos - spot via REST (rápido o suficiente para "tempo real" percebido)

# Idade a partir da qual o book de futures vindo do WebSocket individual
# deixa de ter precedência sobre o polling REST. Ligeiramente maior que o
# intervalo do polling (FUTURES_REST_POLL_INTERVAL = 5s) para o REST não
# ficar brigando com um WS saudável, mas curta o suficiente para que um WS
# silencioso seja substituído antes de o dado virar ficção.
FUTURES_WS_BOOK_MAX_AGE = 6.0

# --- Profundidade do topo do book de futures ---
# O ticker de futures da MEXC não traz quantidade (o de spot traz), então esta
# é a única forma de saber quanto cabe no preço exibido. Calibrado baixo de
# propósito: preço é o dado crítico, profundidade é enriquecimento — quando os
# dois disputam banda, quem cede é o enriquecimento.
FUTURES_DEPTH_INTERVAL_S = 4.0
FUTURES_DEPTH_BATCH = 12
FUTURES_DEPTH_CONCURRENCY = 4
FUTURES_DEPTH_TTL_S = 90.0

# --- Janelas móveis de spread (5/15/30/60 min) ---
#
# Agregadas em baldes de 1 MINUTO, não amostra a amostra.
#
# Guardar cada amostra seria ~1800 por hora por par; com 578 pares isso passa
# de um milhão de registros vivos em memória, para responder uma pergunta que
# não precisa dessa resolução. Um balde por minuto guarda o máximo de entrada
# e o mínimo de saída daquele minuto, e as janelas viram um max/min sobre os
# últimos N baldes: 60 tuplas por par, ~35 mil no total.
#
# Deliberadamente em MEMÓRIA, sem persistir: uma janela de 5 a 60 minutos não
# sobrevive a um reinício de forma útil de qualquer jeito -- depois de
# reiniciar, o histórico correto é justamente "ainda não sei".
# Intervalo MÍNIMO entre dois snapshots enviados ao navegador.
#
# `_broadcast_snapshot` era chamado por SÍMBOLO: cada tick do WebSocket de
# spot (até 180 símbolos subscritos) disparava um snapshot COMPLETO dos ~580
# pares. Medido em 09/08/2026: 3,5 snapshots/s de 843 KB cada = 2,91 MB/s
# contínuos, e o navegador re-renderizando 580 linhas x 15 colunas na mesma
# cadência. Em uma hora: ~10 GB de JSON e 12.600 renderizações completas.
#
# Era isso que matava o processo de renderização do Chrome ("Ah, não!").
#
# Um snapshot é o ESTADO INTEIRO, não um evento: enviar vários por segundo é
# desperdício puro, porque cada um torna o anterior irrelevante. Coalescer
# para 1/s não perde informação nenhuma -- e nenhum operador percebe a
# diferença entre 1 e 3,5 atualizações por segundo.
BROADCAST_MIN_INTERVAL_S = 1.0

SPREAD_WINDOWS_MIN = (5, 15, 30, 60)
SPREAD_BUCKETS_MAX = max(SPREAD_WINDOWS_MIN)


class PairState:
    """Estado em memória de um par (display_symbol, ex: 'EWT')."""

    __slots__ = (
        "display_symbol", "spot_symbol", "futures_symbol",
        "spot_price", "futures_price", "spot_vol", "futures_vol",
        "spot_bid", "spot_ask", "futures_bid", "futures_ask",
        "funding_rate", "spread_pct", "exit_spread_pct", "crossings_count",
        "crossings_1h", "crossings_12h", "crossings_24h",
        "last_crossing_ts", "last_update_ts", "spot_book_ts", "futures_book_ts",
        "spot_bid_qty", "spot_ask_qty", "futures_bid_qty", "futures_ask_qty",
        "futures_top_ts",
        "spot_high_24h", "spot_low_24h", "futures_high_24h", "futures_low_24h",
        "_spread_buckets",
        "min_spread_pct", "min_spread_ts", "max_spread_pct", "max_spread_ts",
        "min_exit_spread_pct", "min_exit_spread_ts",
        "max_exit_spread_pct", "max_exit_spread_ts",
    )

    def __init__(self, display_symbol: str, spot_symbol: str, futures_symbol: str):
        self.display_symbol = display_symbol
        self.spot_symbol = spot_symbol
        self.futures_symbol = futures_symbol
        self.spot_price: Optional[float] = None
        self.futures_price: Optional[float] = None
        # Book completo dos dois mercados - necessário porque entrada e saída
        # usam lados OPOSTOS do book (ver recompute_spread).
        self.spot_bid: Optional[float] = None
        self.spot_ask: Optional[float] = None
        self.futures_bid: Optional[float] = None
        self.futures_ask: Optional[float] = None
        self.spot_vol: float = 0.0
        self.futures_vol: float = 0.0
        self.funding_rate: float = 0.0
        self.spread_pct: Optional[float] = None
        self.exit_spread_pct: Optional[float] = None
        self.crossings_count: int = 0
        self.crossings_1h: int = 0
        self.crossings_12h: int = 0
        self.crossings_24h: int = 0
        self.last_crossing_ts: Optional[float] = None
        self.last_update_ts: float = 0.0
        # Carimbo de tempo POR LADO do book. `last_update_ts` sozinho não
        # serve para decidir execução: ele é atualizado por qualquer fonte,
        # então um par cujo futures está congelado há dois minutos continua
        # parecendo "atualizado agora" só porque o spot ticou. A assimetria
        # entre os dois lados é justamente o que produz spread fantasma, e é
        # o que estes dois campos tornam visível.
        self.spot_book_ts: float = 0.0
        self.futures_book_ts: float = 0.0
        # Quantidade no TOPO do book. O spot vem de graça no ticker 24h; o
        # futures exige consulta de profundidade (ver
        # `futures_depth_enrichment_loop`), então pode ficar None por um tempo.
        # None significa "ainda não medido", NUNCA zero: exibir 0 onde não se
        # sabe faria um book profundo parecer vazio.
        self.spot_bid_qty: Optional[float] = None
        self.spot_ask_qty: Optional[float] = None
        self.futures_bid_qty: Optional[float] = None
        self.futures_ask_qty: Optional[float] = None
        self.futures_top_ts: float = 0.0
        # Máxima/mínima de 24h, base da volatilidade. Vêm dos dois tickers.
        self.spot_high_24h: Optional[float] = None
        self.spot_low_24h: Optional[float] = None
        self.futures_high_24h: Optional[float] = None
        self.futures_low_24h: Optional[float] = None
        # Baldes de 1 minuto: [minuto_epoch, max_entrada, min_saida].
        # Lista (não tupla) porque o balde do minuto corrente é atualizado no
        # lugar a cada amostra.
        self._spread_buckets: deque = deque(maxlen=SPREAD_BUCKETS_MAX)
        self.min_spread_pct: Optional[float] = None
        self.min_spread_ts: Optional[float] = None
        self.max_spread_pct: Optional[float] = None
        self.max_spread_ts: Optional[float] = None
        # Extremos do spread de SAÍDA, rastreados separadamente porque é
        # uma grandeza diferente (lados opostos do book).
        self.min_exit_spread_pct: Optional[float] = None
        self.min_exit_spread_ts: Optional[float] = None
        self.max_exit_spread_pct: Optional[float] = None
        self.max_exit_spread_ts: Optional[float] = None

    def recompute_spread(self):
        """
        Calcula DOIS spreads distintos, porque entrada e saída executam em
        lados OPOSTOS do book:

        ENTRADA (spread_pct): compra Spot + vende Futures
            = (futures_BID - spot_ASK) / spot_ASK
            Você paga o ask do spot e recebe o bid do futures.

        SAÍDA (exit_spread_pct): vende Spot + recompra Futures
            = (futures_ASK - spot_BID) / spot_BID
            Você recebe o bid do spot e paga o ask do futures.

        Usar a fórmula de entrada para decidir a saída (bug anterior) fazia
        o bot "ver" um spread muito menor do que o real ao sair - em pares
        com book largo a diferença passa de 2 pontos percentuais, o que
        gerava saídas em condições piores do que o bot achava, com prejuízo
        pequeno e sistemático.

        Se o book não estiver disponível, cai para os preços de referência
        (comportamento antigo) - melhor um número aproximado do que nenhum.
        """
        spot_ask = self.spot_ask or self.spot_price
        futures_bid = self.futures_bid or self.futures_price
        if spot_ask and futures_bid and spot_ask > 0:
            self.spread_pct = (futures_bid - spot_ask) / spot_ask * 100
        else:
            self.spread_pct = None

        # O spread de SAÍDA exige o book de verdade (spot_bid e futures_ask).
        # Sem eles, fica None em vez de cair para os preços de referência -
        # esse fallback antes produzia um número que PARECIA válido mas era
        # calculado com os preços do lado errado do book, levando o bot a
        # sair em condições muito piores do que enxergava.
        if self.spot_bid and self.futures_ask and self.spot_bid > 0:
            self.exit_spread_pct = (self.futures_ask - self.spot_bid) / self.spot_bid * 100
        else:
            self.exit_spread_pct = None

    def record_spread_sample(self, now: Optional[float] = None):
        """
        Acumula a amostra atual no balde do minuto corrente.

        Só deve ser chamado com preços vindos do BOOK -- o mesmo critério dos
        extremos históricos. Registrar amostras de "último negociado" produz
        máximos e mínimos que nunca foram executáveis, exatamente o problema
        que o reset de extremos do dashboard existe para limpar.
        """
        if self.spread_pct is None:
            return
        agora = now if now is not None else time.time()
        minuto = int(agora // 60)

        if self._spread_buckets and self._spread_buckets[-1][0] == minuto:
            balde = self._spread_buckets[-1]
            balde[1] = max(balde[1], self.spread_pct)
            if self.exit_spread_pct is not None:
                balde[2] = self.exit_spread_pct if balde[2] is None else min(balde[2], self.exit_spread_pct)
        else:
            self._spread_buckets.append([minuto, self.spread_pct, self.exit_spread_pct])

    def window_stats(self, now: Optional[float] = None) -> dict:
        """
        Máximo de ENTRADA e mínimo de SAÍDA em cada janela.

        A assimetria é proposital e vem da estratégia: entra-se no spread mais
        ALTO possível e sai-se no mais BAIXO possível, então o que interessa
        saber de cada janela é o melhor momento que ela teve para cada ponta.
        O lucro de uma operação é `entrada - saída`, então esses dois números
        juntos delimitam a melhor operação que teria sido possível ali.
        """
        agora = now if now is not None else time.time()
        minuto_atual = int(agora // 60)
        out: dict = {}
        for janela in SPREAD_WINDOWS_MIN:
            corte = minuto_atual - janela + 1
            entradas = [b[1] for b in self._spread_buckets if b[0] >= corte and b[1] is not None]
            saidas = [b[2] for b in self._spread_buckets if b[0] >= corte and b[2] is not None]
            out[f"max_entry_{janela}m"] = max(entradas) if entradas else None
            out[f"min_exit_{janela}m"] = min(saidas) if saidas else None
        return out

    @staticmethod
    def _range_pct(high: Optional[float], low: Optional[float]) -> Optional[float]:
        """
        Amplitude de 24h como percentual da mínima.

        É a definição mais direta de volatilidade para esta estratégia:
        responde "quanto este ativo andou hoje", que é o risco a que a perna
        descoberta fica exposta se uma das pontas falhar. Usa a MÍNIMA no
        denominador (não o preço atual) para o número não mudar conforme o
        preço se move dentro da mesma faixa já observada.
        """
        if not high or not low or low <= 0 or high < low:
            return None
        return (high - low) / low * 100

    @property
    def spot_volatility_pct(self) -> Optional[float]:
        return self._range_pct(self.spot_high_24h, self.spot_low_24h)

    @property
    def futures_volatility_pct(self) -> Optional[float]:
        return self._range_pct(self.futures_high_24h, self.futures_low_24h)

    @staticmethod
    def _top_usdt(qty: Optional[float], price: Optional[float], contract_size: float = 1.0):
        if qty is None or not price:
            return None
        return qty * contract_size * price

    def to_dict(self, contract_size: float = 1.0) -> dict:
        return {
            "symbol": self.display_symbol,
            # Profundidade EXECUTÁVEL no topo, em USDT. A entrada compra no ask
            # do spot e vende no bid do futures, então são esses dois lados que
            # limitam a operação.
            "spot_ask_usdt": self._top_usdt(self.spot_ask_qty, self.spot_ask),
            "spot_bid_usdt": self._top_usdt(self.spot_bid_qty, self.spot_bid),
            "futures_bid_usdt": self._top_usdt(self.futures_bid_qty, self.futures_bid, contract_size),
            "futures_ask_usdt": self._top_usdt(self.futures_ask_qty, self.futures_ask, contract_size),
            **self.window_stats(),
            "volatility_pct": self.spot_volatility_pct,
            "futures_volatility_pct": self.futures_volatility_pct,
            "spot_high_24h": self.spot_high_24h, "spot_low_24h": self.spot_low_24h,
            "spot_price": self.spot_price,
            "futures_price": self.futures_price,
            "spot_vol": self.spot_vol,
            "futures_vol": self.futures_vol,
            "spread_pct": self.spread_pct,
            "exit_spread_pct": self.exit_spread_pct,
            "min_exit_spread_pct": self.min_exit_spread_pct,
            "min_exit_spread_ts": self.min_exit_spread_ts,
            "max_exit_spread_pct": self.max_exit_spread_pct,
            "max_exit_spread_ts": self.max_exit_spread_ts,
            "spot_bid": self.spot_bid,
            "spot_ask": self.spot_ask,
            "futures_bid": self.futures_bid,
            "futures_ask": self.futures_ask,
            "funding_rate": self.funding_rate,
            "spot_book_ts": self.spot_book_ts,
            "futures_book_ts": self.futures_book_ts,
            "crossings_count": self.crossings_count,
            "crossings_1h": self.crossings_1h,
            "crossings_12h": self.crossings_12h,
            "crossings_24h": self.crossings_24h,
            "last_crossing_ts": self.last_crossing_ts,
            "min_spread_pct": self.min_spread_pct,
            "min_spread_ts": self.min_spread_ts,
            "max_spread_pct": self.max_spread_pct,
            "max_spread_ts": self.max_spread_ts,
            "futures_link": MEXC_FUTURES_TRADE_URL.format(symbol=self.display_symbol),
            "spot_link": MEXC_SPOT_TRADE_URL.format(symbol=self.display_symbol),
            "last_update_ts": self.last_update_ts,
        }


class ArbitrageEngine:
    def __init__(self, storage: Storage, on_price_update=None):
        self.storage = storage
        self.pairs: dict[str, PairState] = {}
        self.http_client = httpx.AsyncClient()
        self.futures_ws = FuturesWebSocketClient(on_tickers_update=self._on_futures_ws_update)
        self.spot_ws = SpotBookTickerWebSocketClient(
            on_book_ticker_update=self._on_spot_ws_update,
            on_validation_status_change=self._on_spot_ws_validation_change,
        )
        self.connection_status = "connecting"  # online | offline | reconectando
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self._futures_ws_last_msg_ts = 0.0
        # Modo foco: quando o bot está ativo, o sistema processa apenas os
        # pares que ele opera, ignorando os demais. Reduz drasticamente a
        # carga de processamento e a latência dos pares que importam.
        self._focus_mode: bool = False
        self._focus_symbols: set[str] = set()
        # Fonte ATUAL do preço de cada símbolo de futures: "book" (bid real,
        # canal individual) ou "last" (último negociado, canal agregado).
        self._futures_price_source: dict[str, str] = {}
        # Se o preço Spot vindo do REST tinha o book preenchido (askPrice > 0)
        # ou caiu no fallback lastPrice. Complementa is_trusted() do WS.
        self._spot_rest_has_book: dict[str, bool] = {}
        # contractSize por símbolo de futures. Sem ele, a quantidade do book
        # de futures (que vem em CONTRATOS) não pode virar valor em USDT: o
        # contrato de JIMOTHY vale 100 moedas e o de BTC vale 0,0001, então
        # errar aqui produz números fora por ordens de grandeza.
        self._contract_sizes: dict[str, float] = {}
        # Marca que há snapshot novo a enviar. Ver `_broadcast_snapshot`.
        self._snapshot_dirty: bool = False
        # Callback opcional: async fn(symbol, spot_price, futures_price, spread_pct).
        # Usado pelo bot de arbitragem (bot/bot_engine.py) para reagir aos mesmos
        # preços que já alimentam o dashboard, sem o dashboard depender do bot.
        self.on_price_update = on_price_update

    # ---------------- Pub/Sub para o WS do frontend ----------------

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _broadcast(self, event: dict):
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # cliente lento, descarta - próximo snapshot corrige

    # ---------------- Modo foco (bot ativo) ----------------

    async def set_focus_mode(self, enabled: bool, symbols: list[str] | None = None):
        """
        Ativa/desativa o modo foco. Quando ativo, o engine processa apenas
        os pares informados (tipicamente os configurados no bot), ignorando
        todos os outros.

        Efeitos:
        - Loops de polling REST percorrem só os pares em foco (em vez de
          centenas), reduzindo o tempo de ciclo praticamente a zero.
        - WS de spot subscreve apenas esses pares.
        - WS de futures pula o canal agregado, usando só os canais
          individuais (1s, com bid/ask).

        O Dashboard passa a mostrar dados atualizados somente dos pares em
        foco - os demais congelam no último valor conhecido. É uma troca
        deliberada: máxima responsividade onde há dinheiro em jogo.
        """
        self._focus_mode = enabled
        self._focus_symbols = set(symbols or [])

        await self.spot_ws.set_focus_mode(enabled)
        await self.futures_ws.set_focus_mode(enabled)

        if enabled:
            logger.warning(
                "MODO FOCO ATIVADO: processando apenas %d pares (%s). "
                "Os demais pares do Dashboard não serão atualizados.",
                len(self._focus_symbols), ", ".join(sorted(self._focus_symbols)) or "nenhum",
            )
        else:
            logger.info("Modo foco desativado: voltando a processar todos os pares.")

        await self.broadcast_now()

    def _iter_active_pairs(self):
        """
        Itera os pares que devem ser processados agora. Em modo foco,
        retorna apenas os pares do bot; caso contrário, todos.
        """
        if not self._focus_mode:
            return list(self.pairs.values())
        return [s for s in self.pairs.values() if s.display_symbol in self._focus_symbols]

    # ---------------- Descoberta de pares ----------------

    async def discover_pairs(self):
        try:
            spot_tickers = await mexc_rest.fetch_spot_tickers(self.http_client)
            futures_tickers = await mexc_rest.fetch_futures_tickers(self.http_client)
        except Exception as e:
            logger.error("Erro ao descobrir pares: %s", e)
            return

        universe = mexc_rest.build_pair_universe(spot_tickers, futures_tickers)
        logger.info("Universo de pares descoberto: %d pares", len(universe))

        crossings = await self.storage.get_all_crossings()
        extremes = await self.storage.get_all_spread_extremes()
        exit_extremes = await self.storage.get_all_spread_extremes(table="exit_spread_extremes")

        async with self._lock:
            for display, syms in universe.items():
                if display not in self.pairs:
                    state = PairState(display, syms["spot_symbol"], syms["futures_symbol"])
                    saved = crossings.get(syms["futures_symbol"]) or crossings.get(display)
                    if saved:
                        state.crossings_count = saved["count"]
                        state.last_crossing_ts = saved["last_crossing_ts"]
                    saved_extremes = extremes.get(syms["futures_symbol"])
                    if saved_extremes:
                        state.min_spread_pct = saved_extremes["min_spread_pct"]
                        state.min_spread_ts = saved_extremes["min_spread_ts"]
                        state.max_spread_pct = saved_extremes["max_spread_pct"]
                        state.max_spread_ts = saved_extremes["max_spread_ts"]
                    saved_exit = exit_extremes.get(syms["futures_symbol"])
                    if saved_exit:
                        state.min_exit_spread_pct = saved_exit["min_spread_pct"]
                        state.min_exit_spread_ts = saved_exit["min_spread_ts"]
                        state.max_exit_spread_pct = saved_exit["max_spread_pct"]
                        state.max_exit_spread_ts = saved_exit["max_spread_ts"]
                    self.pairs[display] = state

            # Aplica ticker inicial já obtido
            for display, state in self.pairs.items():
                spot = spot_tickers.get(state.spot_symbol)
                fut = futures_tickers.get(state.futures_symbol)
                if spot:
                    state.spot_price = spot["price"]
                    state.spot_bid = spot.get("bid") or None
                    state.spot_ask = spot.get("ask") or None
                    state.spot_vol = spot["vol"]
                    state.spot_bid_qty = spot.get("bid_qty") or None
                    state.spot_ask_qty = spot.get("ask_qty") or None
                    state.spot_high_24h = spot.get("high_24h") or None
                    state.spot_low_24h = spot.get("low_24h") or None
                    if state.spot_bid:
                        state.spot_book_ts = time.time()
                if fut:
                    state.futures_price = fut["price"]
                    state.futures_bid = fut.get("bid") or None
                    state.futures_ask = fut.get("ask") or None
                    state.futures_vol = fut["vol"]
                    state.funding_rate = fut["funding_rate"]
                    state.futures_high_24h = fut.get("high_24h") or None
                    state.futures_low_24h = fut.get("low_24h") or None
                    if state.futures_bid:
                        state.futures_book_ts = time.time()
                state.recompute_spread()
                state.last_update_ts = time.time()

        # Registra os símbolos spot descobertos para o WS de bookTicker
        # subscrever (a subscrição de fato acontece na próxima
        # (re)conexão do WS, gerenciada pelo próprio cliente).
        spot_symbols = [state.spot_symbol for state in self.pairs.values()]
        await self.spot_ws.subscribe(spot_symbols)

    # ---------------- Loop: polling REST de spot ----------------

    async def spot_poll_loop(self):
        """
        Loop de polling REST do preço Spot. Continua rodando sempre,
        mesmo depois do WS de Spot ficar confiável para um símbolo -
        serve tanto de fallback (símbolos onde o WS ainda não validou ou
        perdeu a confiança) quanto de referência contínua de validação
        cruzada para o próprio WS (spot_ws.update_rest_reference).
        """
        while True:
            try:
                spot_tickers = await mexc_rest.fetch_spot_tickers(self.http_client)
                async with self._lock:
                    for state in self._iter_active_pairs():
                        data = spot_tickers.get(state.spot_symbol)
                        if not data:
                            continue

                        # Sempre alimenta a referência de validação do WS,
                        # independente de estar usando REST ou WS como fonte
                        # ativa no momento.
                        self.spot_ws.update_rest_reference(state.spot_symbol, data["price"])

                        # Só usa o preço REST para atualizar o estado se o WS
                        # ainda não for confiável para este símbolo - caso
                        # contrário, o preço já vem (mais rápido) via
                        # _on_spot_ws_update, e o REST aqui serve só de
                        # referência de validação, sem sobrescrever.
                        if not self.spot_ws.is_trusted(state.spot_symbol):
                            state.spot_price = data["price"]
                            state.spot_bid = data.get("bid") or None
                            state.spot_ask = data.get("ask") or None
                            state.spot_vol = data["vol"]
                            # Registra se veio do book (ask preenchido) ou se
                            # caiu no fallback lastPrice - o bot usa isso para
                            # recusar operar em cima de preço não-executável.
                            self._spot_rest_has_book[state.spot_symbol] = data.get("ask", 0) > 0
                            state.spot_bid_qty = data.get("bid_qty") or None
                            state.spot_ask_qty = data.get("ask_qty") or None
                            if state.spot_bid:
                                state.spot_book_ts = time.time()
                            state.recompute_spread()
                            state.last_update_ts = time.time()
                            await self._register_and_maybe_crossing(state, defer_commit=True)
                        else:
                            # Ainda atualiza o que o bookTicker do WS não traz:
                            # volume, quantidade do topo e a faixa de 24h.
                            state.spot_vol = data["vol"]
                            state.spot_bid_qty = data.get("bid_qty") or None
                            state.spot_ask_qty = data.get("ask_qty") or None

                        # A faixa de 24h vem só do REST, independente de o WS
                        # estar sendo usado como fonte de preço.
                        state.spot_high_24h = data.get("high_24h") or None
                        state.spot_low_24h = data.get("low_24h") or None
                await self.storage.commit()
                await self._broadcast_snapshot()
            except Exception as e:
                logger.warning("Erro no polling de spot: %s", e)
            await asyncio.sleep(SPOT_POLL_INTERVAL)

    # ---------------- Callback do WS de spot (bookTicker, quando confiável) ----------------

    async def _on_spot_ws_update(self, spot_symbol: str, bid: float, ask: float):
        """
        Chamado pelo SpotBookTickerWebSocketClient a cada atualização já
        validada contra REST. Usa o ASK (melhor preço de venda do book)
        como `spot_price` - é o preço que efetivamente se paga ao comprar
        a mercado, que é o que a perna Spot da estratégia sempre faz.
        Mesmo campo usado pelo REST (mexc_rest.fetch_spot_tickers).
        """
        async with self._lock:
            for state in self.pairs.values():
                if state.spot_symbol != spot_symbol:
                    continue
                state.spot_price = ask
                state.spot_bid = bid
                state.spot_ask = ask
                state.spot_book_ts = time.time()
                state.recompute_spread()
                state.last_update_ts = time.time()
                await self._register_and_maybe_crossing(state)
                break
        await self._broadcast_snapshot()

    async def _on_spot_ws_validation_change(self, spot_symbol: str, trusted: bool):
        status = "confiável (em uso)" if trusted else "não confiável (voltando para REST)"
        logger.info("WS de Spot para %s agora está: %s", spot_symbol, status)

    # ---------------- Loop: polling REST de funding rate ----------------

    async def futures_rest_poll_loop(self):
        """
        Polling REST periódico do ticker de futures.

        O endpoint REST de futures traz bid1/ask1 (preço de execução real)
        para TODOS os contratos, diferente do canal WebSocket agregado
        (`sub.tickers`), que só traz lastPrice. Como apenas os pares
        configurados no bot recebem subscrição WebSocket individual, este
        polling garante que os demais pares do Dashboard também tenham
        preço de execução - ainda que com atualização mais lenta.

        Também aproveita a mesma resposta para atualizar o funding rate,
        que não vem em nenhum canal WebSocket (evita uma segunda chamada
        REST só para isso).

        Não sobrescreve os pares que estão recebendo book via WebSocket
        individual: para esses, o dado do WS é mais recente.
        """
        while True:
            try:
                futures_tickers = await mexc_rest.fetch_futures_tickers(self.http_client)
                async with self._lock:
                    for state in self._iter_active_pairs():
                        data = futures_tickers.get(state.futures_symbol)
                        if not data:
                            continue

                        # Funding rate e faixa de 24h: sempre atualizam (só
                        # existem no REST, nunca no WebSocket).
                        state.funding_rate = data["funding_rate"]
                        state.futures_high_24h = data.get("high_24h") or None
                        state.futures_low_24h = data.get("low_24h") or None

                        # Preço: cede a vez para o WebSocket individual (mais
                        # rápido) SOMENTE enquanto o dado dele ainda for
                        # recente.
                        #
                        # Antes, a condição era só "é prioritário e já tem
                        # book" — sem olhar a idade. O canal individual da
                        # MEXC (`sub.ticker`) só empurra atualização quando há
                        # negócios naquele contrato, então num par ilíquido
                        # (exatamente os que este bot opera) o bid/ask podia
                        # congelar por minutos enquanto este polling era
                        # instruído a não tocar nele. O resultado era um
                        # spread calculado com o spot de agora contra um
                        # futures de vários minutos atrás.
                        ws_book_age = time.time() - state.futures_book_ts
                        if (
                            self._futures_price_source.get(state.futures_symbol) == "book"
                            and state.futures_symbol in self.futures_ws._priority_symbols
                            and ws_book_age < FUTURES_WS_BOOK_MAX_AGE
                        ):
                            continue

                        if data.get("bid", 0) > 0:
                            state.futures_price = data["price"]
                            state.futures_bid = data.get("bid") or None
                            state.futures_ask = data.get("ask") or None
                            state.futures_vol = data["vol"]
                            self._futures_price_source[state.futures_symbol] = "book"
                            state.futures_book_ts = time.time()
                            state.recompute_spread()
                            state.last_update_ts = time.time()
                            await self._register_and_maybe_crossing(state, defer_commit=True)
                        else:
                            # O REST é a fonte mais completa (traz bid1 para
                            # todos os contratos). Se nem ele tem book para
                            # este par, o par realmente não tem book agora -
                            # marca como "last" para sair da tabela, em vez de
                            # manter um book velho indefinidamente.
                            state.futures_bid = None
                            state.futures_ask = None
                            state.futures_vol = data["vol"]
                            self._futures_price_source[state.futures_symbol] = "last"
                            state.recompute_spread()
                # Um único commit para todo o ciclo, em vez de um por par -
                # com centenas de pares, commits individuais dominavam o
                # tempo do ciclo (cada commit em SQLite força escrita em disco).
                await self.storage.commit()
                await self._broadcast_snapshot()
            except Exception as e:
                logger.warning("Erro no polling REST de futures: %s", e)
            await asyncio.sleep(FUTURES_REST_POLL_INTERVAL)

    # ---------------- Loop: profundidade do futures ----------------

    async def load_contract_sizes(self):
        """
        Multiplicador de todos os contratos numa chamada só.

        Sem ele a quantidade do book de futures (em CONTRATOS) não vira valor
        em USDT. Recarregado raramente: contractSize praticamente não muda, e
        listagens novas entram no ciclo seguinte.
        """
        try:
            resp = await self.http_client.get(
                f"{MEXC_FUTURES_REST_BASE}/api/v1/contract/detail",
                headers=mexc_rest.HEADERS, timeout=15,
            )
            dados = resp.json().get("data") or []
        except Exception as e:
            logger.warning("Não foi possível carregar contractSize dos futures: %s", e)
            return
        for info in dados:
            try:
                self._contract_sizes[info["symbol"]] = float(info["contractSize"])
            except (KeyError, TypeError, ValueError):
                continue
        logger.info("contractSize carregado para %d contratos de futures.", len(self._contract_sizes))

    async def futures_depth_enrichment_loop(self):
        """
        Mede a quantidade no topo do book de FUTURES.

        Existe porque o ticker de futures da MEXC não traz quantidade — só
        preço. O spot traz (`bidQty`/`askQty` no ticker 24h) e por isso sai de
        graça; o futures precisa de uma consulta de profundidade por símbolo.

        Com ~580 pares, medir todos a cada ciclo é impossível. A fila é
        priorizada pelos pares que importam: primeiro os que o bot opera
        (modo foco), depois os de maior spread absoluto — que são os que
        alguém está de fato olhando. Os demais ficam sem o número, exibido
        como vazio e não como zero: "não medido" e "sem liquidez" são coisas
        diferentes e não podem parecer a mesma na tela.
        """
        await self.load_contract_sizes()
        while True:
            await asyncio.sleep(FUTURES_DEPTH_INTERVAL_S)
            try:
                agora = time.time()
                candidatos = [
                    s for s in self._iter_active_pairs()
                    if s.futures_bid and (agora - s.futures_top_ts) > FUTURES_DEPTH_TTL_S
                ]
                # Prioridade: pares do bot primeiro, depois maior |spread|.
                candidatos.sort(
                    key=lambda s: (
                        0 if s.display_symbol in self._focus_symbols else 1,
                        -abs(s.spread_pct or 0),
                    )
                )
                alvos = candidatos[:FUTURES_DEPTH_BATCH]
                if not alvos:
                    continue

                semaforo = asyncio.Semaphore(FUTURES_DEPTH_CONCURRENCY)

                async def medir(state):
                    async with semaforo:
                        try:
                            r = await self.http_client.get(
                                f"{MEXC_FUTURES_REST_BASE}"
                                f"/api/v1/contract/depth/{state.futures_symbol}",
                                params={"limit": 5}, headers=mexc_rest.HEADERS, timeout=5,
                            )
                            d = r.json().get("data") or {}
                        except Exception:
                            return  # falha de enriquecimento nunca derruba o ciclo
                        bids, asks = d.get("bids") or [], d.get("asks") or []
                        if not bids or not asks:
                            return
                        try:
                            state.futures_bid_qty = float(bids[0][1])
                            state.futures_ask_qty = float(asks[0][1])
                            state.futures_top_ts = time.time()
                        except (TypeError, ValueError, IndexError):
                            return

                await asyncio.gather(*(medir(s) for s in alvos), return_exceptions=True)
                await self._broadcast_snapshot()
            except Exception as e:
                logger.warning("Erro no enriquecimento de profundidade do futures: %s", e)

    # ---------------- Loop: re-descoberta periódica de pares novos ----------------

    async def pair_discovery_loop(self):
        while True:
            await asyncio.sleep(PAIR_DISCOVERY_INTERVAL)
            await self.discover_pairs()

    # ---------------- Loop: recálculo de cruzamentos por janela de tempo ----------------

    async def crossing_windows_loop(self):
        """
        Recalcula periodicamente quantos cruzamentos cada par teve nas
        últimas 1h / 12h / 24h. Roda em intervalo próprio (não a cada
        snapshot) para não sobrecarregar o SQLite com queries repetidas.
        """
        while True:
            try:
                now = time.time()
                counts_1h = await self.storage.get_crossing_counts_since(now - 3600)
                counts_12h = await self.storage.get_crossing_counts_since(now - 12 * 3600)
                counts_24h = await self.storage.get_crossing_counts_since(now - 24 * 3600)

                async with self._lock:
                    for state in self._iter_active_pairs():
                        key = state.futures_symbol
                        state.crossings_1h = counts_1h.get(key, 0)
                        state.crossings_12h = counts_12h.get(key, 0)
                        state.crossings_24h = counts_24h.get(key, 0)

                await self.storage.prune_crossing_events()
                await self._broadcast_snapshot()
            except Exception as e:
                logger.warning("Erro ao recalcular janelas de cruzamento: %s", e)
            await asyncio.sleep(CROSSING_WINDOWS_REFRESH_INTERVAL)

    # ---------------- Callback do WS de futures ----------------

    async def _on_futures_ws_update(self, tickers: dict):
        self._futures_ws_last_msg_ts = time.time()
        async with self._lock:
            for state in self._iter_active_pairs():
                data = tickers.get(state.futures_symbol)
                if not data:
                    continue

                has_book = bool(data.get("has_book"))

                if has_book:
                    # Canal individual (sub.ticker): traz bid/ask reais e é a
                    # fonte mais rápida (~1s). Sempre prevalece.
                    state.futures_price = data["price"]
                    state.futures_bid = data.get("bid") or None
                    state.futures_ask = data.get("ask") or None
                    state.futures_vol = data["vol"]
                    self._futures_price_source[state.futures_symbol] = "book"
                    if state.futures_bid:
                        state.futures_book_ts = time.time()
                else:
                    # Canal agregado (sub.tickers): só tem lastPrice.
                    #
                    # NÃO pode destruir o book que o polling REST forneceu
                    # (o REST traz bid1/ask1 para todos os contratos). Antes,
                    # este bloco zerava futures_bid/ask e marcava a fonte como
                    # "last" a cada 2s, enquanto o REST restaurava a cada 5s -
                    # os dois ficavam se sobrescrevendo, fazendo os pares
                    # aparecerem e sumirem da tabela (575 -> 160 -> 355...).
                    #
                    # Aqui só atualizamos o volume e, se ainda não houver book
                    # algum para o par, o preço de referência.
                    state.futures_vol = data["vol"]
                    if not state.futures_bid:
                        state.futures_price = data["price"]
                        self._futures_price_source.setdefault(state.futures_symbol, "last")

                state.recompute_spread()
                state.last_update_ts = time.time()
                await self._register_and_maybe_crossing(state, defer_commit=True)
        await self.storage.commit()
        await self._broadcast_snapshot()

    # ---------------- Fallback REST caso WS de futures fique quieto ----------------

    async def futures_ws_fallback_loop(self):
        """Se o WS de futures não emitir nada por muito tempo, faz polling REST como fallback."""
        while True:
            await asyncio.sleep(REST_FALLBACK_POLL_INTERVAL)
            silent_for = time.time() - self._futures_ws_last_msg_ts
            if self._futures_ws_last_msg_ts == 0 or silent_for > 10:
                try:
                    futures_tickers = await mexc_rest.fetch_futures_tickers(self.http_client)
                    async with self._lock:
                        for state in self._iter_active_pairs():
                            data = futures_tickers.get(state.futures_symbol)
                            if not data:
                                continue
                            state.futures_price = data["price"]
                            state.futures_bid = data.get("bid") or None
                            state.futures_ask = data.get("ask") or None
                            state.futures_vol = data["vol"]
                            state.funding_rate = data["funding_rate"]
                            # O REST de futures TRAZ bid1/ask1, então este
                            # fallback fornece preço de execução real - marca
                            # como "book" quando o bid veio preenchido.
                            self._futures_price_source[state.futures_symbol] = (
                                "book" if data.get("bid", 0) > 0 else "last"
                            )
                            if state.futures_bid:
                                state.futures_book_ts = time.time()
                            state.recompute_spread()
                            state.last_update_ts = time.time()
                            await self._register_and_maybe_crossing(state, defer_commit=True)
                    await self.storage.commit()
                    await self._broadcast_snapshot()
                except Exception as e:
                    logger.warning("Erro no fallback REST de futures: %s", e)

    # ---------------- Cruzamentos ----------------

    async def _register_and_maybe_crossing(self, state: PairState, defer_commit: bool = False):
        """
        Registra a amostra de spread, detecta cruzamentos e atualiza extremos.

        `defer_commit=True` evita um commit de banco por par - quem chama em
        loop sobre muitos pares deve usar isso e chamar `storage.commit()`
        uma única vez ao final. Com centenas de pares monitorados, um commit
        por par domina completamente o tempo do ciclo (cada commit em SQLite
        força escrita em disco).
        """
        if state.spread_pct is None:
            return

        # Os preços deste cálculo vieram do book (executáveis) ou são
        # "último negociado" (que em pares ilíquidos pode estar muito longe
        # do executável)? Usado tanto para decidir se vale registrar
        # extremos quanto para o bot decidir se pode operar.
        futures_from_book = self._futures_price_source.get(state.futures_symbol) == "book"
        spot_from_book = self.spot_ws.is_trusted(state.spot_symbol) or self._spot_rest_has_book.get(
            state.spot_symbol, False
        )
        prices_from_book = futures_from_book and spot_from_book

        # NADA é registrado sem preços do book. Cruzamentos, histórico de
        # spread (sparkline) e extremos calculados a partir de "último
        # negociado" são ficção: em pares sem book ativo, o preço pode estar
        # completamente descolado da realidade (chegando a ordens de grandeza
        # de diferença), gerando cruzamentos fantasma e recordes impossíveis.
        if not prices_from_book:
            return

        # Janelas móveis: mesma porta de entrada dos extremos, e portanto o
        # mesmo critério de "só com preço do book".
        state.record_spread_sample()

        result = await self.storage.register_spread_sample(
            state.futures_symbol, state.spread_pct, defer_commit=defer_commit
        )
        state.crossings_count = result["count"]
        state.last_crossing_ts = result["last_crossing_ts"]

        extremes = await self.storage.update_spread_extremes(
            state.futures_symbol, state.spread_pct, defer_commit=defer_commit
        )
        state.min_spread_pct = extremes["min_spread_pct"]
        state.min_spread_ts = extremes["min_spread_ts"]
        state.max_spread_pct = extremes["max_spread_pct"]
        state.max_spread_ts = extremes["max_spread_ts"]

        # Extremos do spread de SAÍDA, rastreados na tabela própria.
        if state.exit_spread_pct is not None:
            exit_ext = await self.storage.update_spread_extremes(
                state.futures_symbol, state.exit_spread_pct,
                defer_commit=defer_commit, table="exit_spread_extremes",
            )
            state.min_exit_spread_pct = exit_ext["min_spread_pct"]
            state.min_exit_spread_ts = exit_ext["min_spread_ts"]
            state.max_exit_spread_pct = exit_ext["max_spread_pct"]
            state.max_exit_spread_ts = exit_ext["max_spread_ts"]

        if self.on_price_update is not None and state.spot_price and state.futures_price:
            try:
                now = time.time()
                await self.on_price_update(
                    state.display_symbol, state.spot_price, state.futures_price, state.spread_pct,
                    prices_from_book=prices_from_book,
                    exit_spread_pct=state.exit_spread_pct,
                    spot_bid=state.spot_bid, futures_ask=state.futures_ask,
                    # Idade de cada lado do book, medida separadamente. É o que
                    # permite ao bot recusar uma decisão em que um lado está
                    # atual e o outro congelado - ver `_book_too_old`.
                    spot_book_age_s=(now - state.spot_book_ts) if state.spot_book_ts else None,
                    futures_book_age_s=(now - state.futures_book_ts) if state.futures_book_ts else None,
                    funding_rate=state.funding_rate,
                )
            except Exception as e:
                logger.warning("Erro no callback on_price_update (bot): %s", e)

    # ---------------- Status de conexão ----------------

    def compute_connection_status(self) -> str:
        if self.futures_ws.connected:
            return "online"
        silent_for = time.time() - self._futures_ws_last_msg_ts if self._futures_ws_last_msg_ts else 999
        if silent_for < 15:
            return "online"  # fallback REST está cobrindo bem
        return "reconectando"

    # ---------------- Snapshot / broadcast ----------------

    def get_snapshot(self) -> dict:
        """
        Monta o snapshot enviado ao Dashboard.

        Pares cujo preço de Futures NÃO vem do book (sem ⚡) são OMITIDOS:
        eles exibem o "último negociado", que em pares sem negociação recente
        pode estar completamente descolado da realidade - chegando a ordens
        de grandeza de diferença (ex: spot 0,27 e "futures" 97,92). Mostrar
        isso só polui a tela com spreads fictícios e atrapalha a leitura das
        oportunidades reais.
        """
        pairs_list = []
        hidden = 0
        for s in self.pairs.values():
            futures_source = self._futures_price_source.get(s.futures_symbol, "last")
            if futures_source != "book":
                hidden += 1
                continue
            d = s.to_dict(contract_size=self._contract_sizes.get(s.futures_symbol, 1.0))
            d["spot_price_source"] = "websocket" if self.spot_ws.is_trusted(s.spot_symbol) else "rest"
            d["futures_price_source"] = futures_source
            pairs_list.append(d)
        return {
            "type": "snapshot",
            "connection_status": self.compute_connection_status(),
            "pairs": pairs_list,
            "hidden_pairs_without_book": hidden,
            "server_time": time.time(),
        }

    async def _broadcast_snapshot(self):
        """
        Marca que há novidade. O envio de verdade acontece no
        `broadcast_loop`, no máximo uma vez por `BROADCAST_MIN_INTERVAL_S`.

        Coalescer é seguro porque um snapshot é o estado inteiro: se dois
        forem marcados no mesmo intervalo, o segundo já contém tudo que o
        primeiro tinha. Nada é perdido, só o desperdício.
        """
        self._snapshot_dirty = True

    async def broadcast_loop(self):
        """Envia no máximo um snapshot por intervalo, e só se algo mudou."""
        while True:
            await asyncio.sleep(BROADCAST_MIN_INTERVAL_S)
            if not self._snapshot_dirty:
                continue
            self._snapshot_dirty = False
            try:
                await self._broadcast(self.get_snapshot())
            except Exception as e:
                logger.warning("Erro ao enviar snapshot: %s", e)

    async def broadcast_now(self):
        """
        Envio imediato, sem esperar o intervalo. Reservado para mudanças que
        o usuário acabou de provocar (ligar modo foco, resetar extremos), em
        que esperar até 1s pareceria travamento.
        """
        self._snapshot_dirty = False
        await self._broadcast(self.get_snapshot())

    async def clear_spread_extremes(self, display_symbol: str | None = None) -> int:
        """
        Reseta os extremos (mín/máx histórico) de spread, tanto no banco
        quanto no estado em memória (senão o valor antigo continuaria
        aparecendo na tela até o backend reiniciar).

        Sem `display_symbol`, reseta todos os pares.
        """
        if display_symbol:
            state = self.pairs.get(display_symbol)
            if state is None:
                return 0
            removed = await self.storage.clear_spread_extremes(state.futures_symbol)
            removed += await self.storage.clear_spread_extremes(
                state.futures_symbol, table="exit_spread_extremes"
            )
            async with self._lock:
                state.min_spread_pct = None
                state.min_spread_ts = None
                state.max_spread_pct = None
                state.max_spread_ts = None
                state.min_exit_spread_pct = None
                state.min_exit_spread_ts = None
                state.max_exit_spread_pct = None
                state.max_exit_spread_ts = None
        else:
            removed = await self.storage.clear_spread_extremes()
            removed += await self.storage.clear_spread_extremes(table="exit_spread_extremes")
            async with self._lock:
                for state in self.pairs.values():
                    state.min_spread_pct = None
                    state.min_spread_ts = None
                    state.max_spread_pct = None
                    state.max_spread_ts = None
                    state.min_exit_spread_pct = None
                    state.min_exit_spread_ts = None
                    state.max_exit_spread_pct = None
                    state.max_exit_spread_ts = None

        await self.broadcast_now()
        return removed

    async def get_spread_history(self, display_symbol: str) -> list:
        state = self.pairs.get(display_symbol)
        if not state:
            return []
        return await self.storage.get_spread_history(state.futures_symbol)

    # ---------------- Lifecycle ----------------

    async def start(self):
        await self.discover_pairs()
        asyncio.create_task(self.futures_ws.run())
        asyncio.create_task(self.spot_ws.run())
        asyncio.create_task(self.spot_poll_loop())
        asyncio.create_task(self.futures_rest_poll_loop())
        asyncio.create_task(self.pair_discovery_loop())
        asyncio.create_task(self.futures_ws_fallback_loop())
        asyncio.create_task(self.crossing_windows_loop())
        self._broadcast_task = asyncio.create_task(self.broadcast_loop())
        # Referência forte: uma task sem referência pode ser coletada pelo
        # garbage collector no meio da execução, parando sem erro nenhum.
        self._depth_task = asyncio.create_task(self.futures_depth_enrichment_loop())

    async def shutdown(self):
        await self.futures_ws.stop()
        await self.spot_ws.stop()
        await self.http_client.aclose()
