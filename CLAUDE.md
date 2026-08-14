# MEXC Arbitrage Dashboard + Bot — Contexto para Claude Code

Este arquivo documenta o histórico completo de decisões, arquitetura e
bugs corrigidos neste projeto, construído em uma longa sessão anterior no
Claude.ai. Leia isto antes de fazer qualquer mudança — muitas das decisões
aqui parecem "estranhas" ou "conservadoras demais" à primeira vista, mas
cada uma existe por causa de um bug real, com dinheiro real, que já
aconteceu e foi corrigido.

## O que este projeto é

Um sistema de arbitragem Spot × Futures na MEXC, em duas partes:

1. **Dashboard**: monitora centenas de pares (ex: JIMOTHY, TUT, BICO),
   calculando o spread entre o mercado Spot e o Futures perpétuo
   correspondente, em tempo real.
2. **Bot**: opera automaticamente uma estratégia de arbitragem em um
   número pequeno de pares (o usuário opera no máximo ~5 por vez):
   compra Spot + vende Futures (short) quando o spread está favorável,
   e fecha as duas pernas quando o spread converge.

Stack: **backend Python (FastAPI, asyncio, SQLite via aiosqlite)** +
**frontend React (Vite)**. Sem framework de ORM; SQL cru com aiosqlite.

## Como rodar (Windows/PowerShell, ambiente real do usuário)

```powershell
cd backend
python -m venv venv
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned  # só na 1a vez
venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

```powershell
cd frontend
npm install
npm run dev
```

O usuário testa manualmente no navegador em `http://localhost:5173`. Não
há CI. O ambiente de desenvolvimento anterior (sandbox do Claude.ai) **não
tinha acesso de rede à MEXC** — todo teste de integração real com a API
precisa ser feito manualmente pelo usuário, na máquina dele. Ao trabalhar
aqui, prefira testes unitários com mocks a tentar bater na API real.

## Regra de ouro: dinheiro real está em jogo

O modo LIVE do bot envia ordens reais na conta MEXC do usuário. Toda
mudança na lógica de decisão de entrada/saída, cálculo de spread, ou
execução de ordens precisa ser tratada com o mesmo rigor de um sistema
financeiro em produção. Isso significa:

- Nunca adicionar fallbacks "silenciosos" que usam dados de qualidade
  inferior quando os corretos não estão disponíveis (ver seção de bugs
  abaixo — isso já causou prejuízo real duas vezes).
- Preferir "não fazer nada" a "fazer algo com dados incertos".
- Sempre escrever teste automatizado reproduzindo o bug antes de
  corrigi-lo, e manter esses testes.
- Documentar aqui e no `backend/BOT_README.md` qualquer bug financeiro
  encontrado, mesmo depois de corrigido — a documentação do "porquê"
  evita reintroduzir o mesmo erro de forma diferente.

## Arquitetura do backend

```
backend/
├── main.py                    # FastAPI app, endpoints REST, WebSocket, lifecycle
├── engine.py                  # ArbitrageEngine: motor do Dashboard (preços, spread)
├── storage.py                 # SQLite do Dashboard (histórico, extremos, cruzamentos)
├── config.py                  # Constantes de configuração (intervalos, URLs)
├── mexc_rest.py                # Cliente REST público (tickers spot/futures)
├── mexc_ws_futures.py          # Cliente WS de Futures (2 canais, ver abaixo)
├── mexc_ws_spot.py             # Cliente WS de Spot (protobuf, bookTicker)
├── test_credentials.py         # Script manual de validação de API keys (read-only)
├── pytest.ini                  # Configuração da suíte de testes
├── tests/                      # Testes automatizados (rodam sem rede, contra dublês)
└── bot/
    ├── bot_engine.py            # ArbitrageBotEngine: máquina de estados do bot
    ├── bot_storage.py           # SQLite do bot (config por par, posições, eventos)
    ├── depth.py                  # Preço EXECUTÁVEL por profundidade de book (VWAP por tamanho)
    ├── execution.py              # Ordens IOC com teto de slippage, retry e escalonamento
    ├── costs.py                  # Taxas de taker e funding; viabilidade econômica da entrada
    ├── market_data.py            # Leitura pública de book (sem credenciais)
    ├── mexc_spot_client.py      # Cliente REST autenticado Spot (ordens)
    ├── mexc_futures_client.py   # Cliente REST autenticado Futures (ordens)
    ├── mexc_futures_ws_private.py  # WS privado de Futures (não usado ativamente)
    ├── mexc_protobuf_decoder.py # Decodificador manual de wire-format protobuf
    ├── proto/                   # .proto oficiais da MEXC + código gerado (_pb2.py)
    ├── sizing.py                 # Conversão USDT <-> quantidade/contratos, arredondamento
    └── log_buffer.py             # Handler de log em memória (ring buffer) para a aba Logs
```

### Testes

```powershell
cd backend
venv\Scripts\python.exe -m pytest -q
```

A suíte roda inteiramente contra dublês (`tests/conftest.py`), sem rede.
Cada bug financeiro documentado abaixo tem um teste de regressão nomeado —
se um deles voltar a falhar, o bug voltou. Os dublês imitam de propósito os
comportamentos torcidos da API da MEXC (`executedQty=0` numa ordem que
executou, ordem IOC terminando `CANCELED` com preenchimento parcial dentro,
saldo com imprecisão de ponto flutuante): um dublê "limpo demais" faz o
teste passar e a produção quebrar, que foi exatamente como os bugs 4, 5 e 6
chegaram até a conta real.

### Frontend

```
frontend/src/
├── App.jsx                     # Shell com abas: Dashboard | Bot | Histórico | Logs
├── components/
│   ├── ArbitrageTable.jsx      # Tabela principal do Dashboard
│   ├── BalanceBar.jsx          # Saldo Spot/Futures no topo
│   ├── BotPanel.jsx             # Configuração e status do bot
│   ├── BotPairConfigForm.jsx   # Form de adicionar/editar par no bot
│   ├── BotHistoryPanel.jsx     # Histórico de operações (entrada/saída/erros)
│   └── BotLogsPanel.jsx        # Logs técnicos em tempo real
├── hooks/
│   ├── useArbitrageSocket.js   # WS do Dashboard
│   ├── useBotSocket.js          # WS do bot
│   └── useBotHistory.js         # Polling do histórico
└── utils/format.js              # formatPrice, formatSpread, formatDateTime, etc.
```

## Conceito central: DOIS spreads, não um

Esta é a decisão mais importante do projeto e a origem do bug mais caro
que corrigimos. Nunca simplifique de volta para um spread único.

A estratégia é: comprar Spot + vender Futures (entrada), depois vender
Spot + recomprar Futures (saída). Entrada e saída executam em lados
opostos do book:

```
Spread de ENTRADA = (futures_BID - spot_ASK) / spot_ASK
    -> você paga o ask do spot, recebe o bid do futures

Spread de SAÍDA   = (futures_ASK - spot_BID) / spot_BID
    -> você recebe o bid do spot, paga o ask do futures
```

Em pares com book largo (comum em memecoins de baixa liquidez, como
JIMOTHY), esses dois números divergem por vários pontos percentuais. Um
bug histórico usava a fórmula de entrada para decidir a saída também,
fazendo o bot "ver" um spread ótimo quando o real era bem pior — gerando
prejuízo sistemático mesmo com o spread "convergindo" na tela.

`engine.py` -> `PairState.recompute_spread()` calcula os dois:
`spread_pct` (entrada) e `exit_spread_pct` (saída), a partir de
`spot_bid`, `spot_ask`, `futures_bid`, `futures_ask`.

O Dashboard exibe ambos em colunas separadas: "Spread entrada" e "Spread
saída", cada um com seu próprio Mín/Máx histórico (`spread_extremes` e
`exit_spread_extremes`, tabelas separadas no SQLite).

## Conceito central: "preço do book" vs "último negociado"

Segunda decisão crítica. A MEXC tem várias formas de reportar preço:

- `lastPrice`: o último negócio fechado. Pode ser de minutos atrás em par
  ilíquido, e não corresponde a nada executável agora.
- `bid`/`ask`: o topo do livro de ofertas agora. Isso é o preço real de
  execução.

Regra do projeto: nunca usar `lastPrice` para decidir uma operação. Todo
cálculo de spread usado pelo bot exige preços vindos do book. Isso é
rastreado por par via `_futures_price_source` (`"book"` ou `"last"`) e
`spot_ws.is_trusted()` / `_spot_rest_has_book`.

Na prática isso significa:
- Pares sem book são completamente ocultados do Dashboard (não só
  "mostrados com aviso" — ocultados de verdade, e não contam para
  cruzamentos, histórico de spread nem extremos).
- O bot nunca abre posição nova sem preços do book (`prices_from_book`
  passado ao `on_price_update`).
- O bot nunca fecha posição sem o spread de saída E os preços de saída
  completos (`exit_spread_pct`, `spot_bid`, `futures_ask` todos não-
  `None`). Ver seção de bugs para o porquê disso ser estrito sem
  fallback.

O ícone ⚡ na UI indica "este preço vem do book". Sem ⚡ = não confie nele.

## Conceito central: "topo do book" vs "preço executável para o seu tamanho"

Terceira decisão crítica, e a mais recente. É uma continuação direta da
anterior: não basta o preço vir do book — ele precisa vir da parte do book
que a SUA ordem vai realmente consumir.

`bid1`/`ask1` (futures) e `bookTicker` (spot) dão o melhor preço disponível
**para a quantidade que existe naquela primeira camada**. Em memecoin
ilíquida, essa camada frequentemente comporta uma fração da posição. Uma
ordem maior "anda" o book, consumindo camadas progressivamente piores, e o
preço médio de execução fica longe do topo que estava na tela.

Medição ao vivo em JIMOTHY (04/08/2026), com o mesmo book instantâneo:

```
TAMANHO      CONTRATOS   SPREAD TELA   EXECUTÁVEL    MENTIRA
5 USDT       7               0.900%        0.900%     0.000 pp
50 USDT      74              0.900%        0.900%     0.000 pp
100 USDT     148             0.900%        0.856%     0.044 pp
250 USDT     371             0.900%        0.249%     0.651 pp
500 USDT     743             0.900%       -0.765%     1.665 pp  <- book não comporta
```

O mesmo book, no mesmo instante, oferece 0,9% ou -0,77% dependendo só do
tamanho da posição. **Spread não é uma propriedade do par; é uma propriedade
do par PARA UM TAMANHO.** O número na tela sem o tamanho é incompleto.

A solução está em `bot/depth.py`: `walk_by_qty` / `walk_by_quote` caminham as
camadas do book e devolvem o VWAP da execução, e
`entry_executable_spread` / `exit_executable_spread` produzem os dois spreads
já para o tamanho configurado.

### Arquitetura em dois portões

O spread de topo de book continua chegando de graça pelo WebSocket a cada
tick; fazer uma consulta de profundidade a cada tick esbarraria no rate limit
da MEXC. Então o fluxo tem dois portões:

1. **Gatilho (barato, do WS)**: o spread de topo cruza o limiar? Se não,
   nada acontece — nenhuma chamada REST é gasta.
2. **Confirmação (duas chamadas REST em paralelo, ~100ms)**: busca a
   profundidade real dos dois books, recalcula o spread para o tamanho exato
   e verifica se ainda sobra dinheiro depois das taxas. **Só este segundo
   número autoriza a ordem.**

O topo do book virou gatilho. Ele nunca mais decide sozinho.

Os dois books são buscados com `asyncio.gather`, nunca em sequência: buscar
um depois do outro colocaria a latência de uma chamada REST inteira entre os
dois snapshots, e um spread calculado a partir de dois momentos diferentes é
exatamente o tipo de número que parece válido e não é.

## Conceito central: casar as pernas por QUANTIDADE, não por valor

Quarta decisão crítica. As duas pernas precisam ter a mesma quantidade da
moeda base, não o mesmo valor em USDT.

Parece equivalente, mas não é: os dois mercados executam a preços diferentes
— a diferença entre eles é o spread, que é a razão de a operação existir.
Casar valor com preços diferentes produz quantidades diferentes.

No caso real de 03/08/2026: 600 JIMOTHY vendidos no futures contra 608,48
comprados no spot. As 8,48 unidades de diferença são exatamente 1,4% da
posição — o spread de entrada — e constituem uma **aposta direcional embutida
que ninguém decidiu fazer**, numa estratégia cuja premissa inteira é ser
neutra a direção.

Casando por quantidade `Q`, o resultado é exatamente:

```
lucro = Q * [(F_entrada - S_entrada) - (F_saída - S_saída)]
```

Só spread de entrada menos spread de saída, sem nenhum termo direcional. É a
identidade que a estratégia promete.

Detalhe importante: a MEXC cobra a taxa de uma COMPRA no Spot **na própria
moeda comprada** (comprar 600 JIMOTHY deixa ~599,7 na carteira). Como é o
saldo líquido que ficará disponível para vender na saída, a compra é inflada
pela taxa para que o SALDO RESULTANTE case com o short — não a quantidade
bruta pedida. Ver `_target_spot_qty` em `bot_engine.py`.


## Conceito central: multi-exchange e o consenso de preço

Quinta decisão crítica, e a que mais protege dinheiro no modo multi-exchange.

O sistema monitora 3 exchanges x 2 mercados = 6 **venues** (`mexc:spot`,
`gate:futures`, `bingx:futures`, ...). Um símbolo deixou de ser "spot e
futures" e passou a ser uma linha de cotações por venue; as combinações são
derivadas: 9 Spot x Futures + 3 Futures x Futures = **12 por símbolo**
(~5900 monitoráveis, medidas em 05/08/2026).

### O MESMO TICKER EM DUAS EXCHANGES NÃO É NECESSARIAMENTE O MESMO ATIVO

Medições reais dos seis venues ao vivo:

```
BTC    os 6 venues concordam dentro de 0,07%           <- normal
ETH    os 6 venues concordam dentro de 0,06%           <- normal

VANRY  5 venues em ~0,00333, gate:spot em 0,001451     <- 2,3x menor
COTI   todos os futures ~0,0135, todos os spots ~0,0108 <- 20%
SPCX   gate:futures 115,66 contra gate:spot 99,88      <- 15%, MESMA exchange
```

Nenhum é oportunidade: são redenominação, migração para token v2 e mercado
pré-lançamento. VANRY aparecia com **128% de "spread"**. Montar essa operação
compraria um ativo e venderia OUTRO — exposição direcional integral, hedge
nenhum, na estratégia que promete neutralidade.

Note que o caso COTI derruba a intuição de que basta comparar exchanges: a
divergência é entre SPOT e FUTURES, inclusive dentro da mesma exchange.

`evaluate_consensus` compara o mid de cada venue contra a MEDIANA de todos
(mediana e não média: a média é arrastada pelo próprio outlier que se quer
detectar) e descarta quem passar de `MAX_VENUE_DEVIATION_PCT` (5%). Com menos
de 3 venues não há mediana confiável, e quem protege é o limite de
plausibilidade do spread (10%). Linhas barradas guardam o MOTIVO e continuam
visíveis sob demanda — omiti-las faria o operador procurar um número sumido.

### A armadilha da ordenação do book

Cada exchange devolve as camadas na ordem que quer:

```
gate spot/futures  bids=DECR  asks=CRESC
bingx spot         bids=DECR  asks=DECR    <-- único fora do padrão
bingx swap         bids=DECR  asks=CRESC
mexc spot/futures  bids=DECR  asks=CRESC
```

O spot da BingX devolve os asks do PIOR para o melhor. Ler `asks[0]` como
topo daria o pior preço como se fosse o melhor, e o VWAP sairia consumindo o
book de trás para frente — sem exceção, sem log, visível só no fill.
`build_order_book` normaliza sempre e avisa quando a ordem recebida
contradiz a declarada pelo adaptador.

### Convenções da camada

- Adicionar exchange = novo adaptador + entrada em `ADAPTER_CLASSES`. Nada
  mais no sistema conhece nomes de exchange.
- Conversão canônico<->nativo vive SÓ no adaptador (`BTCUSDT`, `BTC_USDT`,
  `BTC-USDT` — o bug 2 com três vezes mais superfície).
- Book de futures vem em CONTRATOS; o multiplicador é `contractSize` (MEXC),
  `quanto_multiplier` (Gate) e `size` (BingX).
- Taxas são lidas por venue e nunca assumidas: Gate futures cobra 0,075%
  contra 0,02% da MEXC — 3,75x, o suficiente para inverter a ordem de
  atratividade das linhas. O snapshot ordena pelo LÍQUIDO, nunca pelo bruto.
- Filtragem no SERVIDOR: 5900 linhas não vão para o navegador filtrar.
- Consenso calculado sobre TODOS os venues, não só os filtrados: um venue
  fora da tela ainda é evidência sobre o preço verdadeiro.


## Conceito central: execução por venue (bot multi-exchange)

Sexta decisão crítica. Cada exchange expressa "abrir venda" e "fechar venda"
de um jeito **incompatível** com as outras:

```
MEXC futures    side=3 abre short, side=2 fecha;  quantidade em CONTRATOS
Gate futures    SEM campo side: o SINAL de `size` é a direção
                size<0 vende, size>0 compra;      quantidade em CONTRATOS
BingX swap      par (side, positionSide):
                SELL/SHORT abre, BUY/SHORT fecha; quantidade na MOEDA BASE
```

Espalhar esses três dialetos pela máquina de estados garantiria que um deles
ficasse errado em algum caminho — e "errado" aqui não é ordem rejeitada, é
**ordem executada na direção contrária, que DOBRA a exposição em vez de
zerá-la**. É a falha mais cara possível nesta camada e a que menos se anuncia.

`bot/venue_trader.py` expõe quatro operações na linguagem da ESTRATÉGIA:

```
open_buy_leg    entra na ponta comprada  (spot BUY ou futures LONG)
close_buy_leg   desfaz a ponta comprada
open_sell_leg   entra na ponta vendida   (futures SHORT — spot não pode)
close_sell_leg  desfaz a ponta vendida
```

**Toda quantidade na interface é na MOEDA BASE.** A conversão para contratos
acontece dentro de cada implementação, via `contract_size`. É isso que
permite casar as pernas por quantidade (a correção do bug 11) entre venues
com unidades diferentes.

### Assinaturas: três esquemas distintos

```
MEXC spot     HMAC-SHA256 da query string,  header X-MEXC-APIKEY
MEXC futures  HMAC-SHA256 de accessKey+ts+body, headers ApiKey/Signature
Gate          HMAC-SHA512 de METHOD
path
query
sha512(body)
ts
BingX         HMAC-SHA256 da query literal, header X-BX-APIKEY
```

Detalhes que devolvem 401 sem dizer por quê:
- Gate: corpo vazio vira o **hash da string vazia**, não linha em branco; e o
  JSON assinado precisa ser byte-a-byte o enviado (separadores compactos).
- Gate: preço em notação científica é rejeitado — `str(0.00000123)` dá
  `"1.23e-06"`, e preço de memecoin cai nessa faixa direto.
- BingX: HTTP 200 com `code != 0` é ERRO; checar só o status engole a falha.
- BingX: a query é assinada como TEXTO — reordenar depois invalida.

### Credenciais ausentes recusam na DECISÃO, não na execução

`VENUE_CLIENTS` em `main.py` mapeia venue → cliente autenticado. Uma
combinação só é operável com cliente dos DOIS lados; `/api/venues/trading`
expõe quais estão liberadas e quais faltam credencial. A recusa acontece
antes de qualquer ordem — o alternativo seria descobrir no meio da operação,
com uma perna já aberta (o cenário do bug 15).

### O `venue_trader` ESTÁ ligado (desde 13/08/2026)

`bot_engine` não envia mais ordem direto: entrada, saída, reversão de perna
órfã, fechamento a mercado e kill switch passam todos por `VenueTrader`. As
antigas `_spot_send` / `_futures_send` / `_wait_*_fill` foram **removidas** —
mantê-las como "legado inofensivo" seria um convite a chamá-las e reintroduzir
o bug 18, do mesmo jeito que manter o nome `new_order_limit_ioc` convidaria a
reintroduzir o 17.

`run_leg` é o único ponto de entrada e fecha o ciclo de vida da ordem antes de
devolver um número: envia → faz POLLING do status → se não terminou sozinha,
CANCELA e RELÊ. Isso generaliza a correção do bug 17 para todos os venues,
inclusive os que documentam IOC de verdade — a sub-lição é que um parâmetro
aceito não é um parâmetro honrado.

Quando o destino de uma ordem não pode ser lido, `run_leg` levanta
`UnknownOrderStateError` e o trader fica **envenenado**: recusa enviar
qualquer ordem nova até liberação manual. Mandar outra ordem sem saber o que a
anterior fez é literalmente o mecanismo que dobrou a posição em 09/08.

**Unidades da moeda base atravessam a interface.** A conversão para contratos
mora dentro de cada executor. `bot_engine` converte de volta para contratos ao
contabilizar a posição — a moeda base é a unidade de TRANSPORTE entre venues,
os contratos são a unidade de CONTABILIDADE do futures.

### Estado de validação: "implementado" ≠ "validado"

**Só o caminho MEXC foi exercitado contra a API real.** Gate e BingX são
implementações a partir da documentação, cobertas por testes contra dublês
(`tests/test_venue_clients.py`).

A trava agora é `BOT_VALIDATED_VENUES` (no `.env`, não na UI — mesma lógica de
`MEXC_BOT_LIVE_MODE`), com `DEFAULT_VALIDATED_VENUES = {mexc:spot,
mexc:futures}`. Um venue fora dela é recusado na configuração, na entrada e na
saída, inclusive em SIMULAÇÃO.

Para liberar um venue:

```powershell
cd backend
venv\Scripts\python.exe -m bot.validate_venue gate:futures --symbol BTC
venv\Scripts\python.exe -m bot.validate_venue gate:futures --symbol BTC --ordem
```

O `--ordem` envia UMA ordem limite mínima 30% fora do mercado (aceita, não
executa) e a acompanha por `run_leg` — o mesmo caminho da produção. Valida
assinatura, formato de símbolo, precisão, leitura de status e cancelamento sem
gastar dinheiro. Só depois disso o venue entra em `BOT_VALIDATED_VENUES`.

Atenção: se as chaves tiverem whitelist de IP, a validação precisa rodar da
máquina cujo IP está cadastrado (hoje, a VPS).

### Risco específico de cross-exchange

Arbitragem entre exchanges exige saldo **pré-posicionado nas duas** (não dá
para transferir na hora nem netar as pernas), e o modo de falha "uma perna
executou e a outra não" fica mais provável (duas APIs, dois rate limits) e
mais caro: **não dá para reverter na mesma conta**, que é o que
`_revert_futures_leg` faz hoje no caminho MEXC.

## Fontes de preço e por que há várias camadas

### Spot
1. REST (`mexc_rest.fetch_spot_tickers`, polling a cada poucos segundos):
   usa `askPrice` do endpoint `/api/v3/ticker/24hr` (que já retorna
   bid/ask no formato FULL, sem precisar de outro endpoint).
2. WebSocket (`mexc_ws_spot.py`, canal `bookTicker`, protobuf): mais
   rápido, mas com uma complicação séria (ver abaixo).

### Futures
1. REST (`mexc_rest.fetch_futures_tickers`): traz `bid1`/`ask1` para
   todos os contratos numa única chamada. É a fonte mais completa.
2. WS canal agregado (`sub.tickers`, todos os contratos, ~2s): NÃO tem
   bid/ask, só `lastPrice`. Confirmado na documentação oficial da MEXC
   (campos `maxBidPrice`/`minAskPrice` são limites de preço para ordens,
   não o book).
3. WS canal individual (`sub.ticker`, por símbolo, ~1s): tem
   `bid1`/`ask1`. Usado só para os pares "prioritários" (configurados no
   bot), por causa do limite de 200 subscrições por conexão da MEXC.

Essas fontes múltiplas por par já causaram um bug sério de fontes se
sobrescrevendo — ver seção de bugs, "Pares oscilando na tabela".

## O decodificador de protobuf do Spot: por que é assim

O WebSocket público de Spot da MEXC usa Protocol Buffers, não JSON.
Durante o desenvolvimento, encontramos um relato técnico confiável
reportando que a documentação oficial de `.proto` da MEXC está
desatualizada/incorreta em pontos importantes (especificamente, o número
de campo exato do `oneof body` no wrapper de mensagens).

Em vez de arriscar um parser hand-rolled com números de campo não
confirmados (que falharia silenciosamente, entregando preços decodificados
errados sem nenhum erro visível — o pior cenário possível para um sistema
que move dinheiro), a solução implementada em
`bot/mexc_protobuf_decoder.py` é:

1. Decodificação genérica do wire-format (TLV: tag, wire-type, length,
   value) sem assumir o número do campo do `oneof`.
2. Testa cada campo `length-delimited` como candidato a ser a submensagem
   `PublicBookTickerV3Api`, e só aceita se o resultado passar validação
   física (preços positivos, `ask >= bid`).
3. Validação cruzada obrigatória contra REST (`mexc_ws_spot.py`): um
   preço do WS só passa a ser "confiável" depois de 5 amostras
   consecutivas concordando com o REST (tolerância 0.5%), e é revalidado
   periodicamente depois disso. Diverge, perde a confiança na hora e
   volta pro REST.

Se você for mexer nesse decoder, não remova a validação cruzada, mesmo
que pareça redundante. Ela é a rede de segurança contra o parser estar
sutilmente errado em algum caso não coberto pelos testes (o ambiente de
desenvolvimento nunca teve acesso de rede à MEXC para testar isso contra
o servidor real).

## Bugs históricos importantes (não repetir)

Esta seção documenta bugs reais, com dinheiro real, encontrados e
corrigidos ao longo do desenvolvimento. Cada um tem uma lição de design
que deve ser preservada.

### 1. Ordenação de spread ignorava o sinal
Bug de UI simples: ordenar "maior→menor" misturava positivos e negativos
por um bug de estado não resetado (`sortBySpreadAbs` travado em `true`).
Corrigido com toggle explícito Magnitude/Valor real. Lição: testar
explicitamente os dois sentidos de ordenação, não só um.

### 2. Bot não entrava com símbolo salvo como "JIMOTHYUSDT" em vez de "JIMOTHY"
O Dashboard identifica pares sem sufixo (`display_symbol`), mas o
formulário do bot permitia salvar com sufixo. Como as chaves eram
diferentes, o bot nunca recebia updates de preço daquele par — configurado
mas "surdo". Corrigido normalizando o símbolo em todos os pontos de
entrada do `bot_engine.py` (`_normalize_symbol`), com migração automática
de configs salvas no formato antigo.
Lição: qualquer identificador que atravessa múltiplos módulos precisa de
uma única fonte de normalização, chamada em todo ponto de entrada, não
confiada a quem está chamando.

### 3. Bot entrava com spread negativo
Usava `abs(spread_pct) >= entry_spread_pct`, entrando em qualquer direção.
A estratégia só é lucrativa com spread positivo (futures > spot na
entrada); negativo trava prejuízo garantido na convergência. Corrigido
para `spread_pct >= entry_spread_pct` (sem valor absoluto), com validação
rejeitando `entry_spread_pct <= 0` na configuração.
Lição: "magnitude" e "direção correta para a estratégia" são coisas
diferentes; nunca usar `abs()` sem justificar explicitamente por quê.

### 4. `Insufficient position` ao vender no Spot
A MEXC desconta taxa de trading na própria moeda comprada (comprar
JIMOTHY com USDT gera taxa cobrada em JIMOTHY). O bot vendia a quantidade
calculada na entrada, sem nunca confirmar o saldo real disponível.
Corrigido: `_close_spot_market` sempre consulta `get_balance()` antes de
vender e usa o mínimo entre calculado e real.
Lição: nunca confiar em quantidade calculada internamente para uma
operação de venda; sempre reconsultar o saldo real antes.

### 5. `amount scale is invalid` (duas vezes — venda e depois compra)
A MEXC exige que a quantidade/valor de uma ordem respeite a precisão
decimal do símbolo. O saldo consultado via API vem com imprecisão de
ponto flutuante (`1188.70000001` em vez de `1188.7`). Corrigido com
`round_spot_quantity()` e `round_spot_quote_qty()` em `bot/sizing.py`,
sempre arredondando para baixo, carregando a precisão real via
`GET /api/v3/exchangeInfo` (spot) e usando isso antes de qualquer ordem.
Lição: todo valor calculado por multiplicação/divisão de floats que vai
ser reenviado para uma API externa precisa ser arredondado na precisão
exata que a API espera — o padrão se repete em qualquer ponto que
"calcula e reenvia".

### 6. Confirmação de fill não confirmava de verdade
A resposta imediata do `POST /order` da MEXC pode retornar
`executedQty=0` mesmo quando a ordem já executou de verdade (a exchange
ainda não refletiu isso na resposta síncrona). O bot achava que tinha
falhado, tentava de novo, e vendia duas vezes (confirmado no histórico
real de ordens da MEXC). Corrigido com `_wait_spot_fill()` (polling do
status real via `get_order`) aplicado tanto na entrada quanto na saída —
a correção inicial só cobriu a entrada, faltando a saída, o que causou
uma segunda rodada do mesmo bug.
Lição: ao corrigir um padrão de bug, buscar todos os lugares onde o
mesmo padrão se repete, não só o que foi reportado.

### 7. Saída usando a fórmula de spread errada (a mais cara)
Ver "Conceito central: DOIS spreads" acima. Bug que gerou prejuízo
sistemático mesmo com "convergência" aparente na tela. A correção inicial
teve um fallback perigoso:
```python
effective_exit_spread = exit_spread_pct if exit_spread_pct is not None else spread_pct
```
Quando o book de saída não estava disponível, caía silenciosamente para o
spread de entrada — reintroduzindo exatamente o bug que a correção deveria
eliminar. Corrigido removendo todo fallback: a saída agora exige sem
exceção `exit_spread_pct`, `spot_bid`, `futures_ask` não-nulos e
`prices_from_book=True`; faltando qualquer um, o bot não sai, mesmo que
isso signifique ficar mais tempo posicionado.
Lição, a mais importante do projeto: em código financeiro, um fallback
para "dado pior mas disponível" quase sempre é pior que "não fazer nada
até ter o dado certo". Questionar todo `if x is not None else fallback`
em código que decide execução de ordens.

### 8. Pares oscilando na tabela (575 -> 160 -> 355 -> 370 pares)
Depois de implementar o filtro de "ocultar pares sem book", duas fontes
de preço de Futures começaram a se sobrescrever: o REST (5s) marcava
"book", o WS agregado (2s) apagava e marcava "last" — brigando
indefinidamente. Corrigido fazendo o canal agregado nunca destruir book
já estabelecido por outra fonte; ele só atualiza volume nesse caso.
Lição: quando múltiplas fontes assíncronas escrevem no mesmo estado,
pensar explicitamente em qual tem precedência e garantir que uma fonte
"pior" nunca apaga dado de uma fonte "melhor" só por chegar depois.

### 9. Performance: 580 pares levavam 4.82s por ciclo (loop de 5s)
Um commit SQLite por par (fsync em disco) dominava o tempo do ciclo.
Corrigido com `defer_commit=True` nos métodos de escrita + um único
`storage.commit()` ao final do loop. Ganho: 26.7x (4.82s -> 0.18s).
Lição: em SQLite, agrupar escritas em lote sempre que possível; commits
individuais em loop são o antipadrão mais comum de performance.

### 10. WS de Spot sem limite de subscrições
Descoberto ao investigar performance: o código subscrevia todos os pares
descobertos (500+) no WS de Spot, mas o limite da MEXC é 200 por conexão.
Corrigido com sistema de prioridade (pares do bot sempre têm vaga) e teto
de 180 (margem de segurança).

### 11. Spread do topo do book decidindo ordens a mercado (a segunda mais cara)
Sintoma relatado: "os spreads não estão batendo com a realidade, às vezes
mais de 1% de diferença". Operação real de JIMOTHY em 03/08/2026, ambos os
eventos no `bot_trade_log`:

```
              TELA        REALIZADO    DIFERENÇA
Entrada      +2.0255%      +1.4146%     -0.61 pp
Saída        +0.2323%      +1.4395%     +1.21 pp
```

Como `lucro = spread_entrada - spread_saída`, a tela prometia **+1,79%** e a
execução entregou **-0,025%** (PnL medido: -0,0010 USDT sobre 4,30 USDT de
notional). **1,82 pontos percentuais — o lucro inteiro da operação — foram
consumidos por execução.**

Duas causas somadas, ambas corrigidas:

1. O spread era calculado no TOPO do book, mas as ordens eram a **mercado**,
   sem nenhum limite de preço, e varriam camadas arbitrariamente piores. Ver
   "topo do book vs preço executável" acima.
2. As pernas eram casadas por VALOR em USDT, deixando 1,4% de exposição
   direcional descoberta. Ver "casar por quantidade" acima. Nesta operação
   isso inverteu o sinal do resultado: com quantidades casadas, o PnL teria
   sido +0,0011 em vez de -0,0010.

Correção em três camadas:
- `bot/depth.py`: o spread passa a ser o VWAP da profundidade real para o
  tamanho da posição; se o book não comporta o tamanho, o bot **não opera**
  (nunca executa "o que couber" — parcial descasa as pernas).
- `bot/execution.py`: ordens **LIMIT IOC** com teto de slippage ancorado no
  VWAP confirmado, com retry e escalonamento explícito.
- `bot/costs.py`: taxas das quatro pernas e funding entram na decisão.

Lição: "preço do book" não é suficiente — é preciso ser o preço da parte do
book que a sua ordem vai consumir. E uma ordem a mercado é uma ordem sem
teto: em book raso ela é o oposto de "executar ao preço da tela".

**Sub-lição sobre o teto de slippage**: o preço-limite é calculado UMA vez, na
decisão, e não se move entre as tentativas de retry. Recalculá-lo a partir do
book novo a cada tentativa pareceria mais adaptativo, mas faria cada retry
perseguir o preço para longe — três tentativas com 0,3% de tolerância
executariam 0,9% pior que o decidido. **Um teto que se move não é um teto.**

### 12. Book de futures congelando sem ninguém perceber
Encontrado ao investigar o bug 11. O `futures_rest_poll_loop` cedia a vez
para o WebSocket individual sempre que o par fosse prioritário e já tivesse
book — **sem olhar a idade do dado**. Mas o canal `sub.ticker` da MEXC só
empurra atualização quando há negócios naquele contrato; num par ilíquido
(exatamente os que este bot opera) o bid/ask podia congelar por minutos
enquanto o polling REST era instruído a não tocar nele.

Resultado: spread calculado com o spot de agora contra um futures de vários
minutos atrás. Parecia oportunidade e era só o relógio.

Corrigido com carimbo de tempo POR LADO do book (`spot_book_ts` /
`futures_book_ts` em `engine.py` — `last_update_ts` sozinho não serve, porque
qualquer fonte o atualiza), precedência do WS válida só enquanto o dado for
recente (`FUTURES_WS_BOOK_MAX_AGE`), e recusa de decisão quando qualquer um
dos lados passa de `MEXC_BOT_MAX_BOOK_AGE_S`.

Lição: "dado fresco" e "dado presente" são coisas diferentes. Toda métrica de
frescor precisa ser por FONTE, não agregada — o perigo mora na assimetria
entre os lados, e uma medida agregada esconde exatamente isso.

### 13. Entrada dupla por corrida entre fontes de preço
Também encontrado durante o bug 11. `on_price_update` é chamado por cinco
fontes assíncronas independentes (WS de spot, WS de futures, dois pollings
REST e o fallback). O estado só mudava para `ENTERING` depois de vários
`await`, então duas fontes podiam ver o mesmo par em `IDLE` e disparar DUAS
entradas para a mesma oportunidade.

Corrigido com o conjunto `_busy`, marcado de forma **síncrona** (sem nenhum
`await` entre o teste e o `add`) — é isso que o torna eficaz num loop de
eventos. Teste: `test_duas_fontes_de_preco_simultaneas_nao_abrem_posicao_dobrada`.

Lição: num loop de eventos, guarda de exclusão precisa ser marcada antes do
primeiro `await`; qualquer `await` entre verificar e marcar reabre a janela.

### 14. Ordem IOC parcial descartada como "não preencheu"
Introduzido junto com a correção do bug 11 e pego pelos testes antes de ir a
produção. Uma ordem IOC parcialmente preenchida tem o restante **cancelado**,
e termina com status `CANCELED` (spot) / `state=4` (futures) — **com um
preenchimento real e válido dentro**.

Os confirmadores de fill existentes tratavam esses status como "terminou sem
preencher" e devolviam `None`. Isso faria o bot ignorar uma compra que existe
de verdade na MEXC, que é a pior classe de erro possível aqui.

Corrigido em `_wait_spot_fill` e `_wait_futures_fill`: status terminal com
quantidade preenchida > 0 é um fill legítimo.

Lição: ao trocar o tipo de ordem, revisar todo o código que interpreta o
CICLO DE VIDA da ordem, não só o que a envia. O tipo novo tem estados
terminais que o antigo nunca produzia.

### 15. Saída com UMA perna declarada como operação concluída (a mais perigosa)
Operação real de JIMOTHY, 05/08/2026 00:43:26. A perna Spot vendeu 800,4
unidades normalmente. A perna de Futures fez 3 tentativas IOC **e** o
escalonamento a mercado, e fechou **ZERO contratos**. O bot então:

- registrou `exit_live` como saída concluída, com "realizado +1,08%";
- limpou a posição do runtime e do banco;
- reportou `net_pct: 0.47%` — resultado positivo.

Nada disso era verdade. Restou um **short descoberto de 800 JIMOTHY** — sem o
spot que o protegia, porque o hedge tinha acabado de ser vendido. O usuário só
percebeu por acaso e fechou na mão 6 minutos depois. Nesses 6 minutos a
posição era uma aposta direcional pura; deu certo por sorte (+0,043 USDT no
total real), mas uma alta de 20% ali teria custado mais que 15 operações
lucrativas.

Três defeitos independentes, todos corrigidos:

1. **Verificação assimétrica.** O código só checava `spot_closed_qty <= 0`.
   Não havia nenhuma verificação equivalente para a perna de Futures. Agora a
   checagem é simétrica (`spot_leg.complete` e `futures_leg.complete`), e
   qualquer perna pendente leva a `PAUSED_ERROR` com o resíduo preservado.
2. **Fallback silencioso fabricando preço.**
   `futures_avg_price = futures_leg.avg_price or executable.futures.avg_price`
   fazia uma perna que não executou parecer executada: o evento gravou
   `exit_futures_price: 0.004216` para uma ordem que nunca preencheu, e o
   "+1,08% realizado" foi calculado contra esse preço inventado. Preço
   realizado agora é `None` quando não houve fill — é a mesma lição do bug 7,
   reaparecida em outro lugar.
3. **Kill switch cego para o estado problemático.** Ele só iterava
   `PairState.OPEN` — justamente o estado em que uma posição quebrada NÃO
   está. Um par em `PAUSED_ERROR` com short aberto era ignorado pelo botão de
   emergência, exatamente quando ele mais importa. Agora cobre `OPEN`,
   `ENTERING`, `EXITING` e `PAUSED_ERROR` com quantidade registrada.

Além disso, a mensagem de erro passou a carregar o que a **MEXC** responde
(`_describe_exchange_position`), não o que o bot acredita: o estado interno é
uma crença, a exchange é o fato, e quando divergem só o fato serve.

**A causa raiz da falha das ordens de Futures não pôde ser determinada**: as
mensagens de erro só existiam no buffer de log em memória (101 linhas), que
rotacionou. Por isso `spot_errors`/`futures_errors` agora vão para o histórico
persistente em TODOS os eventos de saída, inclusive os bem-sucedidos.

Lição: um sistema que move dinheiro precisa tratar "não consegui" e
"consegui" como caminhos igualmente explícitos. O pior estado possível não é
o erro — é o erro que se apresenta como sucesso, porque desliga toda a
vigilância humana justamente quando ela é necessária.

### 16. Rate limit de ordens da MEXC (erro 510) matando o retry e o escalonamento
Causa raiz do incidente 15, determinada em 05/08/2026 pelo histórico de
ordens da própria MEXC (`/api/v1/private/order/list/history_orders`):

```
04/08 13:43:07  side=3 type=3 vol=8 dealVol=8  0.006068  FILL    err=0
04/08 14:41:00  side=2 type=3 vol=8 dealVol=8  0.006459  FILL    err=0
04/08 15:38:44  side=2 type=3 vol=7 dealVol=7  0.006097  FILL    err=0
05/08 00:43:25  side=2 type=3 vol=8 dealVol=0  0.004228  CANCEL  err=18
05/08 00:43:25  side=2 type=3 vol=8 dealVol=0  0.004228  CANCEL  err=18
05/08 00:49:06  side=2 type=5 vol=8 dealVol=8  0.004211  FILL    err=0   <- fechamento manual
```

Duas coisas, ambas confirmadas experimentalmente contra a API real:

**`errorCode 18` = IOC cancelada sem preenchimento.** Verificado enviando uma
IOC deliberadamente inexecutável (compra 5% abaixo do bid): voltou
`state=4, dealVol=0, errorCode=18`. Ou seja, as duas ordens FORAM aceitas e o
motor de matching não achou nada dentro do preço-limite.

Por quê: o book de futures de JIMOTHY tem bid/ask separados por **0,57%**,
quase o dobro do teto de slippage de 0,30%. O limite calculado (0,004228)
mal alcançava a primeira camada de ask; bastou a cotação do topo piscar entre
o snapshot de profundidade e a ordem chegar para não haver nada a executar.

**Erro 510 = rate limit**, e era ele que impedia a recuperação. Reproduzindo
a cadência antiga (`attempt_delay_s=0.15`):

```
t=0.00s  tentativa 1: ACEITA
t=0.56s  tentativa 2: ACEITA
t=1.71s  tentativa 3: REJEITADA -> erro 510 "Requests are too frequent"
t=2.16s  tentativa 4: ACEITA
```

O endpoint de ordens de futures aceita ~2 ordens a cada ~2s. Isso explica a
lacuna entre o log (`futures_attempts: 3`) e o histórico da MEXC (2 ordens):
a terceira tentativa e o escalonamento a mercado **nunca viraram ordem**.

Correções:
- `attempt_delay_s` padrão de 0,15s → **1,0s** (`MEXC_BOT_ATTEMPT_DELAY_S`).
- `is_rate_limited()` distingue 510/429/-1003 de erro real. Rate limit é
  **reenviado com espera crescente sem consumir uma tentativa de preço** —
  "pedi rápido demais" e "o book recusou meu preço" pedem reações opostas.
- O escalonamento a mercado ganhou política própria, mais insistente
  (6 reenvios, espera maior): ele é a rede de segurança contra terminar com
  uma perna aberta, e desistir dele custa muito mais caro que esperar.
- Aviso automático quando o spread interno do book passa do dobro do teto de
  slippage — a condição que faz as IOC não preencherem.

Lição: correr mais rápido que o rate limit não é ser rápido, é não enviar
ordem nenhuma. Toda política de retry precisa conhecer o limite de
frequência do endpoint que ela chama, e distinguir recusa-por-preço de
recusa-por-frequência — tratá-las igual queima tentativas sem nunca ter
chegado à exchange.


### 17. A MEXC spot NAO tem IOC (a que mais descasou as pernas)
Operacao real de JIMOTHY, 09/08/2026. Historico da propria conta:

```
17:36:11  BUY  LIMIT   timeInForce=None  qty=1100.55  executed=1100.55
17:36:20  BUY  MARKET  timeInForce=None  qty=1100.55  executed=350.64
(o fill da LIMIT so aconteceu as 17:36:44, como MAKER)
```

O bot mandava `type=LIMIT` + `timeInForce=IOC`. A MEXC spot **aceita e ignora**
`timeInForce`: o `orderTypes` do `exchangeInfo` lista apenas LIMIT, MARKET e
LIMIT_MAKER. A ordem virou GTC e ficou viva no book.

Cadeia do estrago:
1. LIMIT de 1100,55 fica pendurada; o bot espera 6s, nao confirma, DESISTE.
2. Escalona para MARKET de 1100,55 — mas o saldo esta travado pela LIMIT viva,
   e so 350,64 preenchem.
3. Bot registra `spot_qty: 350.64` e REVERTE 7 dos 11 contratos de futures,
   por achar que o spot nao preencheu.
4. 33 segundos depois a LIMIT abandonada preenche sozinha: +1100,55.

Resultado: 1451,19 comprados no spot contra 400 vendidos no futures. Mais de
mil unidades de exposicao comprada que ninguem decidiu ter.

Correcoes:
- `limit_then_cancel`: coloca, espera pouco, **CANCELA**, e so entao le o
  estado final. O cancelamento e parte da primitiva, nao limpeza.
- Mesma protecao no futures: ordem nao confirmada e cancelada e RELIDA antes
  de o motor desistir dela.
- `new_order_limit_ioc` removido — manter o nome convidaria a reintroducao.

Licao: **uma ordem nao confirmada nao e uma ordem que nao executou.** Enquanto
ela existir, pode preencher — e vai preencher exatamente quando o bot ja
decidiu outra coisa. Nunca abandone uma ordem: cancele, releia, e so entao
decida.

Sub-licao: nunca assumir que um parametro aceito foi honrado. A MEXC nao
rejeitou `timeInForce=IOC`; ela o ignorou em silencio. Confirme o
comportamento no `exchangeInfo` (`orderTypes`) ou no eco da propria ordem.

### 18. Decisao venue-aware com execucao MEXC-only
Encontrado na revisao completa de 09/08/2026, antes de causar prejuizo.

Quando o bot ficou multi-exchange, `fetch_books` passou a ler o book do venue
CONFIGURADO — mas `_spot_send`/`_futures_send` continuaram usando
`self.spot_client`/`self.futures_client` (clientes da MEXC) e montando simbolo
no formato da MEXC. `bot/venue_trader.py` foi escrito para resolver isso e
NUNCA foi ligado.

Um par configurado para Gate x Gate decidiria com o book da Gate e mandaria a
ordem para a MEXC. Nao seria hedge nenhum: dois instrumentos sem relacao.

So nao virou prejuizo porque o modo LIVE exige credencial dos dois venues e
nao ha chave da Gate — mas era uma arma carregada para o dia em que houvesse.

Correcao: `EXECUTABLE_VENUES` trava a execucao em `mexc:spot`/`mexc:futures`,
recusando em TRES pontos (configuracao, entrada, saida) e tambem em SIMULACAO
— uma simulacao que finge operar um venue inexecutavel valida uma estrategia
que nao pode rodar e da confianca falsa. Ampliar essa lista sem ligar o
`venue_trader` reintroduz o bug.

Licao: quando uma capacidade e adicionada em camadas, a camada de DECISAO e a
de EXECUCAO podem divergir sem nenhum erro aparecer — os testes de decisao
passam, os de execucao passam, e a combinacao esta errada. Toda capacidade
nova precisa de um teste que va da decisao ate a ordem enviada.

### 19. A camada multi-exchange escrita e nunca executada
Encontrados em 13/08/2026, ao LIGAR o `venue_trader` que o bug 18 tinha
deixado desconectado. Três defeitos, nenhum detectável sem tentar usá-lo —
código nunca executado é código não testado, por mais revisado que pareça.

**19a. Referência a um método que não existia mais.** `MexcSpotTrader` chamava
`self.client.new_order_limit_ioc(...)`, removido meses antes na correção do
bug 17. A primeira ordem levantaria `AttributeError`. É a prova mais direta de
que o módulo nunca rodou: nenhum teste o exercitava de ponta a ponta, só as
partes isoladas.

**19b. Compra a mercado na Gate com a unidade errada (o mais caro).** Nas três
exchanges:

```
MEXC spot   compra e venda em quantidade da MOEDA BASE
BingX spot  compra e venda em quantidade da MOEDA BASE
Gate spot   VENDA em moeda base, COMPRA em USDT   <-- assimétrico
```

`GateSpotTrader.open_buy_leg` passava `base_qty` para `spot_market`. Numa
memecoin a 0,004 USDT, pedir 1000 unidades gastaria **1000 USDT em vez de 4** —
250x a posição decidida, sem rejeição nenhuma, porque o número é válido.

Corrigido separando em `spot_market_buy(amount_quote)` e
`spot_market_sell(amount_base)`: a unidade está no NOME do parâmetro, e um
`spot_market(side, amount)` genérico aceitaria o número errado calado. Mesmo
motivo de `open_short`/`close_short` existirem em vez de o chamador montar o
sinal. `run_leg` exige `ref_price` para converter e RECUSA se não tiver — sem
preço de referência não há conversão, e chutar o valor de uma ordem a mercado é
o oposto de tudo que este projeto faz.

**19c. Taxa da MEXC aplicada a operação na Gate.** `fees_for` lia
`spot_specs`/`contract_specs`, ambos da MEXC, independentemente do venue. A
Gate cobra 0,075% de taker no futures contra 0,02% da MEXC — 3,75x. Numa
estratégia cuja margem vive entre 1% e 3%, quatro pernas com a taxa errada
mudam o SINAL do resultado esperado: o bot entraria convicto numa operação que
perde dinheiro por construção. Agora a taxa vem do `ContractSpec` do venue
configurado.

Lição: um módulo escrito, revisado e coberto por testes unitários ainda pode
nunca ter rodado. O teste que importa é o que vai da DECISÃO até a ORDEM
ENVIADA — `test_par_na_gate_manda_a_ordem_para_a_gate_e_nao_para_a_mexc`
verifica em qual CLIENTE a ordem aterrissou, que é a pergunta que nenhum teste
de camada isolada faz.

## Funcionalidades importantes já implementadas

- Modo foco: ao ligar qualquer par no bot, o sistema para de processar
  os ~580 pares do Dashboard e concentra tudo nos poucos pares do bot
  (menos subscrições WS, menos processamento). Desliga sozinho quando o
  último par é desligado ou no kill switch. Ver `set_focus_mode` em
  `engine.py`, `mexc_ws_spot.py`, `mexc_ws_futures.py`.
- Kill switch: fecha todas as posições a mercado, em paralelo
  (`asyncio.gather`), cancela ordens pendentes, move todos os pares para
  `MANUAL_HALT`. Trava com confirmação de duplo clique na UI.
- Saída paralela e a mercado: por pedido explícito do usuário ("preciso
  que o bot saia o mais rápido possível"), a saída não tenta ordem
  limite - vai direto a mercado, com as duas pernas disparadas em
  paralelo (não sequencial). Isso é uma troca deliberada de precisão-de-
  preço por velocidade.
- Modo SIMULATION vs LIVE: travado por `ExecutionMode`, exigido no
  construtor do `ArbitrageBotEngine`. LIVE exige clientes reais passados
  explicitamente e uma variável de ambiente `MEXC_BOT_LIVE_MODE=true` no
  `.env` - duas camadas deliberadas, nenhuma acionável só pela UI.
- Histórico com spread real vs spread de tela: cada evento de
  entrada/saída grava tanto o spread calculado dos preços de fill reais
  quanto o que estava na tela no momento da decisão - a diferença entre
  os dois é o custo de slippage daquela operação específica.
- Decomposição completa do slippage no histórico (desde a correção do bug
  11). Cada evento agora grava os TRÊS números, não dois, o que permite
  atribuir a diferença em vez de só constatá-la:
  - `*_signal_pct`: topo do book, o que disparou o gatilho;
  - `*_executable_pct`: o que a profundidade projetou para o tamanho;
  - `*_spread_pct`: o que de fato executou, dos fills.

  Com eles saem duas métricas de diagnóstico: `depth_cost_pct` (o quanto o
  topo do book estava mentindo) e `execution_slippage_pct` (o quanto se
  perdeu entre a projeção e o fill). A primeira grande significa book raso
  para o seu tamanho; a segunda grande significa mercado se movendo rápido
  demais ou teto de slippage folgado demais. São problemas diferentes, com
  soluções diferentes - e antes eram indistinguíveis.
- Contador de recusas por profundidade (`depth_rejections`, exposto em
  `/api/bot/health`): quantas vezes o topo do book prometeu um spread que a
  profundidade real não confirmou. Se for alto num par, o topo do book
  daquele par é sistematicamente enganoso para o seu tamanho de posição.

## Convenções e coisas a nunca reintroduzir

- Nunca usar `lastPrice` para decisão de trading. Só para exibição
  quando não há alternativa, e sempre marcado como tal.
- Nunca usar o TOPO do book para autorizar uma ordem. O topo é gatilho; quem
  autoriza é o VWAP da profundidade para o tamanho da posição (`bot/depth.py`).
- Nunca enviar ordem a mercado no caminho normal de execução. Ordem a mercado
  é ordem sem teto de preço. O caminho normal é LIMIT+IOC com teto ancorado
  no VWAP confirmado; mercado fica reservado ao kill switch, à reversão de
  perna órfã e ao escalonamento explícito e logado do resíduo.
- Nunca deixar o teto de slippage se recalcular entre tentativas de retry.
  Ancorado na decisão, absoluto, imóvel.
- Nunca casar as pernas por valor em USDT. Casar por quantidade da moeda
  base, inflando a compra do spot pela taxa cobrada na própria moeda.
- Nunca adicionar fallback silencioso em código de decisão de
  entrada/saída. Preferir "não fazer nada" com log claro.
- Nunca executar "o que couber" quando o book não comporta o tamanho:
  preenchimento parcial descasa as pernas, que é o pior estado possível
  nesta estratégia.
- Toda guarda de exclusão mútua num loop de eventos precisa ser marcada
  antes do primeiro `await` (ver `_busy` no `bot_engine.py`).
- Frescor de dado é sempre medido POR FONTE, nunca agregado - o perigo mora
  na assimetria entre os lados do book.
- Todo valor calculado que vai virar parâmetro de ordem precisa de
  arredondamento de precisão antes de sair pela API (`sizing.py`).
- Confirmação de fill sempre via polling do status real, nunca confiando
  na resposta síncrona do POST de ordem.
- Múltiplas fontes assíncronas escrevendo no mesmo estado: pensar em
  precedência explicitamente, nunca deixar "quem chegou por último vence"
  por acidente.
- SQLite: sempre `defer_commit` em loops sobre muitos itens, um commit
  agrupado no final.
- O usuário é brasileiro, comunica em português, e prefere respostas
  técnicas diretas com números concretos (ex: "ganho de 26.7x") em vez de
  descrições vagas. Segue esse tom no código e nos comentários também -
  os comentários deste projeto são deliberadamente extensos, explicando o
  "porquê" de decisões não-óbvias, porque isso já preveniu reintrodução
  de bugs corrigidos.

## Onde procurar mais contexto

- `backend/BOT_README.md`: registro cronológico detalhado de cada fase e
  correção, com números reais medidos (benchmarks, exemplos de dados que
  causaram bugs). Mais verboso que este arquivo; útil para entender o
  raciocínio completo por trás de uma decisão específica.
- Comentários inline em `bot_engine.py`, `engine.py`, `mexc_ws_spot.py` e
  `mexc_protobuf_decoder.py`: cada decisão não-óbvia tem uma explicação
  do porquê, não só do quê.
- Docstrings de módulo em `bot/depth.py`, `bot/execution.py` e
  `bot/costs.py`: cada um abre explicando o problema real que motivou sua
  existência, com os números medidos. Leia-os antes de mexer na lógica de
  execução.
- `backend/tests/`: os testes são executáveis e também documentação. Os
  nomeados `test_caso_jimothy_*` reproduzem o episódio de 03/08/2026 com os
  números reais do banco - são a rede de segurança contra a reintrodução do
  bug 11.
- `.env.example`: cada variável de qualidade de execução vem com a
  explicação do que acontece se você aumentar ou diminuir o valor.
