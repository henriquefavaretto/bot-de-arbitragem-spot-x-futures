import { useEffect, useState } from 'react';

const API_BASE = '/api/bot';

export default function BotPairConfigForm({ onConfigured }) {
  const [symbol, setSymbol] = useState('');
  const [entrySpread, setEntrySpread] = useState('5');
  const [exitSpread, setExitSpread] = useState('1');
  const [positionSize, setPositionSize] = useState('100');
  // Onde operar. O par fica travado nesta combinacao; o bot nunca troca de
  // venue sozinho -- abrir num lugar e tentar fechar em outro seria o pior
  // desfecho possivel.
  const [combo, setCombo] = useState('mexc:spot|mexc:futures');
  const [tradingInfo, setTradingInfo] = useState(null);

  useEffect(() => {
    fetch(`${API_BASE.replace('/bot', '')}/venues/trading`)
      .then((r) => r.json())
      .then(setTradingInfo)
      .catch(() => {});
  }, []);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);

    const cleanSymbol = symbol.trim().toUpperCase().replace(/USDT$/, '');
    if (!cleanSymbol) {
      setError('Informe o par (ex: EWTUSDT).');
      return;
    }

    const entryVal = parseFloat(entrySpread);
    const exitVal = parseFloat(exitSpread);
    const sizeVal = parseFloat(positionSize);

    if (isNaN(entryVal) || isNaN(exitVal) || isNaN(sizeVal)) {
      setError('Preencha todos os campos numéricos.');
      return;
    }

    setSubmitting(true);
    try {
      const resp = await fetch(`${API_BASE}/pairs/${cleanSymbol}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: true,
          entry_spread_pct: entryVal,
          exit_spread_pct: exitVal,
          position_size_usdt: sizeVal,
          buy_venue: combo.split('|')[0],
          sell_venue: combo.split('|')[1],
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setError(data.detail || 'Erro ao configurar o par.');
        return;
      }
      setSymbol('');
      onConfigured?.();
    } catch {
      setError('Não foi possível conectar ao backend. Ele está rodando?');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="bot-config-form" onSubmit={handleSubmit}>
      <div className="bot-form-row">
        <label htmlFor="bot-symbol">Par (sem "USDT")</label>
        <input
          id="bot-symbol"
          type="text"
          placeholder="ex: EWT"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="bot-form-input bot-symbol-input"
        />
      </div>
      <div className="bot-form-row">
        <label htmlFor="bot-entry">Entrada (spread % ≥)</label>
        <input
          id="bot-entry"
          type="number"
          step="0.1"
          value={entrySpread}
          onChange={(e) => setEntrySpread(e.target.value)}
          className="bot-form-input"
        />
      </div>
      <div className="bot-form-row">
        <label htmlFor="bot-exit">Saída (spread % ≤)</label>
        <input
          id="bot-exit"
          type="number"
          step="0.1"
          value={exitSpread}
          onChange={(e) => setExitSpread(e.target.value)}
          className="bot-form-input"
        />
      </div>
      <div className="bot-form-row">
        <label htmlFor="bot-combo">Operar em</label>
        <select
          id="bot-combo"
          value={combo}
          onChange={(e) => setCombo(e.target.value)}
          className="bot-form-input"
        >
          {COMBOS.map((c) => {
            // `indisponivel` = o bot sabe DECIDIR neste venue (o dashboard
            // multi-exchange monitora todos) mas ainda nao sabe EXECUTAR
            // nele. Deixar selecionavel produziria um erro depois de o
            // usuario preencher o formulario inteiro.
            const liberado = !c.indisponivel && isTradable(tradingInfo, c.value);
            const motivo = c.indisponivel || (liberado ? '' : 'sem credenciais');
            return (
              <option key={c.value} value={c.value} disabled={!liberado}>
                {c.label}{motivo ? ` — ${motivo}` : ''}
              </option>
            );
          })}
        </select>
      </div>
      <div className="bot-form-row">
        <label htmlFor="bot-size">Tamanho (USDT)</label>
        <input
          id="bot-size"
          type="number"
          step="10"
          value={positionSize}
          onChange={(e) => setPositionSize(e.target.value)}
          className="bot-form-input"
        />
      </div>
      <button type="submit" className="bot-submit-btn" disabled={submitting}>
        {submitting ? 'Salvando...' : '+ Adicionar / Atualizar par'}
      </button>
      {error && <div className="bot-form-error">{error}</div>}
      {tradingInfo && !tradingInfo.live_mode && (
        <div className="bot-form-hint">
          Modo SIMULAÇÃO: nenhuma ordem real é enviada. Todas as combinações ficam
          disponíveis para validar a estratégia sem chave de API.
        </div>
      )}
    </form>
  );
}

/**
 * Combinacoes DENTRO da mesma exchange.
 *
 * Cross-exchange (ex: MEXC spot x Gate futures) fica fora nesta versao: exige
 * saldo pre-posicionado nas duas exchanges e, quando uma perna falha, nao da
 * para reverter na mesma conta. O backend recusa essas combinacoes de
 * qualquer forma (BOT_ALLOW_CROSS_EXCHANGE), entao oferece-las aqui so
 * produziria um erro depois de o usuario preencher o formulario.
 */
const COMBOS = [
  { value: 'mexc:spot|mexc:futures', label: 'MEXC — Spot × Futures' },
  {
    value: 'gate:spot|gate:futures',
    label: 'Gate — Spot × Futures',
    indisponivel: 'execução ainda não implementada',
  },
  {
    value: 'bingx:spot|bingx:futures',
    label: 'BingX — Spot × Futures',
    indisponivel: 'execução ainda não implementada',
  },
];

/**
 * Em SIMULACAO tudo e operavel (nenhuma ordem sai). Em LIVE, so o que tem
 * credencial dos DOIS lados -- e a opcao aparece desabilitada, para o usuario
 * saber que ela existe e o que falta, em vez de ela sumir sem explicacao.
 */
function isTradable(info, value) {
  if (!info) return true;
  if (!info.live_mode) return true;
  const [buy, sell] = value.split('|');
  return (info.tradable_combinations || []).some(
    (c) => c.buy_venue === buy && c.sell_venue === sell,
  );
}
