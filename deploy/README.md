# Deploy na VPS

Backend + frontend numa porta so (8000), escutando **apenas no loopback**,
acessado por tunel SSH.

## Por que tunel e nao porta aberta

A API **nao tem autenticacao**. Ela expoe:

    POST /api/bot/pairs/{symbol}   habilita um par para operar com dinheiro real
    POST /api/bot/kill-switch      fecha todas as posicoes
    GET  /api/bot/balance          saldos da conta

Abrir a porta 8000 na internet daria esses botoes a quem escaneasse o IP.
O tunel resolve isso sem nenhuma configuracao extra: o acesso passa a exigir
a sua chave privada SSH, que voce ja tem.

Se um dia a API ganhar login de verdade, da para reavaliar — ate la, o
`--host 127.0.0.1` no systemd e o que segura.

## 1. Enviar o codigo (da sua maquina, PowerShell)

    cd C:\Users\henrique\Desktop\dashboardvalidadae
    npm --prefix frontend run build

    # `dist` vai junto: assim a VPS nao precisa de Node nem instalar 74 MB
    # de node_modules so para gerar arquivos estaticos.
    scp -i C:\Users\henrique\Desktop\key\ssh-key-2026-08-12.key -r `
        backend\bot backend\exchanges backend\deploy `
        backend\*.py backend\requirements.txt backend\pytest.ini `
        ubuntu@168.110.57.87:/home/ubuntu/arb/backend/

Mais simples e usar rsync (via WSL ou Git Bash), que sabe excluir:

    rsync -avz --progress \
      -e "ssh -i /c/Users/henrique/Desktop/key/ssh-key-2026-08-12.key" \
      --exclude 'venv' --exclude 'node_modules' --exclude '__pycache__' \
      --exclude '*.db' --exclude '*.db-journal' --exclude '.env' \
      ./ ubuntu@168.110.57.87:/home/ubuntu/arb/

**Os `*.db` ficam de fora de proposito.** O `arb_dashboard.db` tem ~2 GB de
historico de spread que se reconstroi sozinho em minutos; copiar isso pela
rede seria lento e inutil. O `arb_bot.db` (44 KB, config e historico de
operacoes) voce pode copiar se quiser levar os pares ja configurados.

## 2. Enviar o .env separadamente

O `.env` tem chaves de API com permissao de TRADE. Ele fica FORA do rsync de
proposito, para nunca ir junto por acidente num comando amplo:

    scp -i C:\Users\henrique\Desktop\key\ssh-key-2026-08-12.key `
        backend\.env ubuntu@168.110.57.87:/home/ubuntu/arb/backend/.env

**Antes de enviar, decida o modo.** Se voce nao quer que a VPS comece a
operar sozinha assim que subir, edite o `.env` e ponha:

    MEXC_BOT_LIVE_MODE=false

Voce liga depois, com a VPS ja validada.

## 3. Instalar (dentro da VPS)

    ssh -i C:\Users\henrique\Desktop\key\ssh-key-2026-08-12.key ubuntu@168.110.57.87
    bash /home/ubuntu/arb/deploy/setup-vps.sh

## 4. Acessar (da sua maquina)

    ssh -i C:\Users\henrique\Desktop\key\ssh-key-2026-08-12.key `
        -L 8000:127.0.0.1:8000 ubuntu@168.110.57.87

Com o tunel aberto, abra no navegador:

    http://localhost:8000

As URLs padrao do frontend ja apontam para `localhost:8000`, entao nada
precisa ser reconfigurado.

## Operacao

    sudo systemctl status arb-dashboard      # estado
    sudo journalctl -u arb-dashboard -f      # log ao vivo
    sudo systemctl restart arb-dashboard     # reiniciar
    sudo systemctl stop arb-dashboard        # PARAR o bot

## Atualizar depois

    # na sua maquina
    npm --prefix frontend run build
    rsync -avz -e "ssh -i .../ssh-key-2026-08-12.key" \
      --exclude venv --exclude node_modules --exclude '*.db' --exclude .env \
      ./ ubuntu@168.110.57.87:/home/ubuntu/arb/

    # na VPS
    sudo systemctl restart arb-dashboard

## Duas coisas para conferir antes de ligar o LIVE

1. **Fuso e relogio.** As assinaturas da MEXC usam timestamp; um relogio
   dessincronizado devolve 401 sem explicar. O Ubuntu ja roda `systemd-timesyncd`,
   mas confirme com `timedatectl` que o NTP esta ativo.
2. **Latencia.** Meça de dentro da VPS antes de confiar:

       curl -o /dev/null -s -w "%{time_total}s\n" https://api.mexc.com/api/v3/ping
