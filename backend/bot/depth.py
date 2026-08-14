"""
Preço EXECUTÁVEL por profundidade de book — o núcleo da correção de slippage.

## O problema que este módulo resolve

Até aqui, todo o sistema calculava spread a partir do TOPO do book
(`bid1`/`ask1` no futures, `bookTicker` no spot). O topo do book é o melhor
preço disponível **para a quantidade que existe naquela primeira camada** —
que em memecoin ilíquida frequentemente é uma fração do tamanho da posição.
Uma ordem a mercado maior que essa camada "anda" o book, consumindo camadas
progressivamente piores, e o preço médio de execução fica muito distante do
topo que estava na tela.

Caso real medido (JIMOTHY, 03/08/2026, notional de apenas 4,30 USDT):

    Entrada:  tela +2,0255%  ->  realizado +1,4146%   (-0,61 pp)
    Saída:    tela +0,2323%  ->  realizado +1,4395%   (+1,21 pp)

O PnL desta estratégia é exatamente `spread_entrada - spread_saída`. Pela
tela: 2,0255 - 0,2323 = +1,79% esperado. Realizado: 1,4146 - 1,4395 =
-0,025%. Ou seja: **1,82 pontos percentuais — o lucro inteiro da operação —
foram consumidos por execução**, não por o mercado ter se movido.

## A solução

Em vez de perguntar "qual o melhor preço do book?", perguntar "qual o preço
MÉDIO que eu vou pagar/receber ao executar EXATAMENTE o meu tamanho contra
este book, agora?". Isso é o VWAP da caminhada pelas camadas.

Com isso o número na tela passa a ser o número que se executa, e o bot
recusa operações que o book não comporta — em vez de descobrir isso depois,
no preço de fill.

## Convenção de unidades (importante, já causou confusão)

- Book SPOT: `qty` de cada camada é a quantidade do ativo BASE (ex: JIMOTHY).
- Book FUTURES: `qty` de cada camada é a quantidade de CONTRATOS, não de
  moeda. Para converter em moeda base, multiplique por `contractSize`.

Nenhuma função deste módulo converte unidades sozinha: quem chama passa a
quantidade já na unidade do book correspondente. Isso é deliberado — a
conversão implícita entre contratos e moeda base é exatamente o tipo de erro
silencioso que este projeto não pode ter.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

logger = logging.getLogger("bot_depth")

# Tolerância relativa para considerar um preenchimento "completo". Evita que
# ruído de ponto flutuante (a soma de dezenas de camadas nunca bate no último
# bit) faça um book que comporta o tamanho ser classificado como insuficiente.
_COMPLETE_EPS = 1e-9


@dataclass(frozen=True)
class BookLevel:
    """Uma camada do livro de ofertas. `qty` está na unidade nativa do book."""
    price: float
    qty: float


@dataclass(frozen=True)
class ExecPrice:
    """
    Resultado de "caminhar" o book para executar um tamanho específico.

    `complete=False` significa que o book NÃO tem profundidade suficiente
    para o tamanho pedido. Quem chama deve tratar isso como impedimento de
    operar, nunca executar o que couber — executar parcialmente deixa as duas
    pernas descasadas, que é o pior estado possível nesta estratégia.
    """
    avg_price: float        # VWAP da execução — o preço que de fato se paga/recebe
    top_price: float        # preço do topo do book (o que a tela mostrava antes)
    filled_qty: float       # quanto o book comportou (na unidade do book)
    requested_qty: float    # quanto foi pedido
    notional: float         # valor total movimentado (preço * qty somado)
    levels_used: int        # quantas camadas foram consumidas
    complete: bool          # o book comportou o tamanho inteiro?

    @property
    def depth_slippage_pct(self) -> float:
        """
        Quanto o preço médio de execução se afasta do topo do book, em %,
        SEMPRE como número positivo (é um custo, independente do lado).

        É a métrica que mede diretamente "o quanto a tela estava mentindo":
        um valor de 1,2% aqui significa que o topo do book prometia um preço
        1,2% melhor do que o tamanho da posição consegue de verdade.
        """
        if self.top_price <= 0 or self.avg_price <= 0:
            return 0.0
        return abs(self.avg_price - self.top_price) / self.top_price * 100


@dataclass
class OrderBook:
    """
    Snapshot de um livro de ofertas.

    `bids` deve vir ordenado do maior preço para o menor, `asks` do menor
    para o maior — a ordem natural em que uma ordem a mercado os consome.
    O construtor NÃO reordena de propósito: reordenar mascararia um payload
    corrompido da exchange, e preferimos falhar visivelmente (ver
    `is_crossed`) a operar em cima de um book que chegou errado.
    """
    symbol: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def age_s(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.ts

    @property
    def is_crossed(self) -> bool:
        """
        Book cruzado (melhor bid >= melhor ask) é fisicamente impossível num
        livro consistente. Quando acontece, o snapshot chegou corrompido, ou
        os dois lados vieram de momentos diferentes. Operar em cima disso
        produz spread fantasma — então quem chama deve recusar.
        """
        if not self.bids or not self.asks:
            return False
        return self.bids[0].price >= self.asks[0].price

    @property
    def is_usable(self) -> bool:
        return bool(self.bids) and bool(self.asks) and not self.is_crossed

    def buy(self, qty: float) -> Optional[ExecPrice]:
        """Preço médio ao COMPRAR `qty` a mercado (consome os asks)."""
        return walk_by_qty(self.asks, qty)

    def sell(self, qty: float) -> Optional[ExecPrice]:
        """Preço médio ao VENDER `qty` a mercado (consome os bids)."""
        return walk_by_qty(self.bids, qty)

    def buy_with_quote(self, quote_amount: float) -> Optional[ExecPrice]:
        """Preço médio ao COMPRAR gastando `quote_amount` de USDT (consome os asks)."""
        return walk_by_quote(self.asks, quote_amount)


def walk_by_qty(levels: Sequence[BookLevel], qty: float) -> Optional[ExecPrice]:
    """
    Caminha o book consumindo camadas até completar `qty` (na unidade do
    book) e devolve o preço médio ponderado dessa execução.

    Retorna None só quando não há nada com que trabalhar (book vazio ou
    quantidade não-positiva). Quando o book existe mas não comporta o
    tamanho, retorna um ExecPrice com `complete=False` e o VWAP do que
    coube — o valor parcial é útil para diagnóstico e para o log, mas
    NUNCA deve ser usado para executar (ver docstring de ExecPrice).
    """
    if qty <= 0 or not levels:
        return None

    filled = 0.0
    notional = 0.0
    levels_used = 0

    for level in levels:
        if level.price <= 0 or level.qty <= 0:
            continue  # camada inválida no payload: ignora em vez de contaminar o VWAP
        remaining = qty - filled
        if remaining <= 0:
            break
        take = min(level.qty, remaining)
        filled += take
        notional += take * level.price
        levels_used += 1

    if filled <= 0:
        return None

    return ExecPrice(
        avg_price=notional / filled,
        top_price=levels[0].price,
        filled_qty=filled,
        requested_qty=qty,
        notional=notional,
        levels_used=levels_used,
        complete=filled >= qty * (1 - _COMPLETE_EPS),
    )


def walk_by_quote(levels: Sequence[BookLevel], quote_amount: float) -> Optional[ExecPrice]:
    """
    Variante para COMPRA no Spot por valor em USDT (`quoteOrderQty`), que é
    como a perna Spot de entrada é enviada: a MEXC recebe quanto gastar, não
    quantas moedas comprar.

    Aqui `requested_qty`/`filled_qty` do resultado ficam na moeda BASE
    (quantas moedas o valor compra), enquanto `notional` é o valor em USDT
    efetivamente gasto — é o par de números necessário para dimensionar a
    perna espelho de futures.
    """
    if quote_amount <= 0 or not levels:
        return None

    spent = 0.0
    acquired = 0.0
    levels_used = 0

    for level in levels:
        if level.price <= 0 or level.qty <= 0:
            continue
        remaining_quote = quote_amount - spent
        if remaining_quote <= 0:
            break
        level_quote = level.price * level.qty
        take_quote = min(level_quote, remaining_quote)
        spent += take_quote
        acquired += take_quote / level.price
        levels_used += 1

    if acquired <= 0:
        return None

    complete = spent >= quote_amount * (1 - _COMPLETE_EPS)
    return ExecPrice(
        avg_price=spent / acquired,
        top_price=levels[0].price,
        filled_qty=acquired,
        # A quantidade "pedida" em moeda base só é conhecida a posteriori;
        # quando o book comportou tudo, pedido == obtido.
        requested_qty=acquired if complete else acquired,
        notional=spent,
        levels_used=levels_used,
        complete=complete,
    )


# ---------------------------------------------------------------------------
# Parsing dos payloads da MEXC
# ---------------------------------------------------------------------------

def parse_spot_depth(payload: dict, symbol: str) -> Optional[OrderBook]:
    """
    Converte a resposta de `GET /api/v3/depth` (Spot).

    Formato: {"lastUpdateId": .., "bids": [["preco","qtd"], ..], "asks": [..]}
    Preços e quantidades vêm como STRING — converter com float é obrigatório,
    comparar strings de preço daria ordenação lexicográfica silenciosamente
    errada.
    """
    if not isinstance(payload, dict):
        return None
    try:
        bids = [BookLevel(float(p), float(q)) for p, q in payload.get("bids", [])]
        asks = [BookLevel(float(p), float(q)) for p, q in payload.get("asks", [])]
    except (TypeError, ValueError) as e:
        logger.warning("Book Spot de %s veio em formato inesperado: %s", symbol, e)
        return None
    return OrderBook(symbol=symbol, bids=bids, asks=asks)


def parse_futures_depth(payload: dict, symbol: str) -> Optional[OrderBook]:
    """
    Converte a resposta de `GET /api/v1/contract/depth/{symbol}` (Futures).

    Formato: {"success": true, "data": {"bids": [[preco, vol, ordens], ..],
              "asks": [..], "timestamp": ms}}

    Duas diferenças relevantes em relação ao Spot:
    - `vol` está em CONTRATOS, não na moeda base.
    - cada camada tem um terceiro elemento (número de ordens) que ignoramos.
    - o timestamp do servidor vem em MILISSEGUNDOS.
    """
    if not isinstance(payload, dict) or not payload.get("success", True):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    try:
        bids = [BookLevel(float(level[0]), float(level[1])) for level in data.get("bids", [])]
        asks = [BookLevel(float(level[0]), float(level[1])) for level in data.get("asks", [])]
    except (TypeError, ValueError, IndexError) as e:
        logger.warning("Book Futures de %s veio em formato inesperado: %s", symbol, e)
        return None

    ts = time.time()
    raw_ts = data.get("timestamp")
    if raw_ts:
        try:
            # A MEXC manda em ms. Só aceitamos o timestamp do servidor se ele
            # for plausível em relação ao relógio local: se o relógio da
            # máquina estiver dessincronizado, um ts remoto "do futuro" faria
            # o book parecer eternamente fresco, derrubando a proteção de
            # staleness justamente quando ela mais importa.
            remote = float(raw_ts) / 1000.0
            if abs(remote - ts) < 30:
                ts = remote
        except (TypeError, ValueError):
            pass

    return OrderBook(symbol=symbol, bids=bids, asks=asks, ts=ts)


# ---------------------------------------------------------------------------
# Spreads executáveis (as duas grandezas que o bot decide em cima)
# ---------------------------------------------------------------------------

@dataclass
class ExecutableSpread:
    """
    O spread que de fato se consegue executar AGORA, para um tamanho
    específico, contra a profundidade real dos dois books.

    `spread_pct` é a grandeza comparável com `entry_spread_pct` /
    `exit_spread_pct` da configuração; `screen_spread_pct` é o mesmo cálculo
    feito com o topo do book (o número que a tela mostrava até então). A
    diferença entre os dois é exatamente o slippage que o bot vinha comendo
    sem enxergar.
    """
    spread_pct: float
    screen_spread_pct: float
    spot: ExecPrice
    futures: ExecPrice
    complete: bool

    @property
    def depth_cost_pct(self) -> float:
        """Quanto o spread executável é pior que o spread de topo de book, em pp."""
        return abs(self.screen_spread_pct - self.spread_pct)


def _spread(sell_price: float, buy_price: float) -> float:
    """
    Spread percentual de uma operação casada, sempre com o preço da perna
    COMPRADA no denominador (é sobre esse valor que o capital é imobilizado).
    """
    if buy_price <= 0:
        return 0.0
    return (sell_price - buy_price) / buy_price * 100


def entry_executable_spread(
    spot_book: OrderBook, futures_book: OrderBook,
    notional_usdt: float, futures_vol: float,
) -> Optional[ExecutableSpread]:
    """
    Spread de ENTRADA executável: compra Spot gastando `notional_usdt`
    (consome os asks do spot) + vende `futures_vol` contratos (consome os
    bids do futures).

    Retorna None se qualquer um dos books estiver inutilizável. Retorna com
    `complete=False` se algum dos lados não tiver profundidade para o
    tamanho — nesse caso o chamador NÃO deve operar.
    """
    if not spot_book.is_usable or not futures_book.is_usable:
        return None

    spot_exec = spot_book.buy_with_quote(notional_usdt)
    futures_exec = futures_book.sell(futures_vol)
    if spot_exec is None or futures_exec is None:
        return None

    return ExecutableSpread(
        spread_pct=_spread(futures_exec.avg_price, spot_exec.avg_price),
        screen_spread_pct=_spread(futures_exec.top_price, spot_exec.top_price),
        spot=spot_exec,
        futures=futures_exec,
        complete=spot_exec.complete and futures_exec.complete,
    )


def exit_executable_spread(
    spot_book: OrderBook, futures_book: OrderBook,
    spot_qty: float, futures_vol: float,
) -> Optional[ExecutableSpread]:
    """
    Spread de SAÍDA executável: vende `spot_qty` do ativo base (consome os
    bids do spot) + recompra `futures_vol` contratos (consome os asks do
    futures).

    Lados OPOSTOS aos da entrada — a distinção entre os dois spreads é a
    decisão mais importante deste projeto e a origem do bug mais caro que já
    foi corrigido aqui. Ver CLAUDE.md, "Conceito central: DOIS spreads".
    """
    if not spot_book.is_usable or not futures_book.is_usable:
        return None

    spot_exec = spot_book.sell(spot_qty)
    futures_exec = futures_book.buy(futures_vol)
    if spot_exec is None or futures_exec is None:
        return None

    return ExecutableSpread(
        spread_pct=_spread(futures_exec.avg_price, spot_exec.avg_price),
        screen_spread_pct=_spread(futures_exec.top_price, spot_exec.top_price),
        spot=spot_exec,
        futures=futures_exec,
        complete=spot_exec.complete and futures_exec.complete,
    )
