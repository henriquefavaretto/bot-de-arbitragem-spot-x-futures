# MEXC Arb Terminal — Monitor de Arbitragem Spot × Futures

Dashboard local em tempo real para monitorar oportunidades de arbitragem entre
o mercado **Spot** e **Futures** da MEXC, com contador de cruzamentos de sinal,
sparkline de histórico de spread, e interface estilo "trading terminal".

---

## Arquitetura

```
mexc-arb-dashboard/
├── backend/          FastAPI + SQLite (Python)
│   ├── main.py            → app FastAPI, WebSocket /ws
│   ├── engine.py           → motor: matching de pares, spread, cruzamentos
│   ├── mexc_rest.py        → cliente REST (spot + futures)
│   ├── mexc_ws_futures.py  → cliente WebSocket de futures (push.tickers)
│   ├── storage.py          → SQLite (persistência de cruzamentos e histórico)
│   ├── config.py           → endpoints e parâmetros centrais
│   └── requirements.txt
└── frontend/          React + Vite
    └── src/
        ├── App.jsx
        ├── hooks/useArbitrageSocket.js   → conexão WS com o backend
        └── components/                    → tabela, cards, sparkline, etc.
```

### Como funciona

- O **backend** conecta na MEXC (REST + WebSocket público, sem necessidade de
  API key) e mantém em memória + SQLite o estado de cada par (preço spot,
  preço futures, spread %, funding rate, contador de cruzamentos).
- **Spot**: atualizado via polling REST a cada 3s (`GET /api/v3/ticker/24hr`).
- **Futures**: atualizado via WebSocket (`wss://contract.mexc.com/edge`,
  canal `push.tickers`, ~2s), com fallback automático para polling REST caso
  o WebSocket fique mais de 10s em silêncio.
- **Funding rate**: não vem no stream de tickers, então é atualizado via
  polling REST a cada 30s.
- Um **cruzamento** é contado sempre que o sinal do spread inverte (positivo
  → negativo ou vice-versa). O contador e o histórico de spread (para o
  sparkline) são persistidos em SQLite (`backend/arb_dashboard.db`), então
  sobrevivem a reinícios do backend.
- O backend expõe seu próprio WebSocket (`/ws`) que já envia os dados
  processados (spread calculado, cruzamentos, etc.) — o frontend não faz
  nenhum cálculo de arbitragem, só exibe.

> **Nota sobre o domínio da API:** em 19/01/2026 a MEXC migrou o domínio REST
> de futures de `contract.mexc.com` para `api.mexc.com` (o WebSocket de
> futures continua em `contract.mexc.com`). O backend já usa os endpoints
> atualizados.

---

## Pré-requisitos

- **Python 3.10+**
- **Node.js 18+** (recomendado 20+)

---

## Como rodar

### 1. Backend

```bash
cd backend
python3 -m venv venv

# Linux/Mac:
source venv/bin/activate
# Windows:
venv\Scripts\activate

pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

O backend sobe em `http://localhost:8000`. Ao iniciar, ele já começa a:
1. Descobrir os pares que existem em Spot **e** Futures simultaneamente.
2. Buscar os preços iniciais via REST.
3. Conectar no WebSocket de futures e começar o polling de spot.

Você pode checar `http://localhost:8000/api/health` para confirmar que está
processando pares (`pairs_tracked` deve ser > 0 depois de alguns segundos).

### 2. Frontend

Em outro terminal:

```bash
cd frontend
npm install
npm run dev
```

Abra `http://localhost:5173` no navegador. O Vite já está configurado com
proxy para `http://localhost:8000` (tanto REST quanto WebSocket), então não
há problema de CORS — não precisa mudar nada.

---

## Funcionalidades da interface

- **Cards de resumo**: total de pares monitorados, maior spread atual, par
  com mais cruzamentos, status da conexão (bolinha piscando verde/âmbar/vermelha).
- **Tabela**: ordenável por qualquer coluna (clique no cabeçalho). Para a
  coluna Spread %, há um toggle explícito na barra de ferramentas:
  - **Magnitude**: ordena pelo tamanho da oportunidade, ignorando o sinal
    (ex: -50% aparece antes de +10%). É o modo padrão.
  - **Valor real**: ordena pelo número com sinal — positivos no topo em
    ordem decrescente, negativos no topo em ordem crescente.
- **Busca** por nome do par, **filtro de spread mínimo** (valor absoluto),
  **filtros de volume mínimo separados para Spot e para Futuros** (útil
  para excluir pares com liquidez baixa demais para operar), e **filtro de
  "Máx. histórico ≥ %"** — mostra só os pares cujo maior spread positivo já
  registrado (`max_spread_pct`) ultrapassou o valor informado. Esse filtro
  olha exclusivamente o extremo positivo; um par que só teve spreads
  negativos grandes (ex: -80%) não aparece se o filtro for 10%, mesmo que
  a magnitude seja alta.
- **Flash de célula**: preço spot, preço futuros e spread piscam em
  verde/vermelho por ~0.6s quando o valor muda, sem re-renderizar a tabela
  inteira.
- **Sparkline** inline por par, mostrando o histórico recente de spread
  (linha tracejada = passou por um cruzamento de sinal no período mostrado).
- **Coluna "Mín / Máx histórico"**: mostra o menor e o maior spread já
  registrados para aquele par desde que o par começou a ser monitorado.
  Persistido em SQLite (sobrevive a reinícios do backend). O tooltip mostra
  há quanto tempo cada extremo ocorreu.
- **Coluna "Comparar" (📊)**: abre o gráfico do contrato futuro do par no
  TradingView. O TradingView não tem um link público que já abra o recurso
  "Compare" com os dois ativos sobrepostos automaticamente (isso só existe
  na Charting Library paga, embeddada em outros sites) — então o tooltip
  do ícone já mostra o símbolo spot exato para você colar em "⊕ Compare"
  dentro do próprio TradingView, com 1 clique.
- **Cruzamentos por janela de tempo**: um seletor na barra de ferramentas
  (**1h / 12h / 24h**) troca o número mostrado na coluna "Cruzamentos" e
  também a ordenação dessa coluna. O contador mostrado passa a refletir
  apenas os cruzamentos de sinal que ocorreram dentro da janela escolhida
  (não mais o total acumulado desde que o backend ligou). A coluna fica
  centralizada. O tooltip do contador mostra tanto a contagem da janela
  quanto o horário do último cruzamento.
- **Tema dark/light** (toggle no canto superior direito, preferência salva
  no navegador).
- Links diretos para a página do par **Spot** e **Futuros** na MEXC.

---

## Persistência

O contador de cruzamentos e o histórico de spread ficam salvos em
`backend/arb_dashboard.db` (SQLite). Se você reiniciar o backend, os
contadores continuam de onde pararam. Para zerar tudo, basta apagar esse
arquivo com o backend desligado.

Cada cruzamento de sinal também é registrado com timestamp numa tabela
separada (`crossing_events`), o que permite calcular quantos cruzamentos
aconteceram nas últimas 1h / 12h / 24h (usado pelo seletor de janela de
tempo na interface). Esses eventos são podados automaticamente após 48h
para o banco não crescer indefinidamente — isso não afeta o contador total
acumulado, que continua para sempre.

O menor e o maior spread já vistos por par também são salvos (tabela
`spread_extremes`), atualizados de forma incremental a cada nova amostra
— não é necessário guardar todo o histórico bruto para isso. Também
sobrevive a reinícios do backend; para zerar os extremos de um par
específico, apague a linha correspondente nessa tabela (ou o arquivo
inteiro do banco, para zerar tudo de uma vez).

---

## Ajustando parâmetros

Os principais parâmetros de timing ficam em `backend/config.py`:

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `FUNDING_RATE_POLL_INTERVAL` | 30s | Frequência do polling de funding rate |
| `REST_FALLBACK_POLL_INTERVAL` | 5s | Frequência de checagem do fallback REST |
| `PAIR_DISCOVERY_INTERVAL` | 300s | Frequência de re-descoberta de pares novos |
| `CROSSING_WINDOWS_REFRESH_INTERVAL` | 10s | Frequência de recálculo dos contadores 1h/12h/24h |
| `SPREAD_HISTORY_MAX_POINTS` | 300 | Pontos de histórico mantidos por par no banco (sparkline) |
| `SPOT_POLL_INTERVAL` (em `engine.py`) | 3s | Frequência do polling REST de spot |

---

## Troubleshooting

- **`pairs_tracked: 0` no `/api/health`**: confira sua conexão com a
  internet e se `api.mexc.com` não está bloqueado por firewall/proxy. Alguns
  provedores de nuvem (AWS/GCP fora de certas regiões) podem sofrer bloqueio
  geográfico da MEXC — nesse caso, considere rodar de uma VPS em
  Singapura/Japão, como a própria MEXC recomenda para acesso estável.
- **WebSocket do frontend não conecta**: confirme que o backend está
  rodando em `localhost:8000` antes de abrir o frontend — o proxy do Vite
  depende disso.
- **Erro 403 nas chamadas REST**: a MEXC por vezes aplica um WAF sensível a
  `User-Agent`. O backend já envia um `User-Agent` de navegador comum; se
  persistir, tente rodar de outra rede/IP.
