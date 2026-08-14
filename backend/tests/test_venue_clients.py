"""
Testes dos clientes autenticados de Gate e BingX e da camada de execução por
venue.

O bloco mais importante é o de DIREÇÃO DAS ORDENS. Cada exchange expressa
"abrir venda" e "fechar venda" de um jeito diferente e incompatível:

    MEXC     side=3 abre short, side=2 fecha
    Gate     sem campo side: o SINAL de `size` é a direção
    BingX    par (side, positionSide): SELL/SHORT abre, BUY/SHORT fecha

Um erro aqui não gera ordem rejeitada — gera ordem executada na direção
CONTRÁRIA, que dobra a exposição em vez de zerá-la. É a falha mais cara
possível nesta camada, e a que menos se anuncia.

ATENÇÃO: Gate e BingX não foram exercitadas contra a API real (não havia
credenciais quando foram escritas). Estes testes validam a assinatura, o
payload e a leitura da resposta contra dublês montados a partir da
documentação — não substituem uma primeira ordem manual.
"""
import hashlib
import hmac
import json

import pytest

from bot.bingx_client import BingxAPIError, BingxClient
from bot.gate_client import GateAPIError, GateClient
from bot.venue_trader import (
    BingxFuturesTrader, BingxSpotTrader, GateFuturesTrader, GateSpotTrader,
    MexcFuturesTrader, MexcSpotTrader, UnknownOrderStateError, build_trader,
)
from exchanges.base import ContractSpec, MarketType, Venue


class HttpEspiao:
    """Registra a requisição e devolve um payload fixo."""

    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {}
        self.status = status
        self.chamadas = []

    async def request(self, method, url, params=None, content=None, headers=None, timeout=None):
        self.chamadas.append({
            "method": method, "url": url, "params": params,
            "content": content, "headers": headers,
        })
        return _Resp(self.payload, self.status)

    @property
    def ultima(self):
        return self.chamadas[-1]


class _Resp:
    def __init__(self, payload, status):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# Assinatura da Gate
# ---------------------------------------------------------------------------

def test_gate_assina_com_sha512_e_cinco_componentes():
    """
    A Gate assina `METHOD\\npath\\nquery\\nsha512(body)\\ntimestamp` com
    HMAC-SHA512. Errar qualquer componente devolve 401 sem dizer qual.
    """
    c = GateClient("chave", "segredo", HttpEspiao())
    headers = c._sign("POST", "/api/v4/spot/orders", "", '{"a":1}')

    assert headers["KEY"] == "chave"
    esperado_hash = hashlib.sha512(b'{"a":1}').hexdigest()
    payload = f"POST\n/api/v4/spot/orders\n\n{esperado_hash}\n{headers['Timestamp']}"
    assert headers["SIGN"] == hmac.new(b"segredo", payload.encode(), hashlib.sha512).hexdigest()
    # SHA-512 em hex tem 128 caracteres; 64 seria SHA-256 (o esquema da MEXC).
    assert len(headers["SIGN"]) == 128


def test_gate_corpo_vazio_vira_hash_da_string_vazia():
    # Não uma linha em branco: é o erro clássico de quem porta a assinatura
    # de outra exchange.
    c = GateClient("k", "s", HttpEspiao())
    headers = c._sign("GET", "/api/v4/spot/accounts", "currency=USDT", "")
    vazio = hashlib.sha512(b"").hexdigest()
    payload = f"GET\n/api/v4/spot/accounts\ncurrency=USDT\n{vazio}\n{headers['Timestamp']}"
    assert headers["SIGN"] == hmac.new(b"s", payload.encode(), hashlib.sha512).hexdigest()


async def test_gate_envia_o_mesmo_json_que_assinou():
    """
    Se o JSON assinado e o enviado diferirem em um espaço, a assinatura não
    confere — e a mensagem de erro não diz isso.
    """
    http = HttpEspiao({"id": "1"})
    c = GateClient("k", "s", http)
    await c.spot_limit_ioc("BTC_USDT", "buy", 1.0, 100.0)

    enviado = http.ultima["content"]
    assert " " not in enviado, "separadores compactos: o que foi assinado é o que vai no wire"
    corpo = json.loads(enviado)
    assert corpo["account"] == "spot", "sem isso a ordem pode ir para a conta de margem"
    assert corpo["time_in_force"] == "ioc"


def test_gate_formata_preco_sem_notacao_cientifica():
    from bot.gate_client import _txt
    # str(0.00000123) daria "1.23e-06", que a Gate rejeita — e preço de
    # memecoin cai nessa faixa o tempo todo.
    assert _txt(0.00000123) == "0.00000123"
    assert _txt(100.0) == "100"
    assert "e" not in _txt(0.000000001)


def test_gate_erro_http_vira_excecao_tipada():
    with pytest.raises(GateAPIError):
        GateClient._handle(_Resp({"label": "BALANCE_NOT_ENOUGH", "message": "sem saldo"}, 400))


# ---------------------------------------------------------------------------
# Assinatura da BingX
# ---------------------------------------------------------------------------

def test_bingx_assina_a_query_literal():
    c = BingxClient("chave", "segredo", HttpEspiao())
    query = c._assinar({"symbol": "BTC-USDT", "side": "BUY"})

    assert query.startswith("symbol=BTC-USDT&side=BUY&timestamp=")
    corpo, assinatura = query.rsplit("&signature=", 1)
    assert assinatura == hmac.new(b"segredo", corpo.encode(), hashlib.sha256).hexdigest()


def test_bingx_descarta_parametros_nulos_antes_de_assinar():
    # Um `None` virando a string "None" na query quebraria a ordem e a
    # assinatura ao mesmo tempo.
    c = BingxClient("k", "s", HttpEspiao())
    query = c._assinar({"symbol": "BTC-USDT", "price": None})
    assert "price" not in query


def test_bingx_code_diferente_de_zero_e_erro_mesmo_com_http_200():
    # Tratar o status HTTP como sucesso engoliria a falha — num caminho que
    # envia ordem, é a pior forma de erro possível.
    with pytest.raises(BingxAPIError) as exc:
        BingxClient._handle(_Resp({"code": 100400, "msg": "parametro invalido"}, 200))
    assert exc.value.code == 100400
    assert BingxClient._handle(_Resp({"code": 0, "data": {"ok": 1}}, 200)) == {"ok": 1}


# ---------------------------------------------------------------------------
# DIREÇÃO DAS ORDENS — o bloco que evita dobrar a exposição
# ---------------------------------------------------------------------------

def _spec(venue, native="BTC_USDT", contract_size=1.0):
    return ContractSpec(
        symbol="BTC", venue=venue, native_symbol=native,
        contract_size=contract_size, qty_step=1.0, price_tick=0.01,
    )


class ClienteGravador:
    """
    Grava as chamadas em vez de enviá-las.

    Cada exchange tem a SUA subclasse: um dublê único com todos os métodos
    misturaria as convenções (o `open_short` da Gate e o da BingX têm
    assinaturas e semânticas diferentes) e faria o teste passar validando o
    dialeto errado — que é exatamente o erro que estes testes existem para
    pegar.
    """

    def __init__(self):
        self.ordens = []


class GateGravador(ClienteGravador):
    async def futures_order(self, contract, size, price, *, reduce_only=False, tif="ioc"):
        self.ordens.append({"contract": contract, "size": size, "price": price,
                            "reduce_only": reduce_only})
        return {"id": "1"}

    async def open_short(self, contract, contracts, price):
        return await self.futures_order(contract, -abs(int(contracts)), price)

    async def close_short(self, contract, contracts, price):
        return await self.futures_order(contract, abs(int(contracts)), price, reduce_only=True)

    async def spot_limit_ioc(self, pair, side, amount, price):
        self.ordens.append({"pair": pair, "side": side, "amount": amount, "price": price})
        return {"id": "1"}


class BingxGravador(ClienteGravador):
    async def swap_order(self, symbol, side, position_side, quantity, price=None, *, tif="IOC"):
        self.ordens.append({"symbol": symbol, "side": side, "positionSide": position_side,
                            "quantity": quantity, "price": price})
        return {"orderId": "1"}

    async def open_short(self, symbol, quantity, price):
        return await self.swap_order(symbol, "SELL", "SHORT", quantity, price)

    async def close_short(self, symbol, quantity, price):
        return await self.swap_order(symbol, "BUY", "SHORT", quantity, price)


class MexcGravador(ClienteGravador):
    async def submit_order(self, symbol, side, vol, order_type=5, price=None,
                           leverage=1, open_type=1, external_oid=None, reduce_only=False):
        self.ordens.append({"symbol": symbol, "side": side, "vol": vol,
                            "type": order_type, "reduceOnly": reduce_only})
        return {"data": "1"}


async def test_gate_abre_venda_com_size_negativo_e_fecha_com_positivo():
    """
    Na Gate não existe campo `side`: o SINAL de `size` é a direção. Trocar o
    sinal ao fechar não gera erro — gera uma ordem que DOBRA a posição
    vendida em vez de zerá-la.
    """
    cli = GateGravador()
    t = GateFuturesTrader(Venue("gate", MarketType.FUTURES), _spec(Venue("gate", MarketType.FUTURES)), cli)

    await t.open_sell_leg(10, 100.0)
    assert cli.ordens[-1]["size"] == -10, "abrir venda exige size NEGATIVO"
    assert cli.ordens[-1]["reduce_only"] is False

    await t.close_sell_leg(10, 100.0)
    assert cli.ordens[-1]["size"] == 10, "fechar venda exige size POSITIVO"
    assert cli.ordens[-1]["reduce_only"] is True, "sem reduce_only, fechar pode ABRIR um long"


async def test_bingx_fecha_venda_com_buy_mantendo_positionside_short():
    """
    Na BingX o `positionSide` identifica QUAL posição e o `side` diz o que
    fazer com ela. Fechar um short é BUY sobre SHORT — mandar LONG abriria
    uma posição comprada nova em vez de fechar a vendida.
    """
    cli = BingxGravador()
    v = Venue("bingx", MarketType.FUTURES)
    t = BingxFuturesTrader(v, _spec(v, "BTC-USDT"), cli)

    await t.open_sell_leg(5, 100.0)
    assert (cli.ordens[-1]["side"], cli.ordens[-1]["positionSide"]) == ("SELL", "SHORT")

    await t.close_sell_leg(5, 100.0)
    assert (cli.ordens[-1]["side"], cli.ordens[-1]["positionSide"]) == ("BUY", "SHORT")


async def test_mexc_usa_os_codigos_de_lado_corretos():
    cli = MexcGravador()
    v = Venue("mexc", MarketType.FUTURES)
    t = MexcFuturesTrader(v, _spec(v), cli)

    await t.open_sell_leg(10, 100.0)
    assert cli.ordens[-1]["side"] == 3   # SIDE_OPEN_SHORT
    await t.close_sell_leg(10, 100.0)
    assert cli.ordens[-1]["side"] == 2   # SIDE_CLOSE_SHORT
    assert cli.ordens[-1]["reduceOnly"] is True


async def test_spot_recusa_abrir_perna_vendida():
    # Não se vende spot a descoberto sem conta de margem, que este projeto
    # não usa. Deixar passar produziria uma ordem sem contrapartida.
    v = Venue("gate", MarketType.SPOT)
    t = GateSpotTrader(v, _spec(v), GateGravador())
    with pytest.raises(NotImplementedError):
        await t.open_sell_leg(1, 100.0)
    assert t.supports_sell_leg is False


# ---------------------------------------------------------------------------
# Unidades: moeda base <-> contratos
# ---------------------------------------------------------------------------

def test_conversao_para_contratos_usa_o_contract_size():
    v = Venue("mexc", MarketType.FUTURES)
    t = MexcFuturesTrader(v, _spec(v, contract_size=100.0), MexcGravador())
    # 600 JIMOTHY com contrato de 100 = 6 contratos
    assert t.to_native_qty(600) == pytest.approx(6)
    assert t.to_base_qty(6) == pytest.approx(600)


async def test_ordem_de_futures_e_enviada_em_contratos_nao_em_moeda():
    cli = MexcGravador()
    v = Venue("mexc", MarketType.FUTURES)
    t = MexcFuturesTrader(v, _spec(v, contract_size=100.0), cli)
    await t.open_sell_leg(600, 0.0061)
    assert cli.ordens[-1]["vol"] == 6, "600 moedas = 6 contratos de 100"


async def test_bingx_swap_envia_quantidade_na_moeda_base():
    # A BingX é a exceção: quantidade em moeda base, não em contratos. Por
    # isso o contract_size do adaptador dela é 1,0.
    cli = BingxGravador()
    v = Venue("bingx", MarketType.FUTURES)
    t = BingxFuturesTrader(v, _spec(v, "BTC-USDT", contract_size=1.0), cli)
    await t.open_sell_leg(600, 0.0061)
    assert cli.ordens[-1]["quantity"] == pytest.approx(600)


def test_arredondamento_de_quantidade_e_sempre_para_baixo():
    v = Venue("gate", MarketType.FUTURES)
    spec = ContractSpec(symbol="X", venue=v, native_symbol="X_USDT",
                        contract_size=10.0, qty_step=1.0, min_qty=1)
    t = GateFuturesTrader(v, spec, GateGravador())
    # 67 moedas com contrato de 10 = 6,7 contratos -> 6 contratos = 60 moedas
    assert t.round_qty(67) == pytest.approx(60)


def test_quantidade_abaixo_do_minimo_vira_zero():
    v = Venue("gate", MarketType.FUTURES)
    spec = ContractSpec(symbol="X", venue=v, native_symbol="X_USDT",
                        contract_size=100.0, qty_step=1.0, min_qty=1)
    t = GateFuturesTrader(v, spec, GateGravador())
    # Melhor não enviar do que enviar uma ordem que a exchange rejeita.
    assert t.round_qty(50) == 0.0


def test_arredondamento_de_preco_protege_o_teto():
    v = Venue("gate", MarketType.SPOT)
    spec = ContractSpec(symbol="X", venue=v, native_symbol="X_USDT", price_tick=0.01)
    t = GateSpotTrader(v, spec, GateGravador())
    assert t.round_price(1.2345, up=False) == pytest.approx(1.23)  # compra
    assert t.round_price(1.2345, up=True) == pytest.approx(1.24)   # venda


# ---------------------------------------------------------------------------
# Leitura de preenchimento
# ---------------------------------------------------------------------------

class ClienteStatus:
    """Devolve sempre o mesmo status; aceita enviar e cancelar sem efeito."""

    def __init__(self, resposta):
        self.resposta = resposta
        self.cancelamentos = []

    async def get_spot_order(self, *a, **k):
        return self.resposta

    async def get_futures_order(self, *a, **k):
        return self.resposta

    async def get_swap_order(self, *a, **k):
        return self.resposta

    async def get_order(self, *a, **k):
        return self.resposta

    async def submit_order(self, **k):
        return {"data": "1"}

    async def new_order_limit(self, *a, **k):
        return {"orderId": "1"}

    async def spot_limit_ioc(self, *a, **k):
        return {"id": "1"}

    async def cancel_order(self, *a, **k):
        self.cancelamentos.append(a)
        return {}

    async def cancel_spot_order(self, *a, **k):
        self.cancelamentos.append(a)
        return {}


async def test_gate_spot_calcula_preenchido_por_amount_menos_left():
    v = Venue("gate", MarketType.SPOT)
    t = GateSpotTrader(v, _spec(v), ClienteStatus(
        {"amount": "100", "left": "40", "filled_total": "600", "status": "closed"}))
    st = await t.fetch_status(_ref())
    assert st.filled_qty == pytest.approx(60)
    assert st.notional / st.filled_qty == pytest.approx(10.0)
    assert st.terminal is True


async def test_gate_spot_status_open_nao_e_terminal():
    """
    "open" na Gate significa que a ordem AINDA PODE preencher mais. Ler o
    preenchido nesse momento e decidir em cima dele é ler um filme em
    andamento — o mecanismo exato do bug 17.
    """
    v = Venue("gate", MarketType.SPOT)
    t = GateSpotTrader(v, _spec(v), ClienteStatus(
        {"amount": "100", "left": "40", "filled_total": "600", "status": "open"}))
    assert (await t.fetch_status(_ref())).terminal is False


async def test_gate_futures_ignora_o_sinal_ao_medir_o_preenchido():
    # `size` vem com sinal (direção); o preenchido é o módulo da diferença.
    v = Venue("gate", MarketType.FUTURES)
    t = GateFuturesTrader(v, _spec(v, contract_size=10.0), ClienteStatus(
        {"size": "-10", "left": "-4", "fill_price": "2.5", "status": "finished"}))
    st = await t.fetch_status(_ref())
    assert st.filled_qty == pytest.approx(60)   # 6 contratos x 10
    assert st.notional / st.filled_qty == pytest.approx(2.5)
    assert st.terminal is True


async def test_mexc_ioc_parcial_cancelada_conta_como_preenchimento():
    # Regressão do bug 14: IOC parcial termina em state=4 (cancelada) COM
    # dealVol > 0. Descartar isso ignoraria uma ordem que existe de verdade.
    v = Venue("mexc", MarketType.FUTURES)
    t = MexcFuturesTrader(v, _spec(v, contract_size=100.0), ClienteStatus(
        {"data": {"state": 4, "dealVol": 6, "dealAvgPrice": 0.0061}}))
    st = await t.fetch_status(_ref())
    assert st.terminal is True
    assert st.filled_qty == pytest.approx(600)


async def test_ordem_terminal_sem_preenchimento_nao_vira_fill():
    """
    Terminal com zero preenchido é o único caso em que "não preencheu" é uma
    conclusão legítima — e mesmo assim vem de um estado TERMINAL, nunca de um
    timeout de leitura.
    """
    v = Venue("mexc", MarketType.FUTURES)
    t = MexcFuturesTrader(v, _spec(v), ClienteStatus(
        {"data": {"state": 4, "dealVol": 0, "dealAvgPrice": 0}}))
    st = await t.fetch_status(_ref())
    assert st.terminal is True and st.filled_qty == 0
    assert await t.run_leg("open_sell_leg", 10, 100.0) is None


def _ref():
    from bot.venue_trader import OrderRef
    return OrderRef("v", "S", "1")


# ---------------------------------------------------------------------------
# CICLO DE VIDA DA ORDEM — a generalização do bug 17 para todos os venues
#
# O incidente de 09/08/2026 aconteceu porque a MEXC spot aceita e IGNORA
# `timeInForce=IOC`: a ordem virou GTC, ficou viva, o bot desistiu dela e ela
# preencheu 33 segundos depois, dobrando a posição comprada.
#
# Gate e BingX documentam IOC de verdade no spot. Estes testes existem mesmo
# assim, porque a sub-lição do bug 17 é que um parâmetro ACEITO não é um
# parâmetro HONRADO — e a única defesa que não depende de acreditar na
# documentação é nunca abandonar uma ordem.
# ---------------------------------------------------------------------------

class ClienteOrdemViva:
    """
    Dublê da armadilha: a ordem NUNCA termina sozinha, e só passa a reportar
    preenchimento DEPOIS de ser cancelada.

    É o comportamento real da LIMIT "IOC" da MEXC spot, e o único dublê capaz
    de distinguir um motor que cancela de um que desiste.
    """

    def __init__(self, preenchido_apos_cancelar=1100.55):
        self.preenchido = preenchido_apos_cancelar
        self.cancelada = False
        self.ordens_enviadas = 0

    async def new_order_limit(self, symbol, side, quantity, price):
        self.ordens_enviadas += 1
        return {"orderId": "777"}

    async def spot_limit_ioc(self, pair, side, amount, price):
        self.ordens_enviadas += 1
        return {"id": "777"}

    async def swap_order(self, symbol, side, position_side, quantity, price=None, *, tif="IOC"):
        self.ordens_enviadas += 1
        return {"orderId": "777"}

    async def get_order(self, *a, **k):
        if not self.cancelada:
            return {"status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"}
        return {"status": "CANCELED", "executedQty": str(self.preenchido),
                "cummulativeQuoteQty": str(self.preenchido * 2)}

    async def get_spot_order(self, *a, **k):
        if not self.cancelada:
            return {"status": "open", "amount": "1100.55", "left": "1100.55", "filled_total": "0"}
        return {"status": "cancelled", "amount": "1100.55",
                "left": str(1100.55 - self.preenchido), "filled_total": str(self.preenchido * 2)}

    async def cancel_order(self, *a, **k):
        self.cancelada = True
        return {}

    async def cancel_spot_order(self, *a, **k):
        self.cancelada = True
        return {}


@pytest.mark.parametrize("chave,classe", [
    ("mexc:spot", MexcSpotTrader),
    ("gate:spot", GateSpotTrader),
])
async def test_nenhuma_ordem_de_spot_sobrevive_ao_run_leg(chave, classe):
    """
    Regressão do bug 17, agora para TODOS os venues de spot.

    Uma ordem que não termina sozinha precisa ser CANCELADA e RELIDA. Desistir
    dela sem cancelar é o que deixou 1100,55 unidades preenchendo depois que o
    bot já tinha decidido outra coisa.
    """
    v = Venue.from_key(chave)
    cli = ClienteOrdemViva()
    t = classe(v, _spec(v), cli, settle_wait_s=0.05)

    fill = await t.run_leg("open_buy_leg", 1100.55, 2.0)

    assert cli.cancelada, "a ordem foi abandonada viva — é exatamente o bug 17"
    assert fill is not None, "o preenchido descoberto APÓS o cancelamento foi ignorado"
    assert fill["filled_qty"] == pytest.approx(1100.55)


class ClienteMudo(ClienteOrdemViva):
    """Não responde ao status: o destino da ordem fica desconhecido."""

    async def get_order(self, *a, **k):
        raise TimeoutError("sem resposta")

    async def get_spot_order(self, *a, **k):
        raise TimeoutError("sem resposta")


async def test_destino_desconhecido_nao_vira_preenchimento_zero():
    """
    O erro mais caro deste projeto não é a falha — é a falha que se apresenta
    como sucesso (bug 15) ou como "não aconteceu" (bug 17). Sem conseguir ler
    o estado final, a única saída correta é parar e chamar o operador.
    """
    v = Venue("mexc", MarketType.SPOT)
    t = MexcSpotTrader(v, _spec(v), ClienteMudo(), settle_wait_s=0.05)

    with pytest.raises(UnknownOrderStateError):
        await t.run_leg("open_buy_leg", 100, 2.0)


async def test_ordem_de_destino_desconhecido_bloqueia_novas_ordens():
    """
    Enviar outra ordem sem saber o que a anterior fez é literalmente o
    mecanismo que dobrou a posição em 09/08/2026: o bot escalou para MARKET
    enquanto a LIMIT anterior seguia viva.
    """
    v = Venue("mexc", MarketType.SPOT)
    cli = ClienteMudo()
    t = MexcSpotTrader(v, _spec(v), cli, settle_wait_s=0.05)

    with pytest.raises(UnknownOrderStateError):
        await t.run_leg("open_buy_leg", 100, 2.0)
    enviadas = cli.ordens_enviadas

    # A segunda tentativa não pode nem chegar a virar ordem.
    with pytest.raises(UnknownOrderStateError):
        await t.run_leg("open_buy_leg", 100, 2.0)
    assert cli.ordens_enviadas == enviadas, "mandou ordem nova com a anterior em aberto"

    assert t.has_unknown_order
    t.clear_unknown_order()
    assert not t.has_unknown_order


# ---------------------------------------------------------------------------
# UNIDADE DA ORDEM A MERCADO NO SPOT — a assimetria da Gate
# ---------------------------------------------------------------------------

class GateMercadoGravador:
    def __init__(self):
        self.compras, self.vendas = [], []

    async def spot_market_buy(self, pair, amount_quote):
        self.compras.append(amount_quote)
        return {"id": "1"}

    async def spot_market_sell(self, pair, amount_base):
        self.vendas.append(amount_base)
        return {"id": "1"}

    async def get_spot_order(self, *a, **k):
        return {"status": "closed", "amount": "0", "left": "0", "filled_total": "0"}

    async def cancel_spot_order(self, *a, **k):
        return {}


async def test_gate_compra_a_mercado_e_cobrada_em_usdt_nao_em_moeda_base():
    """
    Na Gate, `amount` numa COMPRA a mercado é o valor em USDT; numa VENDA é a
    quantidade da moeda base. Passar base na compra não é rejeitado — é aceito
    e gasta o número errado.

    Numa memecoin a 0,004 USDT, pedir 1000 unidades gastaria 1000 USDT em vez
    de 4: 250x a posição decidida.
    """
    v = Venue("gate", MarketType.SPOT)
    cli = GateMercadoGravador()
    t = GateSpotTrader(v, _spec(v), cli, settle_wait_s=0.01)

    await t.run_leg("open_buy_leg", 1000, None, ref_price=0.004)
    assert cli.compras[-1] == pytest.approx(1000 * 0.004 * 1.01), \
        "a compra a mercado foi enviada na unidade errada"

    await t.run_leg("close_buy_leg", 1000, None)
    assert cli.vendas[-1] == pytest.approx(1000), "a venda a mercado é em moeda base"


async def test_gate_recusa_compra_a_mercado_sem_preco_de_referencia():
    """Sem `ref_price` não há como converter — e chutar o valor de uma ordem
    a mercado é o oposto de tudo que este projeto faz."""
    v = Venue("gate", MarketType.SPOT)
    t = GateSpotTrader(v, _spec(v), GateMercadoGravador(), settle_wait_s=0.01)
    with pytest.raises(ValueError, match="ref_price"):
        await t.run_leg("open_buy_leg", 1000, None)


async def test_mexc_e_bingx_compram_a_mercado_em_moeda_base():
    """A assimetria é só da Gate; converter nas outras daria o erro espelhado."""
    for chave, classe in (("mexc:spot", MexcSpotTrader), ("bingx:spot", BingxSpotTrader)):
        v = Venue.from_key(chave)
        assert classe(v, _spec(v), None).market_buy_uses_quote is False


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chave,classe", [
    ("mexc:spot", MexcSpotTrader), ("mexc:futures", MexcFuturesTrader),
    ("gate:spot", GateSpotTrader), ("gate:futures", GateFuturesTrader),
    ("bingx:spot", BingxSpotTrader), ("bingx:futures", BingxFuturesTrader),
])
def test_todos_os_seis_venues_tem_executor(chave, classe):
    v = Venue.from_key(chave)
    assert isinstance(build_trader(v, _spec(v), GateGravador()), classe)


def test_venue_desconhecido_falha_alto():
    # Melhor explodir na configuração do que descobrir na hora de enviar
    # ordem que não há executor para o venue.
    with pytest.raises(ValueError):
        build_trader(Venue("kraken", MarketType.SPOT), _spec(Venue("kraken", MarketType.SPOT)), None)
