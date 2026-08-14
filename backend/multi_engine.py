"""
Motor multi-exchange: cotações por venue e spreads por combinação.

## O que mudou em relação ao engine.py original

O `engine.py` modela um par como "spot e futures da MEXC" — dois preços fixos
por símbolo. Aqui um símbolo é uma linha de cotações **por venue**
(`mexc:spot`, `gate:futures`, `bingx:futures`, ...), e as combinações de
arbitragem são DERIVADAS na hora de montar o snapshot, não armazenadas.

Isso importa porque o número de combinações é grande (12 por símbolo, ~5900
monitoráveis medidas em 05/08/2026) e volátil: um venue que perde o book
some das combinações dele sem afetar as outras. Guardar combinações como
estado exigiria invalidar tudo a cada tick; derivá-las é O(1) por linha e
sempre coerente com as cotações do instante.

## Os dois spreads continuam valendo, generalizados

A distinção entrada/saída — a decisão mais importante deste projeto — vale
para qualquer par de venues:

    ENTRADA = (venda.BID - compra.ASK) / compra.ASK
        compra-se no ask do venue de compra, vende-se no bid do de venda.

    SAÍDA   = (venda.ASK - compra.BID) / compra.BID
        desfaz nos lados OPOSTOS do book dos dois venues.

O que era "spot × futures da MEXC" virou "venue de compra × venue de venda".
A fórmula é idêntica; o que mudou é de onde vêm os dois books.

## Frescor por venue, nunca agregado

Cada `Quote` carrega seu próprio `book_ts`. Com seis fontes assíncronas, um
carimbo agregado esconderia exatamente a assimetria que produz spread
fantasma — a lição do bug 12, agora com três vezes mais superfície para
errar.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from exchanges.base import MarketType, Quote, Venue
from exchanges.registry import Combination, build_adapters, build_combinations

logger = logging.getLogger("multi_engine")

#: Intervalo de polling de cada venue. Todos os seis são consultados em
#: paralelo, então o ciclo dura o tempo do venue mais lento, não a soma.
POLL_INTERVAL_S = 5.0

#: Idade a partir da qual uma cotação para de contar como utilizável no
#: dashboard. Generoso o suficiente para tolerar um ciclo perdido, curto o
#: bastante para não exibir preço de minutos atrás como se fosse de agora.
MAX_QUOTE_AGE_S = 20.0

# ---------------------------------------------------------------------------
# Consenso de preço: a proteção mais importante do modo multi-exchange
# ---------------------------------------------------------------------------
#
# O MESMO TICKER EM DUAS EXCHANGES NÃO É NECESSARIAMENTE O MESMO ATIVO.
#
# Medições reais de 05/08/2026, com os seis venues ao vivo:
#
#   BTC    todos os 6 venues concordam dentro de 0,07%      <- normal
#   ETH    todos os 6 venues concordam dentro de 0,06%      <- normal
#
#   VANRY  5 venues em ~0,00333, mas gate:spot em 0,001451  <- 2,3x menor
#   COTI   todos os futures em ~0,0135, todos os spots em ~0,0108  <- 20%
#   SPCX   gate:futures 115,66 contra gate:spot 99,88       <- 15%, MESMA exchange
#
# Nenhum desses três é oportunidade. São redenominação de token, migração
# para v2 (o spot passa a negociar o token novo enquanto o futures ainda
# referencia o antigo) e mercado pré-lançamento. Montar a "arbitragem" de
# VANRY compraria um ativo e venderia OUTRO: exposição direcional integral,
# hedge nenhum, numa estratégia cuja premissa inteira é ser neutra.
#
# Note que o caso COTI derruba a intuição de que basta comparar exchanges: a
# divergência ali é entre SPOT e FUTURES, inclusive dentro da mesma exchange.
# Por isso a verificação é por venue contra o consenso de todos, e não
# "exchange A contra exchange B".
#
# Desvio máximo de um venue em relação à mediana dos demais antes de ele ser
# considerado um ativo diferente e removido das combinações.
MAX_VENUE_DEVIATION_PCT = 5.0

# Com menos de 3 venues não há mediana confiável — não dá para saber QUAL dos
# dois lados é o estranho. Nesse caso não excluímos ninguém, mas qualquer
# spread acima deste valor é marcado como suspeito: spreads reais desta
# estratégia vivem na casa de 1-3%, e dois dígitos quase sempre significam
# que os dois lados não são o mesmo ativo.
MAX_PLAUSIBLE_SPREAD_PCT = 10.0

#: Mínimo de venues com book para a mediana significar alguma coisa.
MIN_VENUES_FOR_CONSENSUS = 3

# --- Profundidade de topo (a coluna "$" da tabela) ---
# Quanto tempo uma medição de quantidade do topo continua valendo. Curto o
# bastante para não mostrar liquidez que já sumiu, longo o bastante para não
# remedir a cada ciclo.
# Precisa ser MAIOR que o tempo de cobertura completa da fila, senão a
# medição expira antes de a rodada seguinte chegar nela e o número nunca
# estabiliza — a fila vira trabalho de Sísifo. Com ~120 alvos a 16 por 4s, a
# cobertura leva ~30s; 90s dá margem confortável.
#
# Um número de profundidade com um minuto de idade continua respondendo bem a
# pergunta que ele existe para responder ("dá para operar $10 ou $10.000 aqui?").
TOP_OF_BOOK_TTL_S = 90.0
# Quantas medições por rodada, e com que frequência.
#
# Calibrado para baixo depois de medir o efeito colateral: com 40 medições a
# cada 3s, as chamadas de profundidade competiam com o polling de tickers e
# derrubavam venues inteiros ("Venue mexc:futures falhou neste ciclo"). O
# preço é o dado crítico; a profundidade é enriquecimento. Quando os dois
# disputam banda, quem cede é o enriquecimento.
TOP_OF_BOOK_BATCH = 16
TOP_OF_BOOK_INTERVAL_S = 4.0
#: Chamadas simultâneas de profundidade. Baixa de propósito, mesmo motivo.
TOP_OF_BOOK_CONCURRENCY = 4
#: Quantas LINHAS do topo do ranking recebem medição de profundidade.
HOT_ROWS_MAX = 60
#: Por quanto tempo um alvo continua na fila depois de sair da tela.
HOT_TARGET_TTL_S = 120.0
#: Teto da fila, para o enriquecimento não perseguir linhas que ninguém vê.
HOT_TARGETS_MAX = 240


@dataclass
class ConsensusResult:
    """
    Veredito do consenso de preço de um símbolo: qual é o preço de
    referência e quais venues destoam a ponto de não serem o mesmo ativo.
    """
    reference_price: Optional[float]
    deviations_pct: dict[str, float]      # venue_key -> desvio da mediana
    outliers: set[str]                     # venues descartados
    venues_considered: int

    @property
    def has_consensus(self) -> bool:
        return self.venues_considered >= MIN_VENUES_FOR_CONSENSUS


def _mediana(valores: list[float]) -> float:
    ordenados = sorted(valores)
    n = len(ordenados)
    meio = n // 2
    if n % 2:
        return ordenados[meio]
    return (ordenados[meio - 1] + ordenados[meio]) / 2


def evaluate_consensus(quotes_por_venue: dict[str, Quote], now: Optional[float] = None) -> ConsensusResult:
    """
    Compara o preço médio (mid) de cada venue contra a MEDIANA de todos e
    marca como outlier quem destoar mais que `MAX_VENUE_DEVIATION_PCT`.

    Mediana, e não média, porque a média é arrastada pelo próprio outlier que
    se quer detectar: com 6 venues em que 1 está 2,3x fora (o caso VANRY), a
    média sobe o suficiente para o outlier parecer menos absurdo e para os
    venues corretos parecerem levemente errados. A mediana é indiferente a
    quantos valores extremos existam, desde que sejam minoria.

    Com menos de `MIN_VENUES_FOR_CONSENSUS` venues não há como saber qual
    lado é o estranho, então ninguém é excluído — a proteção nesse caso é o
    limite de plausibilidade do spread, aplicado em `compute_spread`.
    """
    agora = now if now is not None else time.time()
    mids: dict[str, float] = {}
    for chave, q in quotes_por_venue.items():
        if not q.has_book or q.age_s(agora) > MAX_QUOTE_AGE_S:
            continue
        mids[chave] = (q.bid + q.ask) / 2

    if not mids:
        return ConsensusResult(None, {}, set(), 0)

    referencia = _mediana(list(mids.values()))
    desvios = {
        chave: abs(mid - referencia) / referencia * 100 if referencia > 0 else 0.0
        for chave, mid in mids.items()
    }

    outliers: set[str] = set()
    if len(mids) >= MIN_VENUES_FOR_CONSENSUS:
        outliers = {c for c, d in desvios.items() if d > MAX_VENUE_DEVIATION_PCT}

    return ConsensusResult(
        reference_price=referencia,
        deviations_pct=desvios,
        outliers=outliers,
        venues_considered=len(mids),
    )


@dataclass
class CombinationSpread:
    """Os dois spreads de uma combinação, mais o contexto para julgá-los."""
    combination: Combination
    entry_spread_pct: float
    exit_spread_pct: float
    buy_ask: float
    buy_bid: float
    sell_ask: float
    sell_bid: float
    funding_buy: float
    funding_sell: float
    buy_top_usdt: Optional[float]   # quanto dá para COMPRAR ao preço de compra
    sell_top_usdt: Optional[float]  # quanto dá para VENDER ao preço de venda
    vol_usdt_min: float          # o menor volume 24h das duas pernas
    max_age_s: float             # a idade do lado MAIS VELHO
    fee_round_trip_pct: float
    # Motivo pelo qual esta linha NÃO deve ser operada, ou None se está
    # limpa. Guardar o motivo em vez de simplesmente omitir a linha é
    # deliberado: o operador precisa ver que VANRY tem 128% de "spread" E
    # que isso é uma colisão de símbolo, senão vai procurar o número sumido.
    suspect_reason: Optional[str] = None
    extremes: Optional[dict] = None

    @property
    def tradeable(self) -> bool:
        return self.suspect_reason is None

    @property
    def net_spread_pct(self) -> float:
        """
        Spread de entrada já descontado das taxas das quatro pernas.

        É o número que responde "sobra dinheiro nesta linha?", em vez de
        "qual o spread bruto?". Numa combinação cross-exchange envolvendo a
        Gate (taker 0,075% em futures contra 0,02% da MEXC), a diferença de
        taxa sozinha pode inverter a ordem de atratividade das linhas.
        """
        return self.entry_spread_pct - self.fee_round_trip_pct

    def to_dict(self) -> dict:
        c = self.combination
        return {
            "key": c.key,
            "symbol": c.symbol,
            "buy_venue": c.buy_venue.key,
            "sell_venue": c.sell_venue.key,
            "buy_venue_label": c.buy_venue.label,
            "sell_venue_label": c.sell_venue.label,
            "kind": c.kind,
            "cross_exchange": c.cross_exchange,
            "entry_spread_pct": self.entry_spread_pct,
            "exit_spread_pct": self.exit_spread_pct,
            "net_spread_pct": self.net_spread_pct,
            "fee_round_trip_pct": self.fee_round_trip_pct,
            "buy_ask": self.buy_ask, "buy_bid": self.buy_bid,
            "sell_ask": self.sell_ask, "sell_bid": self.sell_bid,
            "buy_top_usdt": self.buy_top_usdt,
            "sell_top_usdt": self.sell_top_usdt,
            # O menor dos dois lados: a operação inteira é limitada pela perna
            # mais rasa, exatamente como uma corrente pelo elo mais fraco.
            "executable_top_usdt": (
                min(self.buy_top_usdt, self.sell_top_usdt)
                if self.buy_top_usdt is not None and self.sell_top_usdt is not None else None
            ),
            "funding_buy": self.funding_buy, "funding_sell": self.funding_sell,
            "vol_usdt": self.vol_usdt_min,
            "max_age_s": self.max_age_s,
            "suspect_reason": self.suspect_reason,
            "tradeable": self.tradeable,
            **(self.extremes or {}),
        }


def compute_spread(
    combination: Combination, buy: Quote, sell: Quote,
    fee_round_trip_pct: float, now: Optional[float] = None,
    consensus: Optional[ConsensusResult] = None,
    buy_contract_size: float = 1.0, sell_contract_size: float = 1.0,
) -> Optional[CombinationSpread]:
    """
    Calcula os dois spreads de uma combinação a partir das duas cotações.

    Devolve None se qualquer uma das pernas não tiver book completo. Não há
    fallback para `last`: em combinação cross-exchange a tentação é maior
    (basta um venue ilíquido para a linha sumir), e é exatamente aí que o
    número inventado seria mais convincente e mais errado.

    Quando o consenso de preço aponta uma das pernas como outlier, ou quando
    o spread é grande demais para ser plausível, a linha volta marcada com
    `suspect_reason` — calculada, visível, e barrada para o bot.
    """
    if not buy.has_book or not sell.has_book:
        return None

    agora = now if now is not None else time.time()

    entrada = (sell.bid - buy.ask) / buy.ask * 100
    motivo = None

    if consensus is not None and consensus.outliers:
        fora = [
            v.label for v, chave in (
                (combination.buy_venue, combination.buy_venue.key),
                (combination.sell_venue, combination.sell_venue.key),
            ) if chave in consensus.outliers
        ]
        if fora:
            desvio = max(
                consensus.deviations_pct.get(combination.buy_venue.key, 0),
                consensus.deviations_pct.get(combination.sell_venue.key, 0),
            )
            motivo = (
                f"{', '.join(fora)} destoa {desvio:.1f}% da mediana dos {consensus.venues_considered} "
                f"venues — provavelmente não é o mesmo ativo (redenominação, token v2 ou pré-mercado)"
            )

    if motivo is None and abs(entrada) > MAX_PLAUSIBLE_SPREAD_PCT:
        motivo = (
            f"spread de {entrada:.1f}% é implausível para arbitragem real; "
            f"quase sempre significa que os dois lados não são o mesmo ativo"
        )

    return CombinationSpread(
        combination=combination,
        suspect_reason=motivo,
        # Entrada: paga-se o ask de quem se compra, recebe-se o bid de quem se vende.
        entry_spread_pct=entrada,
        # Saída: recebe-se o bid de quem se comprou, paga-se o ask de quem se vendeu.
        exit_spread_pct=(sell.ask - buy.bid) / buy.bid * 100,
        buy_ask=buy.ask, buy_bid=buy.bid,
        sell_ask=sell.ask, sell_bid=sell.bid,
        # Entrada compra no ASK de quem se compra e vende no BID de quem se
        # vende: é a profundidade DESSES dois lados que limita a operação.
        buy_top_usdt=buy.top_usdt("ask", buy_contract_size),
        sell_top_usdt=sell.top_usdt("bid", sell_contract_size),
        funding_buy=buy.funding_rate, funding_sell=sell.funding_rate,
        vol_usdt_min=min(buy.vol_usdt, sell.vol_usdt),
        # A idade relevante é a do lado MAIS VELHO: um spread com um lado
        # atual e outro parado é o pior caso, não a média dos dois.
        max_age_s=max(buy.age_s(agora), sell.age_s(agora)),
        fee_round_trip_pct=fee_round_trip_pct,
    )


class MultiVenueEngine:
    """
    Mantém as cotações de todos os venues e serve as combinações prontas.

    Deliberadamente sem WebSocket nesta camada: com seis venues e três
    protocolos diferentes, o polling REST em paralelo entrega book completo
    de todos os símbolos de uma vez e é a base correta. O WebSocket faz
    sentido depois, e só para os poucos símbolos que o bot opera — que é
    exatamente o desenho que já existe do lado da MEXC.
    """

    def __init__(self, http_client, fee_table: Optional[dict[str, float]] = None):
        self.adapters = build_adapters(http_client)
        # symbol -> venue_key -> Quote
        self.quotes: dict[str, dict[str, Quote]] = {}
        self.venue_status: dict[str, dict] = {
            k: {"ok": False, "symbols": 0, "last_ok_ts": 0.0, "error": None}
            for k in self.adapters
        }
        # Taxa de taker por venue, em percentual. Preenchida com os padrões
        # conhecidos e refinada por `load_fees` quando os metadados chegam.
        self.fees_pct: dict[str, float] = fee_table or {
            "mexc:spot": 0.05, "mexc:futures": 0.02,
            "gate:spot": 0.20, "gate:futures": 0.075,
            "bingx:spot": 0.10, "bingx:futures": 0.05,
        }
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._running = False
        # Referência forte à task de enriquecimento.
        #
        # `asyncio.create_task` NÃO mantém a task viva sozinho: o event loop
        # guarda só uma referência fraca, e uma task sem referência forte pode
        # ser coletada pelo garbage collector no meio da execução — parando
        # silenciosamente, sem erro nenhum. Foi exatamente o que aconteceu
        # aqui: o enriquecimento funcionava em teste curto e morria no
        # servidor depois de alguns ciclos.
        self._enrichment_task = None
        # Instrumentação do enriquecimento. Sem ela, "a coluna está vazia" não
        # distingue "a task morreu", "a fila está vazia" e "as chamadas estão
        # falhando" — três causas com correções opostas.
        self.enrichment_stats = {"rounds": 0, "targets": 0, "measured": 0, "failed": 0, "last_error": None}
        # Extremos e cruzamentos por combinação, em memória e persistidos em
        # lote. combo_key -> dict. Ver multi_storage.py para por que o
        # histórico do gráfico NÃO mora aqui.
        self.extremes: dict[str, dict] = {}
        self.storage = None
        self._extremes_dirty: set[str] = set()
        # Símbolos que apareceram no último snapshot servido. O
        # enriquecimento de profundidade é dirigido por eles: medir os ~3800
        # símbolos seria impossível, e o número só importa nas linhas que
        # alguém está olhando.
        # Pares (símbolo, venue) que aparecem em alguma tela, com o instante
        # em que foram vistos pela última vez.
        #
        # ACUMULA entre consumidores em vez de ser sobrescrito. Há vários
        # consumidores simultâneos com filtros DIFERENTES — o WebSocket do
        # navegador, cada aba aberta, chamadas REST — e cada `get_snapshot`
        # devolve um conjunto de linhas diferente. Atribuir a lista fazia o
        # último a servir snapshot apagar os alvos de todos os outros: a fila
        # oscilava entre 105 e 0 e quase nada era medido.
        #
        # É o antipadrão de "múltiplas fontes escrevendo no mesmo estado, a
        # última vence por acidente" (bug 8), pela terceira vez neste projeto
        # e em mais um campo diferente. A precedência aqui é explícita:
        # ninguém apaga alvo de ninguém; o que remove é a idade.
        self._hot_targets: dict[tuple[str, str], float] = {}
        # venue_key -> {símbolo: multiplicador do contrato}. Spot é sempre 1;
        # futures varia (contrato de JIMOTHY vale 100 moedas, o de BTC vale
        # 0,0001), e sem isso a quantidade do book não vira valor em USDT.
        self.contract_sizes: dict[str, dict[str, float]] = {}

    # ---------------- Pub/sub ----------------

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=10)
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
                pass  # cliente lento: o próximo snapshot corrige

    # ---------------- Coleta ----------------

    async def poll_once(self):
        """
        Consulta os seis venues EM PARALELO e substitui as cotações.

        Paralelo por dois motivos: o ciclo passa a durar o tempo do venue
        mais lento em vez da soma dos seis; e as cotações descrevem
        aproximadamente o mesmo instante, o que é pré-requisito para
        comparar preços entre elas. Um spread montado com um lado de agora e
        outro de cinco segundos atrás é o tipo de número que parece válido e
        não é.
        """
        chaves = list(self.adapters.keys())
        resultados = await asyncio.gather(
            *(self.adapters[k].fetch_tickers() for k in chaves),
            return_exceptions=True,
        )

        # Funding da BingX exige chamada própria (não vem no ticker de swap).
        funding_bingx: dict[str, float] = {}
        bingx_fut = self.adapters.get("bingx:futures")
        if bingx_fut is not None and hasattr(bingx_fut, "fetch_funding_rates"):
            try:
                funding_bingx = await bingx_fut.fetch_funding_rates()
            except Exception as e:
                logger.warning("Funding da BingX indisponível neste ciclo: %s", e)

        agora = time.time()
        async with self._lock:
            for chave, resultado in zip(chaves, resultados):
                status = self.venue_status[chave]
                if isinstance(resultado, Exception):
                    # `str(excecao)` vem VAZIO em várias exceções de rede do
                    # httpx (ReadTimeout, ConnectError), e um log dizendo só
                    # "falhou neste ciclo:" não permite diagnosticar nada.
                    # O tipo é o que identifica a causa.
                    descricao = f"{type(resultado).__name__}: {resultado}".strip().rstrip(":")
                    status.update(ok=False, error=descricao[:200])
                    logger.warning("Venue %s falhou neste ciclo: %s", chave, descricao)
                    continue

                if chave == "bingx:futures" and funding_bingx:
                    for sym, q in resultado.items():
                        q.funding_rate = funding_bingx.get(sym, 0.0)

                status.update(ok=True, symbols=len(resultado), last_ok_ts=agora, error=None)
                for sym, quote in resultado.items():
                    anterior = self.quotes.get(sym, {}).get(chave)
                    # A cotação nova vem do ticker em massa, que (fora a BingX)
                    # não traz quantidade do topo. Substituir o objeto inteiro
                    # apagaria a profundidade medida pelo enriquecedor a cada
                    # 5 segundos — e como o snapshot é servido logo após o
                    # polling, o valor NUNCA chegaria à tela, apesar de a
                    # medição estar funcionando.
                    #
                    # É o antipadrão de "duas fontes assíncronas escrevendo no
                    # mesmo estado, a pior vence por chegar depois" (bug 8),
                    # reaparecido em outro campo. A precedência aqui é
                    # explícita: quem mede profundidade manda na profundidade,
                    # quem mede preço manda no preço.
                    if (
                        anterior is not None
                        and quote.bid_qty is None
                        and anterior.top_ts
                        and (agora - anterior.top_ts) < TOP_OF_BOOK_TTL_S
                    ):
                        quote.bid_qty = anterior.bid_qty
                        quote.ask_qty = anterior.ask_qty
                        quote.top_ts = anterior.top_ts
                    self.quotes.setdefault(sym, {})[chave] = quote

            # Cotações que pararam de ser renovadas saem de circulação em vez
            # de ficarem congeladas no último valor conhecido.
            for sym, por_venue in list(self.quotes.items()):
                for chave, q in list(por_venue.items()):
                    if q.age_s(agora) > MAX_QUOTE_AGE_S:
                        del por_venue[chave]
                if not por_venue:
                    del self.quotes[sym]

    async def enrich_top_of_book(self, alvos: list[tuple[str, str]], concurrency: int = TOP_OF_BOOK_CONCURRENCY):
        """
        Mede a QUANTIDADE disponível no topo do book para `(símbolo, venue)`.

        Existe porque só a BingX devolve `bidQty`/`askQty` no ticker em massa;
        MEXC e Gate exigem uma consulta de profundidade por símbolo. Com ~3800
        símbolos isso é impossível de fazer para todos — e desnecessário: o
        número só importa nas linhas que o operador está de fato olhando.

        Por isso o enriquecimento é dirigido pela DEMANDA (os símbolos que
        aparecem no snapshot filtrado), limitado em quantidade e com
        concorrência limitada, para não competir com o polling dos tickers
        nem esbarrar em rate limit.

        O que este número responde é a pergunta que o preço sozinho não
        responde: "quanto dá para executar A ESTE PREÇO". Um spread de 3%
        sobre 5 dólares de profundidade não é uma oportunidade — é o mesmo
        erro do bug 11, agora visível na tela antes de virar ordem.
        """
        if not alvos:
            return

        semaforo = asyncio.Semaphore(concurrency)

        async def medir(simbolo: str, venue_key: str):
            adapter = self.adapters.get(venue_key)
            if adapter is None:
                return
            async with semaforo:
                try:
                    book = await adapter.fetch_depth(simbolo, limit=5)
                except Exception as e:
                    # Falha de enriquecimento nunca derruba o snapshot, mas
                    # precisa ficar contabilizada.
                    self.enrichment_stats["failed"] += 1
                    self.enrichment_stats["last_error"] = f"{venue_key}: {type(e).__name__}: {e}"[:200]
                    return
            if book is None or not book.is_usable:
                return
            async with self._lock:
                q = self.quotes.get(simbolo, {}).get(venue_key)
                if q is None:
                    return
                q.bid_qty = book.bids[0].qty
                q.ask_qty = book.asks[0].qty
                q.top_ts = time.time()
                self.enrichment_stats["measured"] += 1

        await asyncio.gather(*(medir(s, v) for s, v in alvos), return_exceptions=True)

    def _alvos_de_enriquecimento(self, limite: int) -> list[tuple[str, str]]:
        """
        Escolhe quais `(símbolo, venue)` medir: os que aparecem nas linhas em
        destaque e ainda não têm quantidade recente.
        """
        agora = time.time()
        alvos: list[tuple[str, str]] = []
        for simbolo, venue_key in list(self._hot_targets.keys()):
            q = self.quotes.get(simbolo, {}).get(venue_key)
            if q is None or not q.has_book:
                continue
            # Já medido há pouco: não gasta chamada de novo.
            if q.top_ts and (agora - q.top_ts) < TOP_OF_BOOK_TTL_S:
                continue
            alvos.append((simbolo, venue_key))
            if len(alvos) >= limite:
                break
        return alvos

    async def enrichment_loop(self):
        """
        Mantém a profundidade de topo atualizada para os símbolos em
        destaque. Roda separado do polling de tickers para que uma consulta
        lenta de profundidade nunca atrase a atualização dos preços — que é o
        dado crítico.
        """
        while self._running:
            try:
                alvos = self._alvos_de_enriquecimento(TOP_OF_BOOK_BATCH)
                self.enrichment_stats["rounds"] += 1
                self.enrichment_stats["targets"] = len(alvos)
                if alvos:
                    await self.enrich_top_of_book(alvos)
            except Exception as e:
                logger.warning("Erro no enriquecimento de profundidade: %s", e)
            await asyncio.sleep(TOP_OF_BOOK_INTERVAL_S)

    async def run(self):
        self._running = True
        await self.load_contract_sizes()
        await self.load_extremes()
        self._enrichment_task = asyncio.create_task(self.enrichment_loop())
        while self._running:
            inicio = time.time()
            try:
                await self.poll_once()
                self.update_extremes()
                await self.flush_extremes()
                await self._broadcast({"type": "multi_snapshot_tick", "server_time": time.time()})
            except Exception as e:
                logger.error("Erro no ciclo do motor multi-exchange: %s", e)
            decorrido = time.time() - inicio
            logger.debug("Ciclo multi-exchange em %.2fs", decorrido)
            await asyncio.sleep(max(0.5, POLL_INTERVAL_S - decorrido))

    async def stop(self):
        self._running = False

    # ---------------- Consulta ----------------

    def update_extremes(self):
        """
        Percorre TODAS as combinações e atualiza mínimos, máximos e
        cruzamentos.

        Roda sobre o universo inteiro (não sobre o snapshot filtrado) de
        propósito: um extremo só é histórico se for observado continuamente.
        Registrar apenas o que passou pelo filtro da tela faria o mínimo e o
        máximo dependerem de quais filtros alguém deixou abertos — o recorde
        de um par mudaria conforme a janela do navegador, o que não é um
        recorde.

        Combinações suspeitas (consenso de preço) ficam de FORA: os 128% do
        VANRY viveriam para sempre no máximo histórico, e todo par colidido
        exibiria um recorde impossível — exatamente o problema que o
        `clear_spread_extremes` do dashboard antigo existe para limpar.
        """
        agora = time.time()
        for simbolo in list(self.quotes.keys()):
            for spread in self.spreads_for_symbol(simbolo, None, agora):
                if not spread.tradeable:
                    continue
                c = spread.combination
                e = self.extremes.get(c.key)
                if e is None:
                    e = {
                        "symbol": c.symbol,
                        "buy_venue": c.buy_venue.key, "sell_venue": c.sell_venue.key,
                        "min_entry_pct": None, "min_entry_ts": None,
                        "max_entry_pct": None, "max_entry_ts": None,
                        "min_exit_pct": None, "min_exit_ts": None,
                        "max_exit_pct": None, "max_exit_ts": None,
                        "crossings": 0, "last_crossing_ts": None,
                        "_last_sign": None,
                    }
                    self.extremes[c.key] = e

                mudou = False
                for campo, valor in (("entry", spread.entry_spread_pct), ("exit", spread.exit_spread_pct)):
                    if e[f"min_{campo}_pct"] is None or valor < e[f"min_{campo}_pct"]:
                        e[f"min_{campo}_pct"], e[f"min_{campo}_ts"] = valor, agora
                        mudou = True
                    if e[f"max_{campo}_pct"] is None or valor > e[f"max_{campo}_pct"]:
                        e[f"max_{campo}_pct"], e[f"max_{campo}_ts"] = valor, agora
                        mudou = True

                # Cruzamento = o spread de entrada trocar de sinal. É o que
                # indica um par que oscila em torno do zero (oportunidade
                # recorrente) em vez de um que ficou parado num extremo.
                sinal = 1 if spread.entry_spread_pct >= 0 else -1
                if e["_last_sign"] is not None and sinal != e["_last_sign"]:
                    e["crossings"] += 1
                    e["last_crossing_ts"] = agora
                    mudou = True
                e["_last_sign"] = sinal

                if mudou:
                    self._extremes_dirty.add(c.key)

    async def flush_extremes(self):
        """Grava em UM único commit tudo que mudou desde a última gravação."""
        if self.storage is None or not self._extremes_dirty:
            return
        agora = time.time()
        chaves = list(self._extremes_dirty)
        self._extremes_dirty.clear()
        registros = []
        for chave in chaves:
            e = self.extremes.get(chave)
            if e is None:
                continue
            registros.append((
                chave, e["symbol"], e["buy_venue"], e["sell_venue"],
                e["min_entry_pct"], e["min_entry_ts"], e["max_entry_pct"], e["max_entry_ts"],
                e["min_exit_pct"], e["min_exit_ts"], e["max_exit_pct"], e["max_exit_ts"],
                e["crossings"], e["last_crossing_ts"], agora,
            ))
        try:
            await self.storage.save_batch(registros)
        except Exception as ex:
            logger.warning("Falha ao gravar extremos: %s", ex)

    async def load_extremes(self):
        if self.storage is None:
            return
        try:
            salvos = await self.storage.load_all()
        except Exception as ex:
            logger.warning("Falha ao carregar extremos: %s", ex)
            return
        for chave, dados in salvos.items():
            partes = chave.split("|")
            if len(partes) != 3:
                continue
            self.extremes[chave] = {
                "symbol": partes[0], "buy_venue": partes[1], "sell_venue": partes[2],
                **dados, "_last_sign": None,
            }
        logger.info("Extremos de %d combinações carregados do banco.", len(self.extremes))

    def contract_size(self, venue_key: str, symbol: str) -> float:
        """Multiplicador do contrato, com 1.0 como padrão seguro para spot."""
        return self.contract_sizes.get(venue_key, {}).get(symbol, 1.0)

    async def load_contract_sizes(self):
        """
        Carrega o multiplicador de todos os contratos de futures — três
        chamadas no total, uma por exchange. Roda na inicialização e é
        recarregada raramente: contractSize praticamente não muda, e listagens
        novas entram no próximo ciclo de recarga.
        """
        for chave, adapter in self.adapters.items():
            if not hasattr(adapter, "fetch_all_contract_sizes"):
                continue
            try:
                self.contract_sizes[chave] = await adapter.fetch_all_contract_sizes()
                logger.info(
                    "Multiplicadores de contrato carregados para %s: %d símbolos.",
                    chave, len(self.contract_sizes[chave]),
                )
            except Exception as e:
                logger.warning("Multiplicadores de contrato de %s indisponíveis: %s", chave, e)

    def fee_round_trip(self, combination: Combination) -> float:
        """
        Taxa taker das QUATRO pernas da operação completa: abrir os dois
        lados e depois fechar os dois.
        """
        compra = self.fees_pct.get(combination.buy_venue.key, 0.1)
        venda = self.fees_pct.get(combination.sell_venue.key, 0.1)
        return (compra + venda) * 2

    def venues_with_book(self, symbol: str, now: Optional[float] = None) -> list[Venue]:
        agora = now if now is not None else time.time()
        return [
            Venue.from_key(chave)
            for chave, q in self.quotes.get(symbol, {}).items()
            if q.has_book and q.age_s(agora) <= MAX_QUOTE_AGE_S
        ]

    def spreads_for_symbol(
        self, symbol: str, enabled_venues: Optional[Iterable[Venue]] = None,
        now: Optional[float] = None,
    ) -> list[CombinationSpread]:
        agora = now if now is not None else time.time()
        por_venue = self.quotes.get(symbol, {})
        combos = build_combinations(symbol, self.venues_with_book(symbol, agora), enabled_venues)

        # O consenso é calculado sobre TODOS os venues do símbolo, não só os
        # habilitados pelo filtro: um venue filtrado da tela ainda é evidência
        # válida sobre qual é o preço verdadeiro do ativo. Restringir a
        # amostra ao filtro faria o mesmo símbolo ser considerado confiável ou
        # suspeito dependendo de quais colunas o operador escolheu ver, o que
        # é precisamente o oposto do que uma verificação de sanidade deve ser.
        consenso = evaluate_consensus(por_venue, agora)

        out = []
        for combo in combos:
            compra = por_venue.get(combo.buy_venue.key)
            venda = por_venue.get(combo.sell_venue.key)
            if compra is None or venda is None:
                continue
            spread = compute_spread(
                combo, compra, venda, self.fee_round_trip(combo), agora, consensus=consenso,
                buy_contract_size=self.contract_size(combo.buy_venue.key, symbol),
                sell_contract_size=self.contract_size(combo.sell_venue.key, symbol),
            )
            if spread is not None:
                e = self.extremes.get(combo.key)
                if e is not None:
                    spread.extremes = {
                        k: v for k, v in e.items()
                        if k not in ("symbol", "buy_venue", "sell_venue", "_last_sign")
                    }
                out.append(spread)
        return out

    def get_snapshot(
        self,
        enabled_venues: Optional[Iterable[Venue]] = None,
        min_net_spread_pct: Optional[float] = None,
        kinds: Optional[Iterable[str]] = None,
        min_vol_usdt: float = 0.0,
        limit: int = 400,
        symbol: Optional[str] = None,
        include_suspect: bool = False,
        min_max_entry_pct: Optional[float] = None,
    ) -> dict:
        """
        Monta o snapshot do dashboard já filtrado e ordenado.

        A filtragem acontece NO SERVIDOR de propósito. São ~5900 combinações
        monitoráveis; serializar todas a cada ciclo e deixar o navegador
        filtrar desperdiçaria banda e travaria a interface — e nenhum operador
        olha 5900 linhas. O `limit` é a última rede: mesmo sem filtro nenhum,
        a resposta tem tamanho previsível.
        """
        agora = time.time()
        kinds_set = set(kinds) if kinds else None
        simbolos = [symbol] if symbol else list(self.quotes.keys())

        linhas: list[CombinationSpread] = []
        suspeitas = 0
        for sym in simbolos:
            for spread in self.spreads_for_symbol(sym, enabled_venues, agora):
                if kinds_set and spread.combination.kind not in kinds_set:
                    continue
                if min_vol_usdt and spread.vol_usdt_min < min_vol_usdt:
                    continue
                # Filtro por MÁXIMO HISTÓRICO: mostra só pares que JÁ chegaram
                # a um spread interessante alguma vez, mesmo que agora estejam
                # parados. É o filtro que separa "par que oscila e volta a
                # abrir" de "par que nunca abriu nada" — um spread de 0,1%
                # agora num par que já bateu 3% é uma oportunidade dormindo;
                # num par cujo recorde é 0,2%, é o teto dele.
                if min_max_entry_pct is not None:
                    maximo = (spread.extremes or {}).get("max_entry_pct")
                    if maximo is None or maximo < min_max_entry_pct:
                        continue
                if not spread.tradeable:
                    suspeitas += 1
                    # Linhas suspeitas ficam FORA por padrão. Elas têm os
                    # maiores spreads do sistema (VANRY apareceu com 128%) e
                    # ocupariam o topo de qualquer ordenação, empurrando as
                    # oportunidades reais para fora da tela.
                    if not include_suspect:
                        continue
                elif min_net_spread_pct is not None and spread.net_spread_pct < min_net_spread_pct:
                    continue
                linhas.append(spread)

        total = len(linhas)
        # Ordenado pelo spread LÍQUIDO, não pelo bruto: é o líquido que diz
        # onde há dinheiro, e ordenar pelo bruto colocaria no topo linhas de
        # exchanges caras que não pagam a própria taxa.
        linhas.sort(key=lambda s: s.net_spread_pct, reverse=True)

        # Alimenta o enriquecimento com as pernas desta tela, SOMANDO ao que
        # outros consumidores já pediram.
        for linha in linhas[:HOT_ROWS_MAX]:
            c = linha.combination
            self._hot_targets[(c.symbol, c.buy_venue.key)] = agora
            self._hot_targets[(c.symbol, c.sell_venue.key)] = agora

        # Poda: alvos que ninguém olha há um tempo saem da fila, senão ela
        # cresce sem limite e o enriquecimento passa a gastar chamadas com
        # linhas que ninguém está vendo.
        limite_idade = agora - HOT_TARGET_TTL_S
        self._hot_targets = {
            alvo: visto for alvo, visto in self._hot_targets.items() if visto >= limite_idade
        }
        if len(self._hot_targets) > HOT_TARGETS_MAX:
            mais_recentes = sorted(self._hot_targets.items(), key=lambda kv: kv[1], reverse=True)
            self._hot_targets = dict(mais_recentes[:HOT_TARGETS_MAX])

        return {
            "type": "multi_snapshot",
            "server_time": agora,
            "rows": [linha.to_dict() for linha in linhas[:limit]],
            "total_matching": total,
            "returned": min(total, limit),
            "suspect_filtered": suspeitas,
            "symbols_tracked": len(self.quotes),
            "venues": self.venue_summary(),
        }

    def enrichment_summary(self) -> dict:
        return {**self.enrichment_stats, "queue": len(self._hot_targets)}

    def venue_summary(self) -> list[dict]:
        agora = time.time()
        out = []
        for chave, status in self.venue_status.items():
            venue = Venue.from_key(chave)
            out.append({
                "key": chave,
                "label": venue.label,
                "exchange": venue.exchange,
                "market": venue.market.value,
                "ok": status["ok"],
                "symbols": status["symbols"],
                "age_s": (agora - status["last_ok_ts"]) if status["last_ok_ts"] else None,
                "error": status["error"],
                "taker_fee_pct": self.fees_pct.get(chave),
            })
        return out
