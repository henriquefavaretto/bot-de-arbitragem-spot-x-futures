#!/usr/bin/env bash
# Preparacao da VPS. Rode UMA VEZ, dentro da VPS, depois de enviar o codigo.
set -euo pipefail

echo "==> Dependencias do sistema"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip

cd /home/ubuntu/arb/backend

echo "==> Ambiente virtual e dependencias Python"
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

echo "==> Permissao do .env (contem chaves de API com permissao de TRADE)"
chmod 600 .env

echo "==> Servico systemd"
sudo cp /home/ubuntu/arb/deploy/arb-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable arb-dashboard
sudo systemctl restart arb-dashboard

sleep 8
echo
echo "==> Status"
sudo systemctl --no-pager status arb-dashboard | head -12
echo
echo "==> Teste local (dentro da VPS)"
curl -s --max-time 10 http://127.0.0.1:8000/api/health || echo "ainda subindo - veja: journalctl -u arb-dashboard -f"
echo
echo "Pronto. A porta 8000 escuta APENAS no loopback."
echo "Acesse da sua maquina com o tunel SSH (ver deploy/README.md)."
