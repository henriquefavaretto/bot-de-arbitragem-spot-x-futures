# Bot de Arbitragem MEXC — Fase 2: Simulação completa (dashboard + máquina de estados + interface)

Esta fase entrega o **ciclo completo de decisão** do bot — entrada, saída,
kill switch, reconexão — rodando em **modo simulação**. Nenhuma ordem real é
enviada nesta fase. O objetivo é validar toda a lógica de negócio e a
interface antes de conectar execução real (Fase 3).

## O que foi construído nesta fase

### Backend

- `bot/mexc_futures_ws_private.py` — WebSocket privado de Futures (login via
  assinatura HMAC, mesmo esquema já validado na Fase 1). Pronto para receber
  fills em tempo real quando a Fase 3 ligar a execução real.
- `bot/sizing.py` — conversão entre "valor em USDT desejado" e as unidades
  nativas de cada mercado: `quoteOrderQty` no spot (a própria MEXC calcula a
  quantidade), e cálculo de `vol` (contratos) no futures a partir do valor
  em USDT, sempre arredondando **para baixo** (nunca expõe mais capital do
  que configurado).
- `bot/bot_storage.py` — persistência SQLite separada do dashboard
  (`arb_bot.db`): configuração por par, posições abertas, e log de eventos
  (entradas, saídas, erros, kill switch).
- `bot/bot_engine.py` — a máquina de estados do bot. Estados por par:
  - `IDLE` — monitorando, sem posição
  - `ENTERING` / `EXITING` — transitórios durante execução (nesta fase são
    instantâneos, já que a simulação não tem latência real de fill)
  - `OPEN` — posição aberta, monitorando spread de saída
  - `PAUSED_ERROR` — pausado automaticamente (ex: tamanho de posição menor
    que 1 contrato) — nunca abre posição nova nesse estado
  - `MANUAL_HALT` — pausado manualmente (toggle ou kill switch)
- Integração no `main.py`: o bot recebe os mesmos preços que já alimentam o
  dashboard (via callback `on_price_update` no `engine.py` do dashboard),
  sem o dashboard depender do bot para funcionar.
- Novos endpoints REST: `/api/bot/health`, `/api/bot/pairs` (GET/POST/DELETE),
  `/api/bot/pairs/{symbol}/resume`, `/api/bot/kill-switch`, `/api/bot/events`.
- Novo WebSocket: `/ws/bot` — snapshot em tempo real do estado de cada par.

### Frontend

- Nova aba "Bot de Arbitragem" no topo, ao lado do Dashboard existente.
- Banner permanente de "MODO SIMULAÇÃO" — impossível confundir com
  execução real.
- Formulário de configuração por par: símbolo, spread de entrada, spread de
  saída, tamanho da posição em USDT.
- Tabela de status: estado atual de cada par (com cor), detalhes da posição
  aberta (spread de entrada, notional, há quanto tempo), toggle
  liga/desliga, botão de remover, botão de retomar (quando pausado).
- Kill switch: botão vermelho fixo, sempre visível, que exige um segundo
  clique de confirmação (o botão pisca "clique novamente para CONFIRMAR"
  por 4 segundos) antes de agir.

## Validações já realizadas

A máquina de estados foi testada exaustivamente de forma isolada (sem
depender de rede), cobrindo:

1. Ciclo completo de entrada e saída: spread pequeno não entra, spread
   grande entra corretamente (casando o valor notional entre as pernas),
   spread convergindo dispara a saída, e o PnL simulado é calculado
   corretamente (validado numericamente: short em futures lucra quando o
   preço cai, exatamente como esperado).
2. Proteção de tamanho mínimo: se `position_size_usdt` for pequeno demais
   para valer 1 contrato no par configurado, o bot se pausa sozinho
   (`PAUSED_ERROR`) com uma mensagem clara, em vez de tentar enviar uma
   ordem inválida.
3. Par pausado nunca reage: uma vez em `PAUSED_ERROR` ou `MANUAL_HALT`,
   novas atualizações de preço são ignoradas até você retomar manualmente.
4. Reconexão conforme especificado: com a conexão degradada, o bot NÃO
   abre posições novas — mas uma posição já aberta continua sendo
   monitorada e consegue sair normalmente quando o spread de saída é
   atingido, mesmo com a conexão instável.
5. Kill switch: fecha (simuladamente) todas as posições abertas e move
   todos os pares para `MANUAL_HALT`, desabilitando a config de cada um.
6. Integração REST completa: todos os endpoints testados de ponta a ponta
   via `TestClient` do FastAPI, incluindo validação de erro (rejeita
   configuração onde `entry_spread_pct <= exit_spread_pct`, por exemplo).

## Atualizações pós-Fase 2

- **Correção de bug**: o bot não estava reagindo a atualizações de preço
  quando o par era configurado com o sufixo `USDT` (ex: `JIMOTHYUSDT`),
  porque o Dashboard identifica os pares sem esse sufixo (`JIMOTHY`). O
  motor do bot agora normaliza automaticamente qualquer símbolo recebido
  (remove o sufixo se vier), incluindo migração automática de configs já
  salvas no formato antigo na primeira subida do backend.
- **Filtros do Dashboard não resetam mais** ao trocar de aba: antes, sair
  da aba Dashboard desmontava a tabela (perdendo busca, ordenação e
  filtros); agora os componentes de cada aba ficam sempre montados, só
  alternando visibilidade.
- **Nova aba "Histórico"**: mostra todas as operações simuladas do bot
  (entradas, saídas, falhas de tamanho, kill switch), com PnL calculado por
  operação, cards de resumo (total de operações, PnL agregado, taxa de
  acerto), filtro por par e por tipo de evento.

## Como testar

```powershell
cd backend
venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

```powershell
cd frontend
npm install
npm run dev
```

Abra `http://localhost:5173`, clique na aba "Bot de Arbitragem", e
configure um par que já apareça no Dashboard (ex: um par com spread
variável). Ajuste o spread de entrada para um valor que você veja
frequentemente cruzar no Dashboard, para observar o bot "entrar" e "sair"
em modo simulação, e confira o log de eventos em `/api/bot/events`.

## Histórico desta fase (Fase 2, arquivado)

As limitações abaixo valiam apenas durante a Fase 2 e foram resolvidas na
Fase 3, documentada logo em seguida:

- Nenhuma ordem real era enviada (`ExecutionMode` travado em `SIMULATION`).
- Os estados `ENTERING`/`EXITING` eram instantâneos.
- Não existia reconciliação de "perna solta".

---

# Fase 3: Execução real

Esta fase liga o bot à sua conta MEXC de verdade. É a parte mais sensível
do projeto — dinheiro real se move a partir daqui quando o modo LIVE está
ativo. Tudo foi construído com múltiplas camadas de proteção deliberadas.

## Correção crítica: entrada com spread negativo

Antes desta fase, o bot entrava em posição tanto com spread positivo quanto
negativo (usava `abs(spread_pct) >= entry_spread_pct`). Isso estava errado:
a estratégia (compra Spot + vende Futures) só é lucrativa na convergência
quando o spread é **positivo** (Futures > Spot). Com spread negativo, a
mesma operação trava prejuízo garantido. Corrigido para exigir
`spread_pct >= entry_spread_pct` (sem valor absoluto), e a configuração
agora rejeita `entry_spread_pct` menor ou igual a zero.

## Como a execução real funciona

### Entrada (perna âncora + perna espelho)

1. **Perna âncora — Futures a mercado** (abre short, `side=3`). Define o
   valor notional **real** executado, já que o preço de fill pode divergir
   do preço de referência usado para calcular a quantidade de contratos.
2. **Confirmação de fill**: o bot faz polling do status da ordem via REST
   (endpoint `get_order`) até confirmar o preenchimento total, com timeout.
   Se não confirmar a tempo, o par é pausado com um aviso explícito para
   **verificação manual** — o bot nunca assume que uma ordem sem
   confirmação foi bem-sucedida.
3. **Perna espelho — Spot a mercado via `quoteOrderQty`**, casada
   exatamente pelo valor notional real da perna âncora (não pelo valor
   configurado original).
4. **Reversão automática**: se a perna Spot falhar por qualquer motivo
   (saldo insuficiente, erro de rede, etc.), o bot fecha a perna Futures
   já aberta **imediatamente, a mercado**, e pausa o par em `PAUSED_ERROR`
   com uma mensagem clara. Isso evita ficar com exposição direcional não
   intencional (só Futures aberto, sem o Spot correspondente).

### Saída (fechamento com retry até 100%)

Conforme especificado: as duas pernas são fechadas com **ordem limite** no
preço de referência atual. Se não preencher totalmente, o bot cancela e
reenvia uma nova ordem limite pelo **saldo restante**, repetindo até
fechar 100% da posição ou esgotar um número máximo de tentativas — nesse
caso extremo, força um fechamento a mercado como último recurso, para
nunca deixar a perna aberta indefinidamente.

### Limite global de exposição

Soma o notional de todas as posições abertas (`OPEN`/`ENTERING`/`EXITING`)
em todos os pares. Uma nova entrada que ultrapassaria o teto configurado é
bloqueada automaticamente — o par simplesmente não entra naquele ciclo,
sem gerar erro (ele continua tentando nos próximos updates de preço, caso
outra posição feche e libere espaço).

### Kill switch em modo LIVE

Cancela todas as ordens pendentes e fecha todas as posições abertas **a
mercado** (prioriza velocidade sobre preço — é parada de emergência), nas
duas pernas de cada par, e move tudo para `MANUAL_HALT`.

## Como ativar o modo LIVE (duas travas deliberadas)

**Trava 1 — variável de ambiente explícita.** Em `backend/.env`:

```
MEXC_BOT_LIVE_MODE=true
MEXC_BOT_MAX_TOTAL_EXPOSURE_USDT=500
MEXC_SPOT_API_KEY=sua_chave_aqui
MEXC_SPOT_SECRET_KEY=seu_secret_aqui
MEXC_FUTURES_API_KEY=sua_chave_aqui
MEXC_FUTURES_SECRET_KEY=seu_secret_aqui
```

Isso exige edição manual de arquivo — não existe nenhum botão na interface
que ative isso sozinho. Se `MEXC_BOT_LIVE_MODE=true` mas as credenciais
estiverem incompletas, o backend recusa e cai em modo SIMULAÇÃO
automaticamente, com um aviso crítico no log.

**Trava 2 — toggle por par continua manual.** Ativar o modo LIVE global
não liga sozinho nenhum par: você ainda precisa marcar "Ligado" em cada
par na aba "Bot de Arbitragem", exatamente como já funcionava na
simulação. Como você pediu, uma vez em modo LIVE, religar um par pela
interface já é suficiente (não pede confirmação extra por par).

Quando LIVE está ativo, a interface mostra um banner vermelho pulsante
"MODO LIVE — DINHEIRO REAL" com a exposição atual e o teto configurado,
substituindo o banner amarelo de simulação.

## Validações já realizadas (com clientes mockados, sem gastar nada)

1. **Entrada bem-sucedida**: confirmado que a ordem Futures (âncora) é
   enviada primeiro, o valor da ordem Spot (espelho) é calculado a partir
   do notional **real** do fill (não do configurado), e os side codes
   estão corretos (`side=3` para abrir short).
2. **Reversão automática**: simulei a perna Spot falhando após o Futures
   já ter executado — confirmado que o bot envia uma segunda ordem Futures
   de fechamento (`side=2`, `reduce_only=True`) automaticamente, e pausa o
   par com mensagem explicando o que aconteceu.
3. **Fechamento com preenchimento parcial**: simulei uma ordem limite que
   preenche só metade do volume — confirmado que o bot detecta, cancela, e
   reenvia uma segunda ordem para o restante, fechando 100% com o preço
   médio ponderado calculado corretamente.
4. **Trava de ativação**: testado que `MEXC_BOT_LIVE_MODE=true` sem
   credenciais completas cai para SIMULAÇÃO; com credenciais preenchidas
   (mesmo que inválidas na API real), o modo LIVE é corretamente ativado
   internamente — a validação das chaves em si só acontece quando uma
   ordem de verdade é enviada.
5. **Limite global de exposição**: confirmado bloqueando a segunda entrada
   quando a soma ultrapassaria o teto configurado.

## Antes de operar com dinheiro real: checklist

- [ ] Confirme que a permissão de **saque está desabilitada** nas suas API
      keys da MEXC (o `test_credentials.py` da Fase 1 avisa isso).
- [ ] Comece com um `MEXC_BOT_MAX_TOTAL_EXPOSURE_USDT` baixo (ex: 20-50
      USDT) e um único par configurado, para observar o comportamento real
      antes de aumentar a exposição.
- [ ] Verifique que há saldo suficiente tanto no Spot quanto no Futures
      (para margem) antes de ligar um par.
- [ ] Acompanhe a aba "Histórico" após as primeiras operações reais para
      confirmar que o PnL calculado bate com o que você vê na própria
      MEXC.
- [ ] Saiba onde fica o botão de Kill Switch antes de precisar dele.

## O que ainda não foi feito (limitações conhecidas desta fase)

- O WebSocket privado de Futures (`bot/mexc_futures_ws_private.py`, criado
  na Fase 2) ainda não é usado como fonte primária de confirmação de fill
  — a confirmação atual é via polling REST do status da ordem, que é mais
  simples e não depende de um parser de mensagens WS 100% coberto por
  testes. Isso adiciona uma pequena latência (~1s por tentativa de poll)
  mas prioriza segurança sobre velocidade.
- Não há reconciliação automática na inicialização do backend (ex: se o
  backend cair com uma posição aberta e voltar, ele confia no que está
  salvo no SQLite; não há uma varredura ativa comparando contra o estado
  real da conta MEXC). Para uso intenso, isso seria uma extensão futura
  recomendada.
- O cálculo de PnL da perna Spot no fechamento assume que o preço médio
  de venda reportado pela MEXC está correto; não há verificação cruzada
  independente.

---

## Atualizações: interface e correção de bugs (pós-Fase 3)

### Bugs corrigidos

- **Histórico só mostrava operações simuladas.** O frontend só reconhecia
  os eventos `entry_simulated`/`exit_simulated`, então qualquer operação
  real (`entry_live`, `exit_live`) ou evento de erro/reversão ficava
  invisível na aba Histórico, mesmo estando salvo corretamente no banco.
  Corrigido: todos os 9 tipos de evento agora são reconhecidos e exibidos.
- **Remover um par não fazia efeito de verdade.** `remove_pair_config`
  removia o par de `self.configs`, mas o snapshot exibido na interface é
  gerado a partir de `self.runtimes` — que nunca era limpo. O par
  continuava aparecendo na tabela (com config vazia) mesmo após "remover".
  Corrigido: agora limpa `configs`, `runtimes` e `contract_specs` juntos,
  e também a posição salva no banco, se houver.

### Novidades

- **Saldo Spot/Futures no topo** (`/api/bot/balance`): mostra o saldo real
  da sua conta MEXC, com cache de 8s para não sobrecarregar a API.
  Funciona em modo SIMULAÇÃO ou LIVE, desde que as credenciais estejam no
  `.env` — usa somente endpoints de leitura, nunca envia ordem.
- **Spread atual na aba Bot**: nova coluna mostrando o spread em tempo
  real de cada par configurado, direto do mesmo feed que alimenta o
  Dashboard — não precisa mais alternar de aba para saber onde o spread
  está agora.
- **Data e hora completas no Histórico**: substituído "há X min/h" por
  timestamp completo (dia/mês/ano hora:min:seg).
- **Botão de apagar histórico** (`DELETE /api/bot/events`): remove todos
  os eventos registrados, com confirmação de duplo clique. Ação
  irreversível.
- **Nova aba "Logs"**: exibe em tempo real tudo que o bot está registrando
  internamente (conexões, decisões, erros, reconexões) — captado
  diretamente dos loggers Python já existentes (`bot_engine`,
  `mexc_futures_ws_private`, etc.) via um handler em memória, sem precisar
  instrumentar cada ponto de decisão manualmente. Suporta filtro por
  nível (INFO/WARNING/ERROR/CRITICAL) e botão de apagar
  (`DELETE /api/bot/logs`).

### Validação

Suite de regressão com 9 cenários cobrindo todas as fases do projeto
(bug do spread negativo, ciclo de entrada/saída, bug de remoção de par,
limite global de exposição, kill switch, normalização de símbolo,
histórico e sua limpeza) — todos passando.

---

## Correção: "Insufficient position" ao fechar a perna Spot

**Sintoma**: ao sair de uma posição, o log mostrava
`MEXC Spot API error 30004: Insufficient position` e o PnL da perna Spot
ficava zerado (`spot=0.0000`), mesmo a operação tendo entrado
normalmente.

**Causa**: a MEXC desconta a taxa de trading na própria moeda comprada
(comprar JIMOTHY com USDT gera uma taxa cobrada em JIMOTHY, não em
USDT). O bot calculava a quantidade a vender a partir do valor
executado na entrada, sem nunca confirmar quanto realmente sobrava
disponível na carteira — então a quantidade calculada ficava sempre
um pouco maior que o saldo real, e a venda era rejeitada.

**Correção**: antes de qualquer venda no Spot (saída normal, kill
switch), o bot agora consulta o saldo livre real do ativo e nunca
tenta vender mais do que existe.

---

## Mudança de estratégia: saída rápida e paralela (a mercado)

Após a correção acima, um novo problema apareceu em pares de baixa
liquidez: a saída (que tentava ordem limite por até ~10 tentativas,
~30 segundos, antes de forçar mercado) ficava presa tempo demais
tentando preencher a ordem limite de Futures, e só depois começava a
fechar o Spot — nesse intervalo o spread já tinha se movido bastante.

**Mudança**: a saída agora:

1. Fecha as duas pernas (Spot e Futures) **a mercado direto**, sem
   nenhuma tentativa de ordem limite antes — prioriza velocidade sobre
   uma possível pequena melhora de preço.
2. Dispara as duas pernas **em paralelo** (`asyncio.gather`), não uma
   depois da outra. Isso corta o tempo total de exposição durante a
   saída praticamente pela metade.
3. Mantém a proteção de saldo real (não tenta vender mais Spot do que
   existe de fato disponível).

O kill switch também foi atualizado para usar essa mesma lógica
paralela e mais rápida.

**Validação**: testado com clientes mockados simulando latência de
rede — confirmado que o tempo total da saída fica próximo ao tempo da
perna mais lenta isolada (não a soma das duas, como seria no fluxo
sequencial anterior), e que cada perna manda exatamente 1 ordem a
mercado, sem retries de limite.


---

## Correção: entrada não confirmava o fill real da perna Spot

**Sintoma**: uma operação entrava e saía normalmente (sem erro), mas o
histórico mostrava "Preço Spot: —" tanto na entrada quanto na saída, e
o PnL da saída considerava só o lado Futures (`spot=0.0000`), gerando
um resultado que não refletia a operação completa.

**Causa**: depois de enviar a ordem de compra a mercado no Spot
(`new_order_market_by_quote`), o bot confiava direto no campo
`executedQty` da resposta imediata do POST. Em mercados menos líquidos,
essa resposta pode retornar antes de a MEXC processar o fill
internamente - vindo com `executedQty=0` mesmo a compra tendo (ou indo)
acontecer de verdade. O bot registrava a posição como aberta, mas com
`entry_spot_qty=0`, então a saída não tinha o que vender.

**Correção**: se a resposta imediata vier com `executedQty=0`, o bot
agora consulta o status real da ordem (mesmo padrão de confirmação já
usado do lado Futures) até confirmar o preenchimento. Se o fill nunca
for confirmado dentro do timeout, a perna Futures já aberta é revertida
automaticamente e o par é pausado com aviso para verificação manual -
o mesmo tratamento que já existia para quando a ordem Spot falha com
uma exceção explícita.

**Se você foi afetado por esse bug antes da correção**: é provável que
exista saldo do ativo (ex: JIMOTHY) parado na sua carteira Spot, comprado
mas não vendido pela saída malsucedida. Vale conferir manualmente na
MEXC e vender esse saldo residual, se houver.

**Validação**: testado com mock simulando resposta imediata vazia seguida
de confirmação real via polling (confirma o fill correto), e com mock
simulando fill que nunca confirma (confirma que a perna Futures é
revertida automaticamente).

---

## Correção: "amount scale is invalid" ao vender no Spot, e falha silenciosa mascarando saídas incompletas

**Sintoma**: mesmo após a correção anterior de confirmação de fill, o
histórico continuou mostrando "Preço Spot: —" em toda saída, e ocasionalmente
o log mostrava `MEXC Spot API error 400: amount scale is invalid`.

**Causa raiz (duas partes)**:

1. **Falta de arredondamento de precisão.** A MEXC exige que a quantidade
   de uma ordem respeite um número máximo de casas decimais por símbolo
   (`baseAssetPrecision`). O saldo real consultado via API frequentemente
   vem com imprecisão de ponto flutuante (ex: `1188.70000001` em vez de
   `1188.7`), e o bot mandava esse valor bruto direto para a MEXC, que
   rejeitava a ordem inteira com HTTP 400 - **antes de qualquer venda
   acontecer**.
2. **A falha era engolida silenciosamente.** Quando a venda falhava (por
   esse motivo ou qualquer outro), o código capturava a exceção, logava
   como `CRITICAL`, mas retornava `(0.0, reference_price)` como se nada
   de errado tivesse acontecido. O evento `exit_live` era registrado como
   se a operação tivesse fechado normalmente, só que com o PnL considerando
   apenas o lado Futures - por isso toda saída aparecia com
   `spot=0.0000`, mesmo quando a causa raiz variava.

**Correção**:

1. Adicionado `round_spot_quantity()` em `sizing.py`, que arredonda
   sempre para BAIXO na precisão exigida pelo símbolo (nunca para cima,
   para não arriscar vender mais do que existe). O bot agora carrega os
   metadados de precisão de cada par via `GET /api/v3/exchangeInfo`
   (novo método `get_exchange_info` no cliente Spot) na inicialização e
   ao configurar um par novo - o mesmo padrão já usado para os metadados
   de contrato Futures.
2. `_close_spot_market` não engole mais exceções - propaga para o
   chamador.
3. `_execute_exit_live` agora: se a venda no Spot falhar, tenta
   **uma segunda vez** (resolve a maioria dos casos, já que reconsulta e
   rearredonda o saldo do zero). Se falhar de novo, o par vai para
   `PAUSED_ERROR` com uma mensagem explícita e visível na aba Bot e no
   Histórico (novo evento `exit_spot_leg_failed`), preservando os dados
   de entrada para você conferir manualmente na MEXC - em vez de
   silenciosamente registrar a operação como concluída.

Também foi corrigido, no processo, um bug secundário introduzido pela
própria tentativa inicial de correção: a variável de controle de erro não
era resetada corretamente no caminho de sucesso, fazendo até saídas bem-
sucedidas caírem no fluxo de "pausar por erro". Coberto por teste
automatizado específico.

**Se você foi afetado por esse bug antes desta correção**: as operações
listadas no seu histórico com `spot=0.0000` provavelmente deixaram saldo
do ativo (ex: JIMOTHY) parado na carteira Spot, nunca vendido. Vale
conferir manualmente na MEXC.

**Validação**: testado com mocks reproduzindo o erro exato
(`amount scale is invalid` por imprecisão de saldo), confirmando que o
arredondamento resolve o problema na maioria dos casos; testado o
caminho de sucesso (não deve mais cair em `PAUSED_ERROR` por engano); e
testado o caminho de falha real e persistente (deve pausar de verdade,
com mensagem clara).

---

## Correção: mesma falha de confirmação de fill também acontecia na SAÍDA (não só na entrada)

**Sintoma**: mesmo após as duas correções anteriores, o log mostrou
`Ordem de venda Spot em JIMOTHYUSDT foi aceita mas executedQty=0 na
resposta` seguido de retry - e o histórico real de ordens da MEXC
revelou que **a venda tinha acontecido de verdade duas vezes** (o bot
vendeu, achou que tinha falhado, tentou de novo, vendeu de novo), antes
de finalmente pausar o par achando que a segunda tentativa também
falhou (quando na verdade só não sobrava mais nada para vender, porque
as duas vendas anteriores já tinham dado conta de quase tudo).

**Causa**: a correção de "confirmar fill via polling antes de desistir"
(`_wait_spot_fill`) tinha sido aplicada apenas na função de **entrada**
(`_execute_entry_live`). A função de **saída a mercado**
(`_close_spot_market`) continuava confiando cegamente em `executedQty`
da resposta imediata do POST, levantando exceção assim que via 0 - o
mesmo problema de fundo (a MEXC pode responder ao POST antes do fill
estar refletido), só que numa parte diferente do código que eu não
tinha coberto antes.

**Correção**: `_close_spot_market` agora usa a mesma função
`_wait_spot_fill` antes de declarar falha - exatamente o mesmo padrão
já usado na entrada. Só declara erro de verdade se nem a resposta
imediata nem o polling de status confirmarem o preenchimento.

**Bônus - `resume_from_halt` agora limpa os dados residuais**: ao
retomar um par pausado por erro, o bot limpa os dados de posição
(quantidades, preços, e também `last_error`, que estava sendo esquecido
antes) - a expectativa é que você já tenha conferido manualmente na
MEXC antes de retomar. A interface agora pede confirmação explícita ao
clicar em "Retomar", lembrando dessa verificação.

**Validação**: testado reproduzindo o cenário exato do log (resposta
imediata com executedQty=0, fill real confirmado só na consulta de
status) - confirma que agora fecha com 1 única venda, sem duplicar.
Testado também o caminho de falha real (nunca confirma) - continua
pausando corretamente. Suite de regressão completa revalidada.

---

## Correção: "amount scale is invalid" também na ENTRADA (compra via quoteOrderQty)

**Sintoma**: mesmo par que antes tinha o erro na saída, passou a
apresentar o mesmo erro (`amount scale is invalid`) na entrada, impedindo
o bot de abrir posição - a perna Futures executava e era revertida
automaticamente logo em seguida.

**Causa**: o valor de `real_notional` (quanto gastar em USDT na compra
Spot) é calculado a partir do fill real do Futures
(`fill["filled_vol"] * fill["avg_price"] * contractSize`) - uma
multiplicação de números de ponto flutuante que frequentemente gera mais
casas decimais do que a MEXC aceita para o parâmetro `quoteOrderQty`
daquele símbolo (ex: `9.316945000000001` em vez de `9.3169`). Esse é o
mesmo tipo de problema já corrigido para a quantidade de venda, só que
do lado do valor de compra, que não tinha sido coberto ainda.

**Correção**: adicionada `round_spot_quote_qty()` em `sizing.py`,
simétrica a `round_spot_quantity()`, mas arredondando pela precisão da
moeda de cotação (`quotePrecision`, USDT) em vez da moeda base. Aplicada
ao `real_notional` antes de enviar a ordem de compra na entrada. Sem a
spec do símbolo carregada ainda, usa um arredondamento conservador de 2
casas decimais como rede de segurança.

**Validação**: testado reproduzindo o cenário exato (valor com
imprecisão de float sendo rejeitado por um mock que valida a precisão
máxima permitida) - confirma que a entrada agora funciona normalmente.
Testado também o fallback sem spec carregada. Suite de regressão
completa revalidada.

---

## Nova funcionalidade: WebSocket de Spot com validação cruzada (substituindo polling REST de 3s)

### Motivação

O Dashboard calculava o spread usando o preço Spot obtido via polling REST
a cada 3 segundos, enquanto o preço Futures já vinha via WebSocket
(~2s). Essa defasagem (até 3s de "atraso" no lado Spot) contribuía para
a diferença observada entre o "spread de tela" (usado para decidir
entrada/saída) e o spread de execução real. Esta atualização migra o
preço Spot para WebSocket também, eliminando essa defasagem.

### Por que isso exigiu cuidado extra

O WebSocket público de Spot da MEXC usa Protocol Buffers (protobuf), não
JSON. Durante a pesquisa, encontramos um relato técnico confiável
reportando que a documentação oficial de `.proto` da MEXC está
desatualizada/incorreta em alguns pontos - especificamente, o número de
campo exato usado no `oneof body` do wrapper de mensagens não pôde ser
confirmado com confiança suficiente por fontes independentes
concordantes. Um número de campo errado faria o parser falhar
silenciosamente (o pior cenário possível: preços errados sem nenhum erro
visível, alimentando decisões de entrada/saída do bot).

### Como isso foi mitigado

1. **Decodificação genérica, sem depender do número exato do campo
   `oneof`** (`bot/mexc_protobuf_decoder.py`): em vez de assumir qual
   número de campo corresponde a `publicBookTicker`, o decodificador varre
   genericamente a estrutura binária (wire-format) e testa cada campo do
   tipo "length-delimited" como candidato, validando o resultado (preços
   numéricos, positivos, `ask >= bid`) antes de aceitar. Qualquer coisa
   que não bata com esse formato é descartada.

2. **Validação cruzada automática contra REST** (`mexc_ws_spot.py`): o
   preço vindo do WebSocket só é usado depois de **5 amostras
   consecutivas** ficarem dentro de 0.5% do preço REST (fonte já
   validada desde a Fase 1). Continua revalidando periodicamente (a cada
   60s) mesmo depois disso - nunca é "confia uma vez e esquece". Se a
   diferença ultrapassar a tolerância, o símbolo perde a confiança
   imediatamente e volta a usar REST.

3. **REST nunca para de rodar**: mesmo com o WebSocket confiável para um
   símbolo, o polling REST de 3s continua ativo em segundo plano - serve
   de fallback automático e de referência contínua de validação.

4. **Indicador visual na interface**: a coluna "Preço Spot" do Dashboard
   mostra um ícone ⚡ ao lado do valor quando aquele par está usando o
   WebSocket (já validado); sem o ícone, está usando REST (ainda
   validando, ou o WS perdeu a confiança).

### Validação realizada

Testado com mensagens protobuf reais, construídas usando a biblioteca
oficial do Google (não bytes inventados à mão):
- Decodificação correta de uma mensagem válida, mesmo com o campo do
  `oneof` em um número arbitrário (simulando não saber o número real).
- Rejeição correta de: bytes aleatórios, preços negativos, livro
  invertido (ask < bid), mensagens sem bookTicker presente.
- Fluxo end-to-end completo (bytes protobuf → decodificação → validação
  cruzada → atualização do spread no engine).
- Confirmação de que um preço divergente (simulando erro de
  parsing) é rejeitado e **não contamina** o estado do engine - o spread
  continua refletindo o último valor confiável.

**Isso não pôde ser testado contra o servidor real da MEXC** (o ambiente
de desenvolvimento não tem acesso de rede à MEXC). Por isso a camada de
validação cruzada existe: mesmo que o parser tenha algum caso extremo não
coberto pelos testes, a validação contra REST deve pegar isso na
prática, mantendo o símbolo em modo REST (mais lento, mas correto) em vez
de aceitar dados suspeitos.

### O que observar nas primeiras execuções reais

- Acompanhe os logs (aba "Logs") por mensagens `WS de Spot para <par>
  agora está: confiável` - isso confirma que a validação passou.
- Se vir repetidamente `Validação do WS de Spot falhou para <par>`, isso
  indica que o parser não está funcionando corretamente para aquele
  símbolo (ou há uma condição de mercado muito incomum) - nesse caso, o
  par continua funcionando normalmente via REST (nenhum risco), mas vale
  me avisar para investigar.
- O ícone ⚡ na coluna Preço Spot do Dashboard é a confirmação visual mais
  simples de que o WS está ativo e validado para aquele par.

---

## Mudança importante: spread agora usa preços de EXECUÇÃO, não "último negociado"

### O que mudou

Antes, o spread era calculado com o `lastPrice` (último preço negociado)
dos dois lados. Agora usa os preços que você **de fato executa**:

| Perna | Antes | Agora | Por quê |
|---|---|---|---|
| Spot | `lastPrice` | **`askPrice`** (melhor venda do book) | A perna Spot é sempre COMPRA - o ask é o que você paga |
| Futures | `lastPrice` | **`bid1`** (melhor compra do book) | A perna Futures é sempre VENDA - o bid é o que você recebe |

Aplicado consistentemente nas 4 fontes de preço do sistema: REST de spot,
REST de futures, WebSocket de spot (bookTicker) e WebSocket de futures
(push.tickers). Se o book vier vazio para algum par (sem liquidez no
momento), há fallback automático para `lastPrice` - melhor um preço
aproximado do que nenhum.

### Por que isso importa (com números reais)

Usando os preços reais do JIMOTHY observados na MEXC:
- ask spot = 0.007840, bid futures = 0.007890 → **spread real = 0.64%**
- lastPrice spot = 0.007754, lastPrice futures = 0.007900 → spread
  antigo = **1.88%**

Ou seja: o método antigo mostrava **1.24 pontos percentuais a mais** de
spread do que realmente existia para executar. Isso explica boa parte da
diferença que vinha aparecendo entre o "spread de tela" no momento da
decisão e o spread efetivamente realizado nas operações - o bot entrava
achando ter margem que não existia.

### Efeito prático esperado

Os spreads exibidos no Dashboard vão parecer **menores** do que antes.
Isso não é um bug nem uma piora: é o número honesto. Como consequência,
pode ser necessário **reduzir o `entry_spread_pct` configurado nos pares**
- um par que antes disparava com "3%" de spread de tela agora talvez
mostre 1.5% para a mesma condição real de mercado.

Recomendação: observe alguns ciclos no Dashboard antes de reativar o bot,
para recalibrar os níveis de entrada/saída com base nos novos valores.

---

## Correção: futures demorava a atualizar (e o bid/ask nunca chegava)

### Dois problemas encontrados

Ao investigar a lentidão relatada no lado Futures, descobri que o canal
WebSocket em uso (`sub.tickers`, agregado, todos os contratos) tinha duas
limitações confirmadas na documentação oficial da MEXC:

1. **Push a cada 2s fixos**, carregando centenas de contratos por
   mensagem - a maioria irrelevante para os pares monitorados.
2. **Não inclui `bid1`/`ask1`.** A lista oficial de campos desse canal
   só tem `lastPrice`, `fairPrice`, `maxBidPrice` e `minAskPrice` (estes
   dois últimos são limites de preço permitidos para ordens, não o topo
   do book). Ou seja: a mudança anterior para usar preço de execução no
   Futures **nunca chegou a funcionar via WebSocket** - o código caía
   sempre no fallback `lastPrice`.

### Solução: canal individual para os pares do bot

Agora o cliente usa **dois canais complementares**:

| Canal | Frequência | Tem bid/ask? | Usado para |
|---|---|---|---|
| `sub.tickers` (agregado) | 2s | Não | Todos os pares do Dashboard (base) |
| `sub.ticker` (individual) | ~1s | **Sim** | Pares configurados no bot |

Os pares configurados no bot são automaticamente promovidos a
"prioritários" e recebem subscrição individual - tanto na inicialização
quanto ao configurar um par novo pela interface. Para esses pares, o
preço passa a ser o **bid real do book** (o que se recebe ao vender), com
latência de ~1s em vez de 2s.

O limite de subscrições por conexão é de 200 (versões antigas da doc
indicavam 30, o que motivou a escolha original pelo canal agregado);
o código respeita um teto de 180 com margem de segurança, e avisa no log
se for excedido.

Para evitar regressão, o canal agregado **ignora** os símbolos
prioritários - senão sobrescreveria o preço bom (bid, 1s) pelo pior
(lastPrice, 2s) a cada push.

### Visibilidade

A coluna "Preço Futuros" do Dashboard agora mostra um ⚡ quando aquele par
está recebendo preço de execução real via canal individual (mesmo padrão
já usado na coluna Preço Spot). Sem o ícone, está usando lastPrice do
canal agregado.

### Validação

10 testes automatizados cobrindo: canal individual usando bid, canal
agregado caindo para lastPrice sem inventar bid/ask, marcação correta de
qual fonte cada símbolo usa, gerenciamento de símbolos prioritários (sem
duplicação), e tratamento de itens malformados. Mais teste de integração
confirmando que configurar um par no bot o promove automaticamente a
subscrição individual.

---

## Correção: histórico agora mostra o spread REAL da operação

### Dois problemas

**1. Preço Spot vazio nas entradas reais.** O backend gravava o campo como
`spot_fill_price` em operações reais, mas o frontend procurava por
`spot_price` (nome usado só nas simuladas). Resultado: a coluna ficava
sempre "—" em toda entrada real.

**2. O spread gravado não correspondia aos preços mostrados.** O campo de
spread guardava o **"spread de tela"** - aquele que estava visível no
momento em que o bot decidiu operar - enquanto as colunas de preço
mostravam os **fills reais** das ordens. Como são medidos em momentos
diferentes (e a execução a mercado tem slippage), os números não batiam.
Isso ficou evidente num caso onde o histórico mostrava saída com
`-3.25%` de spread, mas os preços exibidos (0.007424 spot, 0.007535
futures) davam `+1.5%`.

### Correção

O histórico agora grava e exibe **duas** medidas de spread, lado a lado:

| Coluna | O que é |
|---|---|
| **Spread real** | Calculado dos preços que efetivamente executaram (`(futures_fill - spot_fill) / spot_fill`). É o spread que de fato foi travado na operação. |
| **Spread na tela** | O valor que estava visível quando o bot decidiu operar. Guardado para comparação. |

A diferença entre as duas colunas **é o custo de slippage** daquela
operação - agora visível diretamente, operação por operação, em vez de
precisar cruzar dados manualmente com o histórico da MEXC.

O frontend também aceita os nomes de campo antigos como fallback, então
o histórico já existente continua legível em vez de mostrar "—".

### Exemplo real (do caso relatado)

| | Spread na tela | Spread real |
|---|---|---|
| Entrada | +2.58% | **+1.02%** |
| Saída | -3.25% | **+1.50%** |

Ou seja: a operação entrou achando ter 2.58% de margem, mas travou apenas
1.02% - e o "-3.25%" da saída era um número que não correspondia a nada
executável.

### Validação

8 testes automatizados cobrindo: gravação do campo de preço spot no nome
correto, cálculo do spread realizado a partir dos fills (entrada e saída),
preservação do spread de tela para comparação, e conferência numérica de
que os spreads realizados batem exatamente com os preços de fill.

---

## Correção: preço de futures não correspondia ao book (e o indicador ⚡ mentia)

### Sintoma

O Dashboard exibia um preço de futures que não batia com nenhum valor do
book real da MEXC (nem bid, nem ask, nem último negociado) - e o ícone ⚡
sugeria estar usando preço de execução quando não estava.

### Causas encontradas

**1. O indicador ⚡ nunca era apagado.** A fonte do preço era rastreada num
`set` que só crescia: uma vez que um símbolo recebia um update com book,
ficava marcado como "book" para sempre - mesmo que todos os updates
seguintes viessem do canal agregado (que só tem `lastPrice`). O indicador
passou a refletir a fonte **do update atual**, podendo voltar para "last"
quando apropriado.

**2. A maioria dos pares nunca recebia bid/ask.** Só os pares configurados
no bot têm subscrição WebSocket individual (`sub.ticker`, único canal WS
com bid1/ask1). Todos os outros pares do Dashboard dependiam do canal
agregado (`sub.tickers`), que não traz book - então exibiam `lastPrice`,
um preço que ninguém consegue executar.

### Correção

O antigo `funding_rate_poll_loop` foi reescrito como
`futures_rest_poll_loop`. A mudança aproveita um desperdício que existia:
esse loop já chamava o endpoint REST de ticker de futures (que **traz
bid1/ask1 para todos os contratos**) mas descartava tudo exceto o funding
rate.

Agora ele usa a mesma resposta para atualizar **preço de execução (bid) de
todos os pares**, com intervalo reduzido de 30s para 5s. Pares com
subscrição WebSocket individual não são sobrescritos - para esses, o dado
do WS continua sendo mais recente.

Resultado: todo par do Dashboard passa a mostrar preço de execução real,
não apenas os configurados no bot.

| Fonte | Cobertura | Frequência | Tem bid/ask? |
|---|---|---|---|
| WS `sub.ticker` (individual) | pares do bot | ~1s | Sim |
| REST ticker (novo loop) | **todos os pares** | 5s | **Sim** |
| WS `sub.tickers` (agregado) | todos (fallback) | 2s | Não |

### Validação

5 testes automatizados confirmando: preço vindo do bid quando há book,
fonte marcada corretamente, fonte **voltando** para "last" quando o update
não tem book (o bug principal), e o snapshot expondo o valor correto.

---

## Nova proteção: bot recusa abrir posição com preço não-executável

### Motivação

Quando um par não está recebendo bid/ask (o ⚡ não aparece na tabela), o
preço exibido é o **último negociado** - que em pares ilíquidos pode estar
muito distante do que se consegue executar agora. O spread calculado em
cima disso é essencialmente ficção, e foi a causa de operações entrarem
achando ter margem que não existia.

### Como funciona

O engine do dashboard agora informa ao bot, a cada atualização, se **ambos**
os preços (spot e futures) vieram do book. O bot usa isso assim:

| Situação | Comportamento |
|---|---|
| Preços do book (⚡) | Opera normalmente |
| Preço fora do book | **Não abre posição nova** |
| Preço fora do book, mas posição já aberta | **Sai normalmente** |

A assimetria é deliberada: recusar *entrar* em cima de preço duvidoso
evita uma operação ruim, mas recusar *sair* prenderia você numa posição
aberta - que é sempre o risco maior. Sair continua sempre permitido.

Quando a trava efetivamente bloqueia uma entrada (ou seja, o spread teria
sido suficiente), isso é logado como WARNING e aparece na aba Logs:

```
Entrada em JIMOTHY BLOQUEADA: spread de 3.00% atingiria o nível de
entrada, mas os preços não vieram do book (usando último negociado).
Spread pode ser fictício - aguardando preço executável.
```

Logs de bloqueio só são emitidos quando o spread teria disparado a
entrada - updates rotineiros de pares sem book não geram ruído.

### Validação

4 testes automatizados: não entra sem book mesmo com spread bom, entra
normalmente com book, sai normalmente mesmo sem book, e o default do
parâmetro mantém retrocompatibilidade.

---

## Correção: mín/máx histórico de spread contaminado por preços não-executáveis

### Sintoma

O "Máx histórico" de um par mostrava um valor alto demais (ex: +5.31%) que
nunca correspondeu a uma oportunidade real - foi registrado num momento em
que o preço vinha do "último negociado", não do book.

### Causa

Os extremos eram atualizados a **cada** amostra de spread, independente da
qualidade do preço. Um pico calculado com `lastPrice` desatualizado virava
um recorde permanente, contaminando a referência que você usa para
calibrar os níveis de entrada.

### Correção

Os extremos agora só são atualizados quando **ambos** os preços vieram do
book (mesma condição já usada pela trava de entrada do bot). Sem book, os
extremos anteriores são mantidos intactos - nenhum recorde novo é criado
a partir de preço não-executável.

### Reset dos extremos já contaminados

Como os recordes antigos continuam no banco, foi adicionado um botão
**"↺ Resetar mín/máx"** na barra de ferramentas do Dashboard (com
confirmação de duplo clique). Ele limpa os extremos de todos os pares,
tanto no banco quanto na tela, e a partir daí só valores executáveis são
registrados.

Também disponível via API: `DELETE /api/spread-extremes` (opcionalmente
com `?symbol=XXX` para resetar apenas um par).

### Validação

5 testes automatizados: spread de 10% sem book é ignorado, spread de 2%
com book é registrado, o máximo final reflete o valor do book (não o
fictício), e o reset limpa banco e memória corretamente.

---

## Otimização: performance com centenas de pares monitorados

### O problema

Com 580 pares monitorados, cada ciclo de atualização gravava no banco de
forma extremamente ineficiente: **um commit SQLite por par**, e ainda uma
query redundante relendo dados que já estavam em memória.

Medição real do ciclo completo (580 pares):

```
Tempo por ciclo: 4,82s
```

O loop de polling REST roda a cada 5 segundos - ou seja, o sistema
gastava ~96% do intervalo só escrevendo no banco. Em máquinas com disco
mais lento (ou antivírus interceptando escritas), isso estouraria os 5s
e o sistema ficaria permanentemente atrasado, atualizando cada vez mais
devagar.

Commits em SQLite são caros porque cada um força escrita física em disco
(fsync). Mil e cento e sessenta commits a cada 5 segundos é um padrão de
uso que o SQLite não foi feito para suportar.

### A correção

1. **Commits agrupados**: os métodos de escrita aceitam `defer_commit=True`,
   e os loops que percorrem muitos pares fazem **um único commit ao final
   do ciclo** em vez de um por par.
2. **Query redundante eliminada**: `register_spread_sample` relia do banco
   valores que já tinha calculado em memória - uma query extra por par, por
   ciclo.

### Resultado

```
ANTES  (commit por par):  4,82s  (8,31ms/par)
DEPOIS (commit agrupado): 0,18s  (0,31ms/par)
Ganho: 26,7x mais rápido
```

O ciclo passou a usar 3,6% do intervalo de 5s, deixando folga ampla mesmo
em máquinas mais lentas ou com ainda mais pares.

**Resposta prática**: não, o número de pares (mesmo 580, todos com ⚡) não
deixa mais o sistema lento. O polling REST sempre foi **uma única chamada
HTTP** que retorna todos os contratos de uma vez - o gargalo era o banco,
não a rede, e agora está resolvido.

### Validação

6 testes confirmando que os commits agrupados não alteram a lógica:
detecção de cruzamentos, contadores, timestamps e extremos continuam
corretos, e os dados persistem corretamente após o commit agrupado.

---

## Modo foco: máximo desempenho quando o bot está ligado

### Como funciona

Assim que **qualquer par é ligado** no bot, o sistema entra automaticamente
em **modo foco**: todo o processamento se concentra apenas nos pares
operados, ignorando os demais.

O que muda com o foco ativo:

| Componente | Sem foco | Com foco |
|---|---|---|
| Loops de polling REST | percorrem todos os pares (~580) | só os pares do bot |
| WS de spot | até 180 símbolos subscritos | só os pares do bot |
| WS de futures | canal agregado (centenas de contratos a cada 2s) + individuais | **apenas** canais individuais (~1s, com bid/ask) |
| Dashboard | todos os pares atualizando | só os pares do bot atualizam |

O modo desliga sozinho quando o último par é desligado (ou no kill
switch), e o Dashboard volta a monitorar tudo.

### A troca

Os demais pares do Dashboard **param de atualizar** enquanto o bot está
ligado - congelam no último valor conhecido. Isso é deliberado: com o bot
operando dinheiro real em poucos pares, faz mais sentido gastar toda a
capacidade do sistema neles do que dividir atenção com centenas de pares
que você não está operando naquele momento.

Um banner azul **"⚡ MODO FOCO"** aparece na aba do Bot enquanto isso está
ativo, listando quais pares estão em foco.

### Correção de bug encontrada no processo

O WebSocket de spot subscrevia **todos** os pares descobertos (podendo
passar de 500), mas o limite da MEXC é de **200 subscrições por conexão**.
As excedentes eram provavelmente rejeitadas em silêncio - ou pior,
derrubavam a conexão. Agora há um teto de 180 com sistema de prioridade:
pares do bot têm vaga garantida, e os demais ocupam o que sobra.

### Ganho medido

```
Ciclo com 580 pares:      51,3ms
Ciclo com 5 pares (foco):  4,5ms
Ganho no processamento:    11x
```

O ganho real é maior que isso: a medição cobre só o processamento local.
Em modo foco o sistema também deixa de **receber e desserializar**
centenas de mensagens de WebSocket por segundo (o canal agregado de
futures sozinho traz todos os contratos a cada 2s), o que reduz
drasticamente a carga de rede e CPU.

### Validação

9 testes automatizados: filtragem correta dos pares ativos, propagação do
foco aos dois WebSockets, ganho de desempenho, e desativação correta.
Mais teste de integração confirmando que o modo liga ao ativar um par e
desliga no kill switch.

---

## BUG CRÍTICO CORRIGIDO: spread de saída era calculado com a fórmula de entrada

### O sintoma

Uma operação registrou saída com spread de tela de **+0.08%** mas spread
real de **+2.69%** - divergência grande demais para ser slippage, num par
que sempre tem ordens acima de 5 USDT no topo do book. O spread também não
muda 2 pontos percentuais em segundos (leva minutos).

### A causa raiz

O sistema calculava **um único spread**, sempre com a fórmula de ENTRADA:

```
spread = (futures_BID - spot_ASK) / spot_ASK
```

Isso está correto para **entrar**: você compra spot pagando o ask e vende
futures recebendo o bid.

Mas a **saída executa nos lados OPOSTOS do book**: você vende o spot
(recebendo o **bid**) e recompra o futures (pagando o **ask**). O spread
relevante para sair é:

```
spread_saida = (futures_ASK - spot_BID) / spot_BID
```

Com o book largo do JIMOTHY, os dois números diferem em **2,74 pontos
percentuais**:

```
Spread ENTRADA: +1,37%   (fut_bid 0,006609 vs spot_ask 0,006520)
Spread SAÍDA:   +4,10%   (fut_ask 0,006700 vs spot_bid 0,006436)
```

O bot "via" 1,37% e achava que o spread tinha convergido, quando na
realidade sair custava 4,10%. Ou seja: **ele saía sistematicamente em
condições muito piores do que acreditava**, pagando o spread do book duas
vezes (uma na entrada, outra na saída).

Isso explica o padrão de prejuízos pequenos e consistentes mesmo com o
spread "convergindo" bonito na tela.

### A correção

O `PairState` agora mantém o **book completo** dos dois mercados
(`spot_bid`, `spot_ask`, `futures_bid`, `futures_ask`) e calcula **dois
spreads distintos**:

| Spread | Fórmula | Usado para |
|---|---|---|
| `spread_pct` | `(fut_bid − spot_ask) / spot_ask` | decidir ENTRADA |
| `exit_spread_pct` | `(fut_ask − spot_bid) / spot_bid` | decidir SAÍDA |

O bot passou a usar o spread de saída na decisão de sair, e os preços do
lado correto do book como referência das ordens de saída. Se o book não
estiver disponível, cai para o comportamento anterior (retrocompatível).

### No Dashboard

A coluna "Spread %" foi desdobrada em duas: **"Spread entrada"** e
**"Spread saída"**. Ambas ordenáveis. Agora dá para ver de imediato que os
dois números são bem diferentes em pares ilíquidos - e é a diferença entre
eles que define se a operação tem margem real.

### Impacto prático nos seus parâmetros

Como o spread de saída é sempre **maior** que o de entrada (a diferença é
a soma dos dois spreads de book), o `exit_spread_pct` configurado nos
pares precisa ser recalibrado. Um valor como 0,2% pode ser inatingível se
o book do par for largo - o bot ficaria preso na posição.

Recomendação: observe a nova coluna "Spread saída" no Dashboard por alguns
minutos antes de religar o bot, e configure o nível de saída com base nos
valores que ela realmente atinge.

### Validação

11 testes automatizados: cálculo correto de cada spread, confirmação de
que são números diferentes, fallback sem book, exposição no snapshot, e
o comportamento central do bot - **não sair** quando apenas o spread de
entrada convergiu, e sair quando o de saída converge.

---

## Mín/Máx histórico do spread de saída

Complementa a correção dos dois spreads: agora o Dashboard rastreia e
exibe os extremos **de cada spread separadamente**.

| Coluna | Conteúdo |
|---|---|
| Mín / Máx entrada | extremos de `(fut_bid − spot_ask) / spot_ask` |
| Mín / Máx saída | extremos de `(fut_ask − spot_bid) / spot_bid` |

Os extremos de saída ficam numa tabela própria (`exit_spread_extremes`),
já que são uma grandeza diferente e sistematicamente maior. Ambos seguem
as mesmas regras já estabelecidas: só registram com preços do book, e o
botão "↺ Resetar mín/máx" limpa os dois de uma vez.

### Para que serve na prática

O `exit_spread_pct` que você configura no bot precisa ser um valor que o
spread de saída **realmente alcança**. A coluna "Mín / Máx saída" mostra
exatamente essa faixa: se o mínimo histórico de saída de um par é 2,8%,
configurar saída em 0,2% deixaria o bot preso na posição indefinidamente.

Use o **mínimo** da coluna de saída como referência do que é atingível, e
configure o nível de saída um pouco acima disso.

### Nota técnica

Os métodos de extremos foram generalizados para operar sobre qualquer uma
das duas tabelas, com validação do nome contra uma lista fixa (SQLite não
aceita nome de tabela como parâmetro, então a interpolação é validada para
evitar injeção de SQL).

### Validação

9 testes: registro em tabelas separadas, valores diferentes entre entrada
e saída, exposição no snapshot, reset limpando ambas, e rejeição de nome
de tabela inválido.

---

## BUG CRÍTICO: fallback fazia a saída usar a régua errada mesmo após a correção

### O sintoma

Operação do PONS registrou **"spread na tela +0,11%"** mas **"spread real
+2,05%"** - mesmo com a correção dos dois spreads já aplicada e o backend
reiniciado.

### A causa

A correção anterior tinha um fallback perigoso:

```python
effective_exit_spread = exit_spread_pct if exit_spread_pct is not None else spread_pct
```

Quando o book de saída não estava disponível naquele instante
(`exit_spread_pct=None`), o bot **caía silenciosamente para o spread de
entrada** - exatamente a régua errada que a correção pretendia eliminar.

Havia um segundo fallback com o mesmo problema no `recompute_spread` do
engine: sem `spot_bid`/`futures_ask`, ele calculava o "spread de saída"
usando os preços de referência (do lado errado do book), produzindo um
número que **parecia válido** mas não era.

Reproduzido em teste: com `exit_spread_pct=None` e spread de entrada de
0,11%, o bot saía e registrava exatamente o padrão observado -
tela 0,11%, real 2,05%.

### A correção: saída estrita

A saída agora exige **todas** as condições, sem nenhum fallback:

| Condição | Sem ela |
|---|---|
| `exit_spread_pct` disponível | não sai |
| `spot_bid` disponível | não sai |
| `futures_ask` disponível | não sai |
| Preços do book (⚡) | não sai (loga aviso) |
| Spread de saída ≤ nível configurado | não sai |

Se qualquer uma faltar, o bot **permanece na posição** aguardando dados
confiáveis. Ficar posicionado esperando é mais seguro do que sair às
cegas com a régua errada - foi o que gerou os prejuízos.

O `recompute_spread` também ficou estrito: sem book real, `exit_spread_pct`
fica `None` em vez de produzir um número enganoso.

### Simetria com a entrada

Agora entrada e saída têm exatamente as mesmas exigências: ambas só
executam com preços do book (⚡) e com o spread calculado do lado correto
para aquela operação.

### Validação

7 testes cobrindo cada condição isoladamente, incluindo a reprodução
exata do caso do PONS (que agora é bloqueado) e a confirmação de que o
log passa a registrar o spread de saída, não o de entrada.

---

## Pares sem book (sem ⚡) agora são completamente ignorados

### Motivação

Pares cujo preço de Futures vem do "último negociado" (sem ⚡) podem exibir
valores absurdamente descolados da realidade. Um caso real observado:

```
EWT    spot 0,27460    "futures" 97,9200
```

Uma diferença de ordens de grandeza - o "futures" era um negócio antigo
que nada tem a ver com o preço executável atual. Esses pares poluíam a
tela com spreads fictícios e, pior, contaminavam as estatísticas.

### O que mudou

Pares sem book agora são ignorados em **todos** os níveis:

| Onde | Antes | Agora |
|---|---|---|
| Tabela do Dashboard | apareciam com preço absurdo | **omitidos** |
| Contador de cruzamentos | contabilizavam cruzamentos fantasma | não registram |
| Histórico de spread (sparkline) | gravavam pontos fictícios | não registram |
| Mín/Máx histórico | já protegido | continua protegido |
| Callback do bot | recebia os dados | não recebe |

A barra de ferramentas mostra quantos pares estão ocultos no momento
(ex: "412 pares · 168 ocultos (sem book)"), com tooltip explicando o
motivo - assim você sabe que eles existem, sem que poluam a análise.

### Efeito colateral esperado

O número de "Pares monitorados" vai cair bastante, e vai **variar** ao
longo do tempo conforme os pares ganham e perdem book. Isso é o
comportamento correto: a lista agora mostra apenas oportunidades reais,
onde o spread exibido corresponde a algo executável.

### Validação

7 testes usando o caso real do EWT: par com book aparece, par sem book é
omitido, contagem de ocultos correta, e confirmação de que o par sem book
não registra cruzamento, extremos nem histórico de spread.

---

## Correção: pares apareciam e sumiam da tabela (575 → 160 → 355 → 370)

### O sintoma

Logo após o filtro de pares sem book, o contador de "Pares monitorados"
oscilava violentamente a cada poucos segundos.

### A causa

Duas fontes de preço de Futures estavam **se sobrescrevendo**:

| Fonte | Frequência | Tem book? | O que fazia |
|---|---|---|---|
| Polling REST | 5s | Sim (bid1) | marcava o par como "book" |
| WS agregado | 2s | Não | **apagava** o book e marcava "last" |

A cada 2 segundos o canal agregado zerava `futures_bid`/`futures_ask` e
rebaixava a marcação para "last" (tirando o par da tabela); 5 segundos
depois o REST restaurava. Daí a oscilação.

Foi um erro de design meu: tratei "fonte do preço" como um estado único
sobrescrito por qualquer atualização, quando o correto é considerar se o
preço **atual** é executável, independente de qual canal chegou por
último.

### A correção

O canal agregado (sem book) agora **não destrói** dados de book mais
completos:

- Se o par já tem book (de qualquer fonte), o agregado só atualiza o
  volume - não mexe em bid/ask nem na marcação de fonte.
- Se o par nunca teve book, o agregado fornece o preço de referência e
  marca "last" (par fica oculto, corretamente).
- O canal individual (com book) sempre prevalece, por ser o mais rápido.
- O REST, sendo a fonte mais completa, é o único autorizado a **rebaixar**
  um par para "last" - quando nem ele consegue bid para aquele contrato,
  o par realmente não tem book.

### Validação

7 testes cobrindo a sequência exata que causava a oscilação: REST fornece
book → agregado chega sem book (par permanece visível, book preservado) →
individual chega com book (prevalece), mais a confirmação de que pares que
nunca tiveram book continuam ocultos.
