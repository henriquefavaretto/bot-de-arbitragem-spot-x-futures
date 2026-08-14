"""
Testes da configuração de VENUE por par.

Estes testes cobrem o portão que decide ONDE o bot opera. Uma configuração
inválida aceita aqui vira ordem enviada no lugar errado depois — e o pior
caso não é uma ordem rejeitada, é uma perna aberta numa exchange e a outra
tentada em outra.
"""
import pytest

from bot.bot_engine import ExecutionMode, PairState
from conftest import FakeMarketClient, futures_depth_payload, make_engine, spot_depth_payload


def books_bons():
    return (
        FakeMarketClient(spot_depth_payload(bids=[(0.999, 1e6)], asks=[(1.00, 1e6)]), kind="spot"),
        FakeMarketClient(futures_depth_payload(bids=[(1.02, 1e6)], asks=[(1.021, 1e6)]), kind="futures"),
    )


async def test_par_guarda_o_venue_escolhido():
    ms, mf = books_bons()
    engine, storage = make_engine(contract_size=1, market_spot=ms, market_futures=mf)
    await engine.set_pair_config(
        "JIMOTHY", True, 1.5, 0.5, 100.0, "mexc:spot", "mexc:futures",
    )
    cfg = engine.configs["JIMOTHY"]
    assert cfg.buy_venue == "mexc:spot"
    assert cfg.sell_venue == "mexc:futures"
    assert cfg.cross_exchange is False
    # E precisa ter sido PERSISTIDO: um reinício não pode mudar o venue de um
    # par sem ninguém ter pedido.
    assert storage.configs["JIMOTHY"]["buy_venue"] == "mexc:spot"


async def test_spot_contra_spot_e_recusado():
    # Sem instrumento vendido a descoberto dos dois lados não existe posição
    # neutra a montar — seria só comprar em dois lugares.
    engine, _ = make_engine(contract_size=1)
    with pytest.raises(ValueError, match="dois mercados spot"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "mexc:spot", "gate:spot")


async def test_mesmo_venue_dos_dois_lados_e_recusado():
    # Dois futures IGUAIS: isola esta regra da de spot-contra-spot, que
    # dispararia antes se o teste usasse dois spots.
    engine, _ = make_engine(contract_size=1)
    with pytest.raises(ValueError, match="venues diferentes"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "mexc:futures", "mexc:futures")


async def test_venue_desconhecido_e_recusado():
    engine, _ = make_engine(contract_size=1)
    with pytest.raises(ValueError, match="Venue desconhecido"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "kraken:spot", "mexc:futures")


async def test_cross_exchange_bloqueado_por_padrao():
    """
    Operar entre exchanges exige saldo pré-posicionado nos dois lados e, se
    uma perna falhar, NÃO dá para reverter na mesma conta. Fica desligado
    até o caminho de mesma-exchange estar validado.
    """
    engine, _ = make_engine(contract_size=1)
    with pytest.raises(ValueError, match="entre exchanges diferentes"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "mexc:spot", "gate:futures")


async def test_cross_exchange_ainda_barra_na_execucao_mesmo_se_habilitado():
    """
    `allow_cross_exchange` libera a regra de negocio, mas a trava de EXECUCAO
    e independente e vem depois: enquanto o envio de ordem for MEXC-only, um
    par com perna na Gate seria decidido com o book da Gate e mandado para a
    MEXC.
    """
    engine, _ = make_engine(contract_size=1, allow_cross_exchange=True)
    with pytest.raises(ValueError, match="execução de gate:futures"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "mexc:spot", "gate:futures")


async def test_venue_sem_execucao_implementada_e_recusado():
    """
    A recusa acontece AQUI, ao configurar — nunca no meio da operação com uma
    perna já aberta.

    Este e o portao mais importante do arquivo: a DECISAO ja sabe ler o book
    de qualquer venue, mas o ENVIO DE ORDEM ainda usa os clientes da MEXC.
    Deixar configurar um par na Gate faria o bot decidir com o book de uma
    exchange e executar em outra.
    """
    engine, _ = make_engine(contract_size=1)
    with pytest.raises(ValueError, match="execução de gate:spot"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "gate:spot", "gate:futures")


async def test_a_mensagem_aponta_o_caminho_da_correcao():
    # Um erro que so diz "nao pode" faz o usuario achar que e limitacao da
    # exchange. Dizer ONDE esta a limitacao, e o que fazer a respeito, evita
    # isso -- aqui: validar o venue e listá-lo em BOT_VALIDATED_VENUES.
    engine, _ = make_engine(contract_size=1)
    with pytest.raises(ValueError, match="validate_venue"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "bingx:spot", "bingx:futures")
    with pytest.raises(ValueError, match="BOT_VALIDATED_VENUES"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "bingx:spot", "bingx:futures")


async def test_venue_validado_por_env_passa_a_ser_configuravel():
    """
    A trava é sobre VALIDAÇÃO, não sobre a exchange: uma vez que o usuário
    valide o venue contra a API real e o declare no .env, a configuração passa
    a ser aceita — sem tocar em código.
    """
    engine, _ = make_engine(
        contract_size=1,
        validated_venues=frozenset({"mexc:spot", "mexc:futures", "gate:spot", "gate:futures"}),
    )
    await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "gate:spot", "gate:futures")
    assert engine.configs["X"].buy_venue == "gate:spot"


async def test_simulacao_tambem_recusa_venue_sem_execucao():
    """
    A trava vale em SIMULACAO tambem, de proposito.

    Uma simulacao que finge operar um venue cuja execucao nao existe valida
    uma estrategia que nao pode ser executada -- e da confianca falsa
    justamente para o passo seguinte, que e ligar o modo real.
    """
    engine, _ = make_engine(contract_size=1, mode=ExecutionMode.SIMULATION)
    with pytest.raises(ValueError, match="execução de bingx"):
        await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "bingx:spot", "bingx:futures")


async def test_book_vem_dos_venues_configurados_e_nao_da_mexc():
    """
    O motor precisa ler profundidade do venue ESCOLHIDO. Ler sempre da MEXC
    faria o bot decidir com o book de uma exchange e executar em outra — um
    spread inteiramente fictício.
    """
    ms, mf = books_bons()
    engine, _ = make_engine(contract_size=1, market_spot=ms, market_futures=mf)

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert ms.calls > 0 and mf.calls > 0, "leu os books dos venues configurados"
    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN


async def test_snapshot_expoe_o_venue_de_cada_par():
    # Sem isso a interface nao tem como mostrar onde cada par opera -- e o
    # usuario nao teria como conferir se configurou o que queria.
    engine, _ = make_engine(contract_size=1)
    await engine.set_pair_config("X", True, 1.5, 0.5, 100.0, "mexc:spot", "mexc:futures")
    cfg = next(p for p in engine.get_snapshot() if p["symbol"] == "X")["config"]
    assert cfg["buy_venue"] == "mexc:spot"
    assert cfg["sell_venue"] == "mexc:futures"
    assert cfg["cross_exchange"] is False


async def test_sem_adaptador_do_venue_a_decisao_e_adiada():
    # Preferir não fazer nada a operar sem book — a regra de ouro do projeto.
    engine, storage = make_engine(contract_size=1, market_spot=None, market_futures=None)
    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )
    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE
    assert storage.events == []


async def test_nenhuma_ordem_sai_para_venue_sem_execucao_implementada():
    """
    Defesa em profundidade: mesmo que uma config de venue nao-MEXC entre por
    outro caminho (banco antigo, edicao manual, bug futuro), NENHUMA ordem
    pode sair.

    Sem esta trava, um par Gate x Gate decide com o book da Gate e manda a
    ordem para os clientes da MEXC -- comprando e vendendo instrumentos sem
    relacao nenhuma entre si. E o pior desfecho possivel desta camada.
    """
    from conftest import FakeFuturesClient, FakeSpotClient
    from bot.bot_engine import ExecutionMode

    ms, mf = books_bons()
    spot, futures = FakeSpotClient(), FakeFuturesClient()
    engine, _ = make_engine(
        contract_size=1, mode=ExecutionMode.LIVE,
        spot_client=spot, futures_client=futures,
        market_spot=ms, market_futures=mf, spot_limit_wait_s=0,
    )
    # Contorna a validacao de configuracao de proposito, simulando uma config
    # que chegou por outro caminho.
    engine.configs["JIMOTHY"].buy_venue = "gate:spot"
    engine.configs["JIMOTHY"].sell_venue = "gate:futures"

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert spot.orders == [], "nenhuma ordem de spot pode sair para venue sem execucao"
    assert futures.orders == [], "nenhuma ordem de futures pode sair para venue sem execucao"
    assert engine.runtimes["JIMOTHY"].state == PairState.IDLE


# ---------------------------------------------------------------------------
# DA DECISAO ATE A ORDEM ENVIADA
#
# A licao do bug 18: quando uma capacidade e adicionada em camadas, a camada
# de DECISAO e a de EXECUCAO podem divergir sem nenhum erro aparecer -- os
# testes de decisao passam, os de execucao passam, e a combinacao esta errada.
# Por isso este bloco nao testa nenhuma das duas isoladamente: ele configura um
# par na Gate e verifica em QUAL CLIENTE a ordem aterrissou.
# ---------------------------------------------------------------------------

class GateFake:
    """Dublê da Gate que registra tudo que recebe, spot e futures."""

    def __init__(self):
        self.spot_orders, self.futures_orders = [], []

    # -- spot --
    async def spot_limit_ioc(self, pair, side, amount, price):
        self.spot_orders.append({"pair": pair, "side": side, "amount": amount, "price": price})
        return {"id": str(len(self.spot_orders))}

    async def spot_market_buy(self, pair, amount_quote):
        self.spot_orders.append({"pair": pair, "side": "buy", "market_quote": amount_quote})
        return {"id": str(len(self.spot_orders))}

    async def spot_market_sell(self, pair, amount_base):
        self.spot_orders.append({"pair": pair, "side": "sell", "market_base": amount_base})
        return {"id": str(len(self.spot_orders))}

    async def get_spot_order(self, order_id, pair):
        o = self.spot_orders[int(order_id) - 1]
        qtd = o.get("amount") or o.get("market_base") or 0
        return {"status": "closed", "amount": str(qtd), "left": "0",
                "filled_total": str(float(qtd) * 1.0)}

    async def cancel_spot_order(self, order_id, pair):
        return {}

    async def get_spot_balance(self, asset):
        return {"free": 1e9, "locked": 0.0}

    async def cancel_all_spot(self, pair):
        return {}

    # -- futures --
    async def futures_order(self, contract, size, price, *, reduce_only=False, tif="ioc"):
        self.futures_orders.append({"contract": contract, "size": size, "price": price,
                                    "reduce_only": reduce_only})
        return {"id": str(len(self.futures_orders))}

    async def open_short(self, contract, contracts, price):
        return await self.futures_order(contract, -abs(int(contracts)), price)

    async def close_short(self, contract, contracts, price):
        return await self.futures_order(contract, abs(int(contracts)), price, reduce_only=True)

    async def get_futures_order(self, order_id):
        o = self.futures_orders[int(order_id) - 1]
        return {"status": "finished", "size": str(o["size"]), "left": "0", "fill_price": "1.02"}

    async def cancel_futures_order(self, order_id):
        return {}

    async def cancel_all_futures(self, contract):
        return {}


def _gate_specs(symbol="JIMOTHY"):
    from exchanges.base import ContractSpec as VenueSpec, Venue
    return {
        (symbol, "gate:spot"): VenueSpec(
            symbol=symbol, venue=Venue.from_key("gate:spot"),
            native_symbol=f"{symbol}_USDT", contract_size=1.0,
            qty_step=0.01, price_tick=1e-6, taker_fee_pct=0.2,
        ),
        (symbol, "gate:futures"): VenueSpec(
            symbol=symbol, venue=Venue.from_key("gate:futures"),
            native_symbol=f"{symbol}_USDT", contract_size=1.0,
            qty_step=1.0, price_tick=1e-6, min_qty=1, taker_fee_pct=0.075,
        ),
    }


async def test_taxa_usada_e_a_do_venue_configurado_nao_a_da_mexc():
    """
    A Gate cobra 0,075% de taker no futures contra 0,02% da MEXC -- 3,75x.

    Numa estrategia cuja margem vive entre 1% e 3%, quatro pernas com a taxa
    errada mudam o SINAL do resultado esperado: o bot entraria convicto numa
    operacao que perde dinheiro por construcao. Por isso a taxa vem do venue
    configurado, nunca de um padrao.
    """
    engine, _ = make_engine(
        contract_size=1, futures_taker_pct=0.02, spot_taker_pct=0.05,
        buy_venue="gate:spot", sell_venue="gate:futures",
        validated_venues=frozenset({"gate:spot", "gate:futures"}),
    )
    # Antes de carregar os metadados do venue, cai nos da MEXC.
    assert engine.fees_for("JIMOTHY").futures_taker_pct == pytest.approx(0.02)

    engine.venue_specs.update(_gate_specs())
    taxas = engine.fees_for("JIMOTHY")
    assert taxas.futures_taker_pct == pytest.approx(0.075), "usou a taxa da MEXC num par da Gate"
    assert taxas.spot_taker_pct == pytest.approx(0.2)


async def test_par_na_gate_manda_a_ordem_para_a_gate_e_nao_para_a_mexc():
    """
    O teste que o bug 18 pedia e nao existia.

    Antes, `fetch_books` lia o book da Gate e `_spot_send` mandava a ordem para
    a MEXC. Nenhum teste pegava isso porque cada camada, isolada, estava certa.
    Aqui a assercao e sobre a COMBINACAO: decidiu com a Gate, executou na Gate.
    """
    from conftest import FakeFuturesClient, FakeSpotClient

    ms, mf = books_bons()
    mexc_spot, mexc_futures = FakeSpotClient(), FakeFuturesClient()
    gate = GateFake()
    validados = frozenset({"mexc:spot", "mexc:futures", "gate:spot", "gate:futures"})

    engine, _ = make_engine(
        contract_size=1, mode=ExecutionMode.LIVE,
        spot_client=mexc_spot, futures_client=mexc_futures,
        market_spot=ms, market_futures=mf, spot_limit_wait_s=0,
        buy_venue="gate:spot", sell_venue="gate:futures",
        validated_venues=validados,
        venue_clients={
            "mexc:spot": mexc_spot, "mexc:futures": mexc_futures,
            "gate:spot": gate, "gate:futures": gate,
            "bingx:spot": None, "bingx:futures": None,
        },
    )
    engine.venue_specs.update(_gate_specs())

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert gate.futures_orders, "a perna vendida nao chegou na Gate"
    assert gate.spot_orders, "a perna comprada nao chegou na Gate"
    assert mexc_spot.orders == [], "VAZOU ordem de spot para a MEXC num par configurado na Gate"
    assert mexc_futures.orders == [], "VAZOU ordem de futures para a MEXC num par configurado na Gate"

    # E no dialeto da Gate: abrir venda exige size NEGATIVO (nao existe `side`).
    assert gate.futures_orders[0]["size"] < 0, "abrir venda na Gate exige size negativo"
    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN


async def test_saida_de_par_na_gate_tambem_vai_para_a_gate():
    """
    A saida e o caminho em que errar o venue e IRREVERSIVEL: a posicao ja
    existe, e fechar no lugar errado ABRE uma posicao nova em vez de zerar a
    que existe.
    """
    from conftest import FakeFuturesClient, FakeSpotClient

    mexc_spot, mexc_futures = FakeSpotClient(), FakeFuturesClient()
    gate = GateFake()

    # Dois books em sequencia: o primeiro abre a oportunidade, o segundo ja
    # convergiu (futures_ask praticamente colado no spot_bid), que e a
    # condicao de saida.
    ms = FakeMarketClient(spot_depth_payload(bids=[(0.999, 1e6)], asks=[(1.00, 1e6)]), kind="spot")
    mf = FakeMarketClient([
        futures_depth_payload(bids=[(1.02, 1e6)], asks=[(1.021, 1e6)]),
        futures_depth_payload(bids=[(0.999, 1e6)], asks=[(1.0005, 1e6)]),
    ], kind="futures")

    engine, _ = make_engine(
        contract_size=1, mode=ExecutionMode.LIVE,
        spot_client=mexc_spot, futures_client=mexc_futures,
        market_spot=ms, market_futures=mf, spot_limit_wait_s=0,
        buy_venue="gate:spot", sell_venue="gate:futures",
        validated_venues=frozenset({"gate:spot", "gate:futures"}),
        venue_clients={"gate:spot": gate, "gate:futures": gate},
    )
    engine.venue_specs.update(_gate_specs())

    # Entra e depois sai, no mesmo par.
    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=2.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )
    assert engine.runtimes["JIMOTHY"].state == PairState.OPEN
    abertura = len(gate.futures_orders)

    await engine.on_price_update(
        "JIMOTHY", 1.00, 1.02, spread_pct=0.0, prices_from_book=True,
        exit_spread_pct=0.1, spot_bid=0.999, futures_ask=1.021,
        spot_book_age_s=0.1, futures_book_age_s=0.1,
    )

    assert len(gate.futures_orders) > abertura, "a saida nao chegou na Gate"
    fechamento = gate.futures_orders[-1]
    assert fechamento["size"] > 0, "fechar venda na Gate exige size POSITIVO"
    assert fechamento["reduce_only"] is True, "sem reduce_only, fechar pode ABRIR um long"
    assert mexc_futures.orders == [], "VAZOU ordem de saida para a MEXC"
