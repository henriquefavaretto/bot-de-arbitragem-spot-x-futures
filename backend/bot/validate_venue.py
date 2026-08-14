"""
Validação de um VENUE contra a API real, com dinheiro de verdade e o menor
valor possível.

## Por que este script existe

Os bugs 4, 5, 6, 14, 16 e 17 deste projeto foram TODOS comportamentos de API
que passaram pelos testes contra dublês e só apareceram na conta real:

    bug  4  a taxa é cobrada na moeda comprada, então o saldo vendável é menor
    bug  5  a quantidade precisa vir na precisão exata do símbolo
    bug  6  o POST responde `executedQty=0` numa ordem que executou
    bug 14  IOC parcial termina CANCELED com preenchimento real dentro
    bug 16  o endpoint aceita ~2 ordens a cada 2s e recusa o resto (erro 510)
    bug 17  a MEXC spot ACEITA e IGNORA `timeInForce=IOC`

Nenhum era descobrível lendo a documentação. Gate e BingX foram implementadas
A PARTIR DA DOCUMENTAÇÃO e nunca receberam uma ordem real — assumir que não
têm esquisitices equivalentes seria apostar contra todo o histórico deste
projeto. As três exchanges já divergem em coisas tão básicas quanto a unidade
de uma compra a mercado.

## O que ele faz

Só leitura por padrão. Com `--ordem`, envia UMA ordem mínima e a acompanha
pelo ciclo de vida completo (enviar -> ler -> cancelar -> reler), que é
exatamente o caminho que o bot usa. A ordem é LIMITE, longe do mercado, para
NÃO executar: o objetivo é validar assinatura, formato de símbolo, precisão,
leitura de status e cancelamento — não gastar dinheiro.

## Uso

    cd backend
    venv\\Scripts\\python.exe -m bot.validate_venue gate:futures --symbol BTC
    venv\\Scripts\\python.exe -m bot.validate_venue gate:futures --symbol BTC --ordem

Passando em tudo, inclua o venue em `BOT_VALIDATED_VENUES` no `.env`:

    BOT_VALIDATED_VENUES=gate:spot,gate:futures

Enquanto não estiver lá, o bot RECUSA operar naquele venue — na configuração,
na entrada e na saída.
"""
import argparse
import asyncio
import logging
import os
import sys

import httpx
from dotenv import load_dotenv

from bot.bingx_client import BingxClient
from bot.gate_client import GateClient
from bot.mexc_futures_client import MexcFuturesClient
from bot.mexc_spot_client import MexcSpotClient
from bot.venue_trader import build_trader
from exchanges.base import MarketType, Venue
from exchanges.registry import build_adapters

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("validate_venue")

OK, FALHA, AVISO = "  [OK]  ", "  [FALHA]", "  [AVISO]"


def _cliente_para(venue_key: str, http):
    """Cliente autenticado do venue, ou None se faltar credencial."""
    exchange = venue_key.split(":")[0]
    if exchange == "gate":
        k, s = os.getenv("GATE_API_KEY", ""), os.getenv("GATE_SECRET_KEY", "")
        return GateClient(k, s, http) if k and s else None
    if exchange == "bingx":
        k, s = os.getenv("BINGX_API_KEY", ""), os.getenv("BINGX_SECRET_KEY", "")
        return BingxClient(k, s, http) if k and s else None
    if venue_key == "mexc:spot":
        k, s = os.getenv("MEXC_SPOT_API_KEY", ""), os.getenv("MEXC_SPOT_SECRET_KEY", "")
        return MexcSpotClient(k, s, http) if k and s else None
    if venue_key == "mexc:futures":
        k, s = os.getenv("MEXC_FUTURES_API_KEY", ""), os.getenv("MEXC_FUTURES_SECRET_KEY", "")
        return MexcFuturesClient(k, s, http) if k and s else None
    return None


async def validar(venue_key: str, symbol: str, enviar_ordem: bool) -> bool:
    venue = Venue.from_key(venue_key)
    load_dotenv()
    falhas = []

    async with httpx.AsyncClient() as http:
        print(f"\n=== Validando {venue_key} com {symbol} ===\n")

        # --- 1. Credenciais ---
        cliente = _cliente_para(venue_key, http)
        if cliente is None:
            print(f"{FALHA} Sem credenciais no .env para {venue.exchange}.")
            print("         Preencha as chaves e rode de novo.")
            return False
        print(f"{OK} Credenciais encontradas para {venue.exchange}.")

        # --- 2. Metadados: formato de símbolo, contractSize, tick, passo ---
        #
        # É aqui que o bug 2 (símbolo com formato divergente) e o bug 5
        # (precisão da quantidade) seriam pegos antes de virarem ordem.
        adaptadores = build_adapters(http)
        adaptador = adaptadores.get(venue_key)
        if adaptador is None:
            print(f"{FALHA} Sem adaptador de mercado para {venue_key}.")
            return False
        try:
            specs = await adaptador.fetch_specs([symbol])
        except Exception as e:
            print(f"{FALHA} Não foi possível ler metadados: {e}")
            return False
        spec = specs.get(symbol)
        if spec is None:
            print(f"{FALHA} {symbol} não existe em {venue_key} (ou não é par USDT).")
            return False
        print(f"{OK} Símbolo nativo: {spec.native_symbol}")
        print(f"        contract_size={spec.contract_size:g}  qty_step={spec.qty_step:g}  "
              f"price_tick={spec.price_tick:g}  taker={spec.taker_fee_pct:g}%")
        if spec.contract_size <= 0:
            falhas.append("contract_size inválido — a conversão de quantidade sairia errada")
        if spec.price_tick <= 0:
            print(f"{AVISO} price_tick zerado: o preço-limite não será alinhado ao tick.")

        # --- 3. Book: confirma que a leitura de profundidade funciona ---
        try:
            book = await adaptador.fetch_depth(symbol, 5)
        except Exception as e:
            print(f"{FALHA} Falha ao ler profundidade: {e}")
            return False
        if book is None or not book.is_usable:
            print(f"{FALHA} Book inutilizável para {symbol} em {venue_key}.")
            return False
        print(f"{OK} Book: bid={book.best_bid:.10g} ask={book.best_ask:.10g} "
              f"(largura {(book.best_ask - book.best_bid) / book.best_bid * 100:.3f}%)")

        trader = build_trader(venue, spec, cliente)

        # --- 4. Saldo (endpoint autenticado de LEITURA) ---
        #
        # Valida a ASSINATURA sem mover dinheiro. Se a assinatura estiver
        # errada, é aqui que aparece — e não com uma ordem no ar.
        ativo = "USDT" if venue.market == MarketType.FUTURES else symbol
        try:
            saldo = await trader.free_balance(ativo)
            print(f"{OK} Assinatura aceita. Saldo livre de {ativo}: {saldo}")
        except Exception as e:
            print(f"{FALHA} Endpoint autenticado recusou: {e}")
            print("         Cheque a chave, o secret, as permissões e o IP na whitelist.")
            return False

        # --- 5. Posições abertas ---
        try:
            print(f"{OK} Posição: {await trader.describe_position()}")
        except Exception as e:
            print(f"{AVISO} Não foi possível ler posições: {e}")

        if not enviar_ordem:
            print("\nLeitura validada. Rode de novo com --ordem para validar o ciclo de vida "
                  "de uma ordem real (mínima e propositalmente inexecutável).")
            return not falhas

        # --- 6. Ciclo de vida de uma ordem REAL ---
        #
        # O preço fica 30% ABAIXO do bid numa COMPRA: a ordem é aceita, entra
        # no book e não executa. Isso valida o caminho inteiro (envio,
        # precisão, leitura de status, cancelamento e releitura) sem custo.
        #
        # É o mesmo `run_leg` que o bot usa em produção — validar um caminho
        # diferente do de produção não validaria nada.
        preco = trader.round_price(book.best_bid * 0.7, up=False)
        qtd = trader.round_qty(max(spec.min_qty, 1) * spec.contract_size)
        if qtd <= 0:
            print(f"{FALHA} Quantidade mínima arredondou para zero — confira qty_step/min_qty.")
            return False

        print(f"\n  Enviando ordem de teste: COMPRAR {qtd:g} {symbol} @ {preco:.10g} "
              f"(~30% abaixo do mercado, NÃO deve executar)")
        try:
            fill = await trader.run_leg("open_buy_leg", qtd, preco)
        except Exception as e:
            print(f"{FALHA} O ciclo de vida da ordem falhou: {e}")
            print("         ATENÇÃO: confira MANUALMENTE se ficou ordem aberta na exchange.")
            return False

        if fill is None:
            print(f"{OK} Ordem enviada, lida, cancelada e relida — sem preenchimento, como esperado.")
            print(f"{OK} Ciclo de vida completo validado (nenhuma ordem sobreviveu).")
        else:
            print(f"{AVISO} A ordem PREENCHEU {fill['filled_qty']:g} (notional {fill['notional']:g}).")
            print("         Não era esperado a 30% do mercado. Confira o book e sua posição.")
            falhas.append("ordem de teste preencheu — confira manualmente")

    print()
    if falhas:
        for f in falhas:
            print(f"{FALHA} {f}")
        return False

    print(f"  VALIDADO: {venue_key}")
    print(f"  Para liberar a execução, acrescente ao .env:")
    print(f"      BOT_VALIDATED_VENUES={venue_key}")
    print("  (separe por vírgula se já houver outros)")
    return True


def main():
    p = argparse.ArgumentParser(description="Valida um venue contra a API real.")
    p.add_argument("venue", help="ex: gate:futures, bingx:spot, mexc:spot")
    p.add_argument("--symbol", default="BTC", help="símbolo canônico (padrão: BTC)")
    p.add_argument(
        "--ordem", action="store_true",
        help="envia UMA ordem limite mínima e propositalmente inexecutável, "
             "para validar o ciclo de vida completo",
    )
    args = p.parse_args()

    try:
        Venue.from_key(args.venue)
    except Exception:
        print(f"Venue inválido: {args.venue}")
        sys.exit(2)

    ok = asyncio.run(validar(args.venue, args.symbol.upper(), args.ordem))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
