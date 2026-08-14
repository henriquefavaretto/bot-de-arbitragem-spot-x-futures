import { useMemo, useState } from 'react';
import { useBotHistory } from '../hooks/useBotHistory';
import { formatSpread, formatDateTime, formatPrice } from '../utils/format';

const API_BASE = '/api/bot';

// Cobre todos os tipos de evento gerados pelo backend, tanto em modo
// SIMULAÇÃO quanto LIVE - antes só entry_simulated/exit_simulated eram
// reconhecidos, então eventos reais (entry_live, exit_live) e de erro
// ficavam invisíveis nesta tela.
const EVENT_LABELS = {
  entry_simulated: { label: 'Entrada (sim.)', color: 'var(--positive)', kind: 'entry' },
  exit_simulated: { label: 'Saída (sim.)', color: 'var(--accent)', kind: 'exit' },
  entry_live: { label: 'Entrada (real)', color: 'var(--positive)', kind: 'entry' },
  exit_live: { label: 'Saída (real)', color: 'var(--accent)', kind: 'exit' },
  entry_failed_size_too_small: { label: 'Falha: tamanho', color: 'var(--negative)', kind: 'error' },
  entry_failed_no_contract_spec: { label: 'Falha: sem specs do contrato', color: 'var(--negative)', kind: 'error' },
  entry_failed_futures_order: { label: 'Falha: ordem futures', color: 'var(--negative)', kind: 'error' },
  entry_fill_unconfirmed: { label: 'Falha: fill não confirmado', color: 'var(--negative)', kind: 'error' },
  entry_failed_spot_order_reverted: { label: 'Falha: spot revertido', color: 'var(--negative)', kind: 'error' },
  entry_failed_spot_fill_unconfirmed: { label: 'Falha: fill spot não confirmado', color: 'var(--negative)', kind: 'error' },
  exit_spot_leg_failed: { label: '⚠ Saída incompleta: verificar Spot', color: 'var(--negative)', kind: 'error' },
  kill_switch_close: { label: 'Kill Switch', color: 'var(--negative)', kind: 'error' },
};

function formatUsdt(value) {
  if (value === null || value === undefined) return '—';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${value.toFixed(4)} USDT`;
}

export default function BotHistoryPanel() {
  const { events, loading, error, refetch } = useBotHistory();
  const [symbolFilter, setSymbolFilter] = useState('');
  const [eventTypeFilter, setEventTypeFilter] = useState('all'); // all | trades | errors
  const [clearing, setClearing] = useState(false);
  const [clearConfirm, setClearConfirm] = useState(false);

  const filtered = useMemo(() => {
    let result = events;

    if (symbolFilter.trim()) {
      const q = symbolFilter.trim().toUpperCase();
      result = result.filter((e) => e.symbol.toUpperCase().includes(q));
    }

    if (eventTypeFilter === 'trades') {
      result = result.filter((e) => {
        const info = EVENT_LABELS[e.event];
        return info?.kind === 'entry' || info?.kind === 'exit';
      });
    } else if (eventTypeFilter === 'errors') {
      result = result.filter((e) => EVENT_LABELS[e.event]?.kind === 'error');
    }

    return result;
  }, [events, symbolFilter, eventTypeFilter]);

  const summary = useMemo(() => {
    const exits = events.filter((e) => e.event === 'exit_simulated' || e.event === 'exit_live');
    const totalPnl = exits.reduce((acc, e) => acc + (e.detail.pnl_total_usdt || 0), 0);
    const wins = exits.filter((e) => (e.detail.pnl_total_usdt || 0) > 0).length;
    const losses = exits.filter((e) => (e.detail.pnl_total_usdt || 0) < 0).length;
    const liveCount = exits.filter((e) => e.event === 'exit_live').length;
    return { totalTrades: exits.length, totalPnl, wins, losses, liveCount };
  }, [events]);

  const handleClearHistory = async () => {
    if (!clearConfirm) {
      setClearConfirm(true);
      setTimeout(() => setClearConfirm(false), 4000);
      return;
    }
    setClearing(true);
    try {
      await fetch(`${API_BASE}/events`, { method: 'DELETE' });
      await refetch();
    } finally {
      setClearing(false);
      setClearConfirm(false);
    }
  };

  return (
    <div className="bot-history-panel">
      <div className="bot-simulation-banner">
        <span>
          O histórico mostra tanto operações simuladas quanto reais (identificadas na coluna "Evento").
          O PnL é calculado com base nos preços de mercado no momento de cada entrada/saída.
        </span>
      </div>

      <div className="summary-cards">
        <div className="card">
          <div className="card-label">Operações fechadas</div>
          <div className="card-value">{summary.totalTrades}</div>
          {summary.liveCount > 0 && <div className="card-sub">{summary.liveCount} reais</div>}
        </div>
        <div className="card">
          <div className="card-label">PnL total</div>
          <div className={`card-value mono ${summary.totalPnl >= 0 ? 'positive' : 'negative'}`}>
            {formatUsdt(summary.totalPnl)}
          </div>
        </div>
        <div className="card">
          <div className="card-label">Ganhos / Perdas</div>
          <div className="card-value mono">
            <span className="positive">{summary.wins}</span>
            {' / '}
            <span className="negative">{summary.losses}</span>
          </div>
        </div>
        <div className="card">
          <div className="card-label">Taxa de acerto</div>
          <div className="card-value mono">
            {summary.totalTrades > 0 ? `${((summary.wins / summary.totalTrades) * 100).toFixed(0)}%` : '—'}
          </div>
        </div>
      </div>

      <div className="table-panel">
        <div className="table-toolbar">
          <input
            type="text"
            placeholder="Buscar par... (ex: EWT)"
            value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value)}
            className="search-input"
          />
          <div className="spread-mode-toggle">
            <span className="toggle-label">Mostrar:</span>
            <button
              className={`toggle-btn ${eventTypeFilter === 'all' ? 'active' : ''}`}
              onClick={() => setEventTypeFilter('all')}
            >
              Tudo
            </button>
            <button
              className={`toggle-btn ${eventTypeFilter === 'trades' ? 'active' : ''}`}
              onClick={() => setEventTypeFilter('trades')}
            >
              Entradas/Saídas
            </button>
            <button
              className={`toggle-btn ${eventTypeFilter === 'errors' ? 'active' : ''}`}
              onClick={() => setEventTypeFilter('errors')}
            >
              Erros/Kill Switch
            </button>
          </div>
          <button className="bot-action-btn" onClick={refetch}>
            ⟳ Atualizar
          </button>
          <button
            className={`bot-action-btn danger ${clearConfirm ? 'confirm' : ''}`}
            onClick={handleClearHistory}
            disabled={clearing || events.length === 0}
          >
            {clearConfirm ? 'Confirmar apagar tudo?' : '🗑 Apagar histórico'}
          </button>
          <div className="result-count">{filtered.length} eventos</div>
        </div>

        <div className="table-scroll">
          <table className="arb-table">
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Data e hora</th>
                <th style={{ textAlign: 'left' }}>Par</th>
                <th style={{ textAlign: 'left' }}>Evento</th>
                <th style={{ textAlign: 'right' }} title="Spread real da operação, calculado dos preços que efetivamente executaram">Spread real</th>
                <th style={{ textAlign: 'right' }} title="Spread que estava na tela quando o bot decidiu operar - a diferença para o real é o custo de slippage">Spread na tela</th>
                <th style={{ textAlign: 'right' }}>Preço Spot</th>
                <th style={{ textAlign: 'right' }}>Preço Futuros</th>
                <th style={{ textAlign: 'right' }}>Notional (USDT)</th>
                <th style={{ textAlign: 'right' }}>PnL</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={9} className="empty-row">Carregando histórico...</td>
                </tr>
              )}
              {!loading && error && (
                <tr>
                  <td colSpan={9} className="empty-row">{error}</td>
                </tr>
              )}
              {!loading && !error && filtered.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty-row">
                    {events.length === 0
                      ? 'Nenhuma operação registrada ainda. Configure um par na aba "Bot de Arbitragem".'
                      : 'Nenhum evento corresponde ao filtro.'}
                  </td>
                </tr>
              )}
              {!loading && !error && filtered.map((e, idx) => (
                <HistoryRow key={`${e.symbol}-${e.ts}-${idx}`} event={e} />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function HistoryRow({ event }) {
  const info = EVENT_LABELS[event.event] || { label: event.event, color: 'var(--text-secondary)', kind: 'other' };
  const d = event.detail;

  const isEntry = info.kind === 'entry';
  const isExit = info.kind === 'exit';

  // Spread REALIZADO (calculado dos preços de fill reais)
  const spread = isEntry ? d.spread_pct : isExit ? d.exit_spread_pct : null;

  // Spread que estava na tela quando a decisão foi tomada - a diferença
  // entre os dois é o custo de slippage da execução a mercado.
  const spreadSignal = isEntry ? d.spread_signal_pct : isExit ? d.exit_spread_signal_pct : null;

  // Aceita tanto os nomes de campo novos quanto os antigos (eventos
  // gravados antes desta correção), para o histórico existente continuar
  // legível em vez de mostrar "—".
  const spotPrice = isEntry
    ? (d.spot_price ?? d.spot_fill_price ?? null)
    : isExit
      ? (d.exit_spot_price ?? null)
      : (d.spot_fill_price ?? null);
  const futuresPrice = isEntry
    ? (d.futures_price ?? d.futures_fill_price ?? null)
    : isExit
      ? (d.exit_futures_price ?? null)
      : null;
  const notional = isEntry ? d.notional_usdt : null;
  const pnl = isExit ? d.pnl_total_usdt : null;
  const errorDetail = d.error || d.reason || d.spot_error;

  return (
    <tr className="table-row">
      <td className="mono">{formatDateTime(event.ts)}</td>
      <td className="col-symbol">{event.symbol}</td>
      <td>
        <span className="bot-state-badge" style={{ color: info.color, borderColor: info.color }}>
          {info.label}
        </span>
        {errorDetail && <div className="bot-error-text">{errorDetail}</div>}
      </td>
      <td className="mono num-cell">
        {spread !== null && spread !== undefined ? formatSpread(spread) : '—'}
      </td>
      <td className="mono num-cell spread-signal-cell">
        {spreadSignal !== null && spreadSignal !== undefined ? formatSpread(spreadSignal) : '—'}
      </td>
      <td className="mono num-cell">{formatPrice(spotPrice)}</td>
      <td className="mono num-cell">{formatPrice(futuresPrice)}</td>
      <td className="mono num-cell">{notional !== null && notional !== undefined ? notional.toFixed(2) : '—'}</td>
      <td className="mono num-cell">
        {pnl !== null && pnl !== undefined ? (
          <span className={pnl >= 0 ? 'positive' : 'negative'}>{formatUsdt(pnl)}</span>
        ) : (
          '—'
        )}
      </td>
    </tr>
  );
}
