"""
Script de validação das credenciais da MEXC (Spot e Futures).

ESTE SCRIPT NÃO ENVIA NENHUMA ORDEM. Só faz chamadas de leitura:
- Saldo da conta spot
- Ativos da conta futures
- Metadados de um contrato (público)
- Posições abertas em futures (deve vir vazio se você não tiver posição)

Rode com: python test_credentials.py
(a partir da pasta backend/, com o venv ativado e o .env preenchido)
"""
import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.mexc_spot_client import MexcSpotClient, MexcSpotAPIError, MexcSpotAuthError
from bot.mexc_futures_client import MexcFuturesClient, MexcFuturesAPIError, MexcFuturesAuthError

load_dotenv()


def mask(value: str) -> str:
    if not value or len(value) < 8:
        return "(não preenchida)"
    return value[:4] + "..." + value[-4:]


async def main():
    print("=" * 60)
    print("Validação de credenciais MEXC — SOMENTE LEITURA")
    print("Nenhuma ordem será enviada por este script.")
    print("=" * 60)

    spot_key = os.getenv("MEXC_SPOT_API_KEY", "")
    spot_secret = os.getenv("MEXC_SPOT_SECRET_KEY", "")
    fut_key = os.getenv("MEXC_FUTURES_API_KEY", "")
    fut_secret = os.getenv("MEXC_FUTURES_SECRET_KEY", "")

    print(f"\nSpot API Key:      {mask(spot_key)}")
    print(f"Futures API Key:   {mask(fut_key)}")

    if not all([spot_key, spot_secret, fut_key, fut_secret]):
        print("\n[ERRO] Preencha o arquivo .env (copie de .env.example) antes de rodar este teste.")
        return

    async with httpx.AsyncClient() as http_client:
        # ---------------- SPOT ----------------
        print("\n--- SPOT ---")
        try:
            spot_client = MexcSpotClient(spot_key, spot_secret, http_client)
            account = await spot_client.get_account()
            can_trade = account.get("canTrade")
            print(f"[OK] Conta spot acessada. canTrade={can_trade} canWithdraw={account.get('canWithdraw')}")
            if account.get("canWithdraw"):
                print("[AVISO] Esta chave TEM permissão de saque habilitada! "
                      "Recomendo desabilitar saque na MEXC por segurança.")

            usdt_balance = await spot_client.get_balance("USDT")
            print(f"[OK] Saldo USDT (spot): free={usdt_balance['free']} locked={usdt_balance['locked']}")
        except (MexcSpotAuthError, MexcSpotAPIError) as e:
            print(f"[FALHA] {e}")
        except httpx.RequestError as e:
            print(f"[FALHA] Erro de conexão ao acessar api.mexc.com: {e}")
            print("        Verifique sua internet/firewall — o bot precisa acessar api.mexc.com diretamente.")
        except Exception as e:
            print(f"[FALHA] Resposta inesperada da MEXC (spot): {e}")

        # ---------------- FUTURES ----------------
        print("\n--- FUTURES ---")
        try:
            futures_client = MexcFuturesClient(fut_key, fut_secret, http_client)

            contract = await futures_client.get_contract_detail("BTC_USDT")
            if contract.get("success"):
                data = contract["data"]
                if isinstance(data, list):
                    data = data[0] if data else {}
                print(f"[OK] Metadados do contrato BTC_USDT: contractSize={data.get('contractSize')} "
                      f"minVol={data.get('minVol')} maxLeverage={data.get('maxLeverage')}")
            else:
                print(f"[AVISO] Não foi possível obter metadados do contrato: {contract}")

            assets = await futures_client.get_assets()
            if assets.get("success"):
                usdt_assets = [a for a in assets.get("data", []) if a.get("currency") == "USDT"]
                if usdt_assets:
                    a = usdt_assets[0]
                    print(f"[OK] Saldo USDT (futures): availableBalance={a.get('availableBalance')} "
                          f"positionMargin={a.get('positionMargin')}")
                else:
                    print("[OK] Conta futures acessada, sem saldo USDT listado (ok se você nunca depositou lá).")
            else:
                print(f"[FALHA] Resposta inesperada ao buscar ativos futures: {assets}")

            positions = await futures_client.get_open_positions()
            if positions.get("success"):
                pos_list = positions.get("data", [])
                print(f"[OK] Posições abertas em futures: {len(pos_list)}")
                for p in pos_list:
                    print(f"      - {p.get('symbol')} side={p.get('positionType')} vol={p.get('holdVol')}")
            else:
                print(f"[AVISO] Não foi possível listar posições: {positions}")

        except (MexcFuturesAuthError, MexcFuturesAPIError) as e:
            print(f"[FALHA] {e}")
        except httpx.RequestError as e:
            print(f"[FALHA] Erro de conexão ao acessar api.mexc.com: {e}")
            print("        Verifique sua internet/firewall — o bot precisa acessar api.mexc.com diretamente.")
        except Exception as e:
            print(f"[FALHA] Resposta inesperada da MEXC (futures): {e}")

    print("\n" + "=" * 60)
    print("Teste concluído. Se todos os itens acima mostraram [OK], as")
    print("credenciais e a assinatura estão corretas para as próximas fases.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
