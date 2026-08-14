"""
Conversão entre "valor em USDT desejado" e as unidades nativas de cada mercado.

- Spot: a MEXC aceita quoteOrderQty diretamente (gastar/receber X USDT), então
  não precisamos calcular quantidade manualmente — a própria exchange faz a
  conversão ao preço de execução. Ainda assim, aplicamos os filtros de
  quantidade mínima/passo do símbolo antes de enviar, para não ter a ordem
  rejeitada por regra de lote.

- Futures: não existe "valor em quote" nativo. É preciso calcular `vol`
  (quantidade de CONTRATOS, não de moeda) a partir do valor em USDT desejado:

      vol = valor_usdt / (preco * contractSize)

  arredondado para baixo no múltiplo de contrato mais próximo (nunca para
  cima, para não expor mais capital do que configurado), respeitando minVol.
"""
import math
from dataclasses import dataclass


@dataclass
class ContractSpec:
    symbol: str
    contract_size: float   # valor de 1 contrato, na moeda base (ex: 0.0001 BTC por contrato)
    min_vol: int            # quantidade mínima de contratos por ordem
    vol_scale: int = 0       # casas decimais permitidas em vol (normalmente 0, contratos são inteiros)
    # `priceUnit` é o TICK de preço do contrato (ex: 0.000001). Toda ordem
    # LIMITE precisa ter o preço alinhado a um múltiplo exato desse tick, ou
    # a MEXC rejeita. Só passou a importar quando as ordens deixaram de ser
    # exclusivamente a mercado (ordens a mercado não carregam preço efetivo).
    price_unit: float = 0.0
    price_scale: int = 8
    taker_fee_pct: float = 0.02  # percentual, não fração (0.02 = 0,02%)

    @classmethod
    def from_api_response(cls, data: dict) -> "ContractSpec":
        # Importado aqui e não no topo para manter sizing.py sem dependência
        # de módulos que dependem dele (costs.py é folha, mas a direção da
        # dependência fica mais óbvia assim).
        from bot.costs import parse_futures_taker_pct

        price_scale = int(data.get("priceScale", 8) or 8)
        try:
            price_unit = float(data.get("priceUnit", 0) or 0)
        except (TypeError, ValueError):
            price_unit = 0.0
        # Sem priceUnit no payload, deriva o tick da escala de preço: com
        # priceScale=6, o tick é 0.000001. É a mesma grandeza expressa de
        # duas formas, e a MEXC nem sempre devolve as duas.
        if price_unit <= 0:
            price_unit = 10 ** (-price_scale)

        return cls(
            symbol=data["symbol"],
            contract_size=float(data["contractSize"]),
            min_vol=int(data.get("minVol", 1)),
            vol_scale=int(data.get("volScale", 0)),
            price_unit=price_unit,
            price_scale=price_scale,
            taker_fee_pct=parse_futures_taker_pct(data),
        )


def usdt_to_futures_vol(usdt_value: float, price: float, spec: ContractSpec) -> int:
    """
    Calcula quantos contratos comprar/vender para expor aproximadamente
    `usdt_value` USDT de valor nocional, ao preço atual `price`.

    Arredonda para BAIXO (nunca expõe mais do que o configurado). Retorna 0
    se o valor solicitado for menor que 1 contrato — nesse caso o chamador
    deve tratar como "valor insuficiente para este par" e não enviar ordem.
    """
    if price <= 0 or spec.contract_size <= 0:
        return 0

    raw_vol = usdt_value / (price * spec.contract_size)
    step = 10 ** (-spec.vol_scale) if spec.vol_scale > 0 else 1
    vol = math.floor(raw_vol / step) * step

    if spec.vol_scale == 0:
        vol = int(vol)

    if vol < spec.min_vol:
        return 0

    return vol


def futures_vol_to_usdt(vol: float, price: float, spec: ContractSpec) -> float:
    """Valor nocional em USDT de uma posição/ordem de `vol` contratos ao preço `price`."""
    return vol * spec.contract_size * price


@dataclass
class SpotSymbolSpec:
    symbol: str
    base_asset_precision: int
    quote_precision: int
    min_notional: float = 0.0
    # Casas decimais permitidas no PREÇO de uma ordem limite. Distinto de
    # `quote_precision` (que é a precisão do VALOR em USDT usado no
    # quoteOrderQty): um par pode aceitar 2 casas no valor total e 8 no
    # preço unitário. Confundir os dois gera "price scale is invalid".
    price_precision: int = 8
    taker_fee_pct: float = 0.05  # percentual, não fração (0.05 = 0,05%)

    @classmethod
    def from_exchange_info(cls, data: dict) -> "SpotSymbolSpec":
        from bot.costs import parse_spot_taker_pct

        min_notional = 0.0
        for f in data.get("filters", []):
            if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = float(f.get("minNotional", f.get("minNotionalValue", 0)) or 0)

        quote_precision = int(data.get("quotePrecision", 8))
        return cls(
            symbol=data["symbol"],
            base_asset_precision=int(data.get("baseAssetPrecision", 8)),
            quote_precision=quote_precision,
            min_notional=min_notional,
            price_precision=int(data.get("quoteAssetPrecision", quote_precision) or quote_precision),
            taker_fee_pct=parse_spot_taker_pct(data),
        )


def validate_spot_quote_qty(usdt_value: float, spec: SpotSymbolSpec) -> bool:
    """Confirma que o valor em USDT respeita o notional mínimo exigido pela MEXC para este par."""
    if spec.min_notional and usdt_value < spec.min_notional:
        return False
    return usdt_value > 0


def round_spot_quantity(qty: float, spec: SpotSymbolSpec) -> float:
    """
    Arredonda uma quantidade do ativo base para o número de casas decimais
    permitido pela MEXC (baseAssetPrecision), sempre para BAIXO - nunca
    arredonda para cima, para não correr o risco de vender mais do que o
    saldo real disponível. Sem isso, a MEXC rejeita a ordem com o erro
    "amount scale is invalid" quando a quantidade tem mais casas decimais
    do que o permitido (o que acontece com frequência, já que o saldo
    consultado via API pode vir com arredondamento de ponto flutuante
    acumulado, ex: 1188.70000001 em vez de 1188.7).
    """
    if qty <= 0:
        return 0.0
    factor = 10 ** spec.base_asset_precision
    return math.floor(qty * factor) / factor


def _quantize(value: float, tick: float, up: bool) -> float:
    """
    Alinha `value` a um múltiplo exato de `tick`, para baixo ou para cima.

    A tolerância de 1e-9 no número de ticks existe porque a divisão
    `value / tick` acumula erro de ponto flutuante de forma traiçoeira:
    0.007 / 0.000001 dá 6999.999999999999 em vez de 7000, e um `floor`
    ingênuo devolveria um tick a MENOS — deslocando o preço da ordem para
    fora do que foi calculado, silenciosamente, só de vez em quando.
    """
    if tick <= 0:
        return value
    ticks = value / tick
    ticks = math.ceil(ticks - 1e-9) if up else math.floor(ticks + 1e-9)
    # Segundo arredondamento: a multiplicação de volta reintroduz erro
    # (7000 * 0.000001 = 0.007000000000000001), e a MEXC rejeita preço com
    # mais casas do que o tick comporta.
    decimals = max(0, min(12, -math.floor(math.log10(tick)) if tick < 1 else 0))
    return round(ticks * tick, decimals)


def round_price_for_buy(price: float, tick: float) -> float:
    """
    Arredonda o preço de uma ordem LIMITE de COMPRA para BAIXO, no tick.

    A direção não é arbitrária: o preço vem de um teto de slippage
    calculado ("não aceito pagar mais que X"). Arredondar para cima
    estouraria esse teto — pouco, mas estouraria, e a garantia deixaria de
    ser uma garantia. Arredondar para baixo, no pior caso, deixa a ordem
    um tick menos agressiva e ela não preenche; isso o retry resolve, um
    fill acima do teto não.
    """
    if price <= 0:
        return 0.0
    return _quantize(price, tick, up=False)


def round_price_for_sell(price: float, tick: float) -> float:
    """
    Arredonda o preço de uma ordem LIMITE de VENDA para CIMA, no tick.
    Mesma lógica de `round_price_for_buy`, espelhada: o teto aqui é um piso
    ("não aceito receber menos que X").
    """
    if price <= 0:
        return 0.0
    return _quantize(price, tick, up=True)


def spot_price_tick(spec: SpotSymbolSpec) -> float:
    """Tick de preço do símbolo Spot, derivado da precisão de preço."""
    return 10 ** (-spec.price_precision)


def round_spot_quote_qty(quote_qty: float, spec: SpotSymbolSpec) -> float:
    """
    Arredonda um valor em moeda de cotação (USDT) para o número de casas
    decimais permitido pela MEXC (quotePrecision) ao enviar uma ordem via
    quoteOrderQty. Sempre para BAIXO, pelo mesmo motivo de
    round_spot_quantity: o valor calculado internamente (ex: a partir do
    fill do Futures, via multiplicação de floats) frequentemente vem com
    mais casas decimais do que o permitido, gerando "amount scale is
    invalid" mesmo em ordens de COMPRA por valor (não só em vendas por
    quantidade).
    """
    if quote_qty <= 0:
        return 0.0
    factor = 10 ** spec.quote_precision
    return math.floor(quote_qty * factor) / factor
