import { useCallback, useEffect, useState } from 'react';
import { useBotSocket } from '../hooks/useBotSocket';
import BotPairConfigForm from './BotPairConfigForm';
import { formatTimeAgo, formatSpread } from '../utils/format';

const API_BASE = '/api/bot';
const HEALTH_POLL_MS = 4000;

const STATE_LABELS = {
  IDLE: { label: 'Aguardando', color: 'var(--text-secondary)' },
  ENTERING: { label: 'Entrando...', color: 'var(--accent)' },
  OPEN: { label: 'Posição aberta', color: 'var(--positive)' },
  EXITING: { label: 'Saindo...', color: 'var(--accent)' },
  PAUSED_ERROR: { label: 'Pausado (erro)', color: 'var(--negative)' },
  MANUAL_HALT: { label: 'Pausado manualmente', color: 'var(--text-tertiary)' },
};

export default function BotPanel({ dashboardPairs = {} }) {
  const { pairs, socketState } = useBotSocket();
  const [killSwitchConfirm, setKillSwitchConfirm] = useState(false);
  const [killSwitchBusy, setKillSwitchBusy] = useState(false);
  const [health, setHealth] = useState(null);
  const [removingSymbol, setRemovingSymbol] = useState(null);

  const fetchHealth = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/health`);
      if (resp.ok) setHealth(await resp.json());
    } catch {
      // silencioso - o banner mostra "verificando..." enquanto health for null
    }
  }, []);

  useEffect(() => {
    fetchHealth();
    const interval = setInterval(fetchHealth, HEALTH_POLL_MS);
    return () => clearInterval(interval);
  }, [fetchHealth]);

  const list = Object.values(pairs);
  const isLive = health?.execution_mode === 'live';

  const handleKillSwitch = async () => {
    if (!killSwitchConfirm) {
      setKillSwitchConfirm(true);
      setTimeout(() => setKillSwitchConfirm(false), 4000);
      return;
    }
    setKillSwitchBusy(true);
    try {
      await fetch(`${API_BASE}/kill-switch`, { method: 'POST' });
    } finally {
      setKillSwitchBusy(false);
      setKillSwitchConfirm(false);
    }
  };

  const handleResume = async (symbol) => {
    const confirmed = window.confirm(
      `Retomar ${symbol}?\n\nIsso limpa os dados da posição pausada (quantidades, preços de entrada). ` +
      `Confirme antes que você já verificou manualmente na MEXC se há saldo residual do ativo na ` +
      `carteira Spot ou posição aberta no Futures que precise ser resolvida.`
    );
    if (!confirmed) return;
    await fetch(`${API_BASE}/pairs/${symbol}/resume`, { method: 'POST' });
  };

  const handleRemove = async (symbol) => {
    setRemovingSymbol(symbol);
    try {
      const resp = await fetch(`${API_BASE}/pairs/${symbol}`, { method: 'DELETE' });
      if (!resp.ok) {
        alert(`Não foi possível remover ${symbol}. Tente novamente.`);
      }
      // O snapshot via WebSocket já reflete a remoção no próximo push do
      // backend; não precisamos atualizar o estado local manualmente.
    } catch {
      alert(`Erro de conexão ao tentar remover ${symbol}.`);
    } finally {
      setRemovingSymbol(null);
    }
  };

  const handleToggleEnabled = async (pair) => {
    await fetch(`${API_BASE}/pairs/${pair.symbol}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: !pair.config.enabled,
        entry_spread_pct: pair.config.entry_spread_pct,
        exit_spread_pct: pair.config.exit_spread_pct,
        position_size_usdt: pair.config.position_size_usdt,
      }),
    });
  };

  return (
    <div className="bot-panel">
      {health === null && (
        <div className="bot-simulation-banner">
          <span>Verificando modo de execução do bot...</span>
        </div>
      )}

      {health !== null && !isLive && (
        <div className="bot-simulation-banner">
          <span className="bot-simulation-badge">MODO SIMULAÇÃO</span>
          <span>
            Nenhuma ordem real está sendo enviada. Todas as entradas e saídas abaixo são calculadas e
            registradas, mas não movimentam seu saldo na MEXC.
          </span>
        </div>
      )}

      {health !== null && isLive && (
        <div className="bot-live-banner">
          <span className="bot-live-badge">⚠ MODO LIVE — DINHEIRO REAL</span>
          <span>
            O bot está enviando ordens REAIS na sua conta MEXC. Exposição atual:{' '}
            <strong>{health.current_total_exposure_usdt?.toFixed(2)} USDT</strong>
            {health.max_total_exposure_usdt != null && (
              <> de um teto global de <strong>{health.max_total_exposure_usdt.toFixed(2)} USDT</strong></>
            )}
            .
          </span>
        </div>
      )}

      {health?.focus_mode && (
        <div className="bot-focus-banner">
          <span className="bot-focus-badge">⚡ MODO FOCO</span>
          <span>
            Todo o processamento está concentrado nos {health.focus_symbols?.length ?? 0} pares ligados
            {health.focus_symbols?.length > 0 && <> (<strong>{health.focus_symbols.join(', ')}</strong>)</>}
            {' '}para latência mínima. Os demais pares do Dashboard não estão sendo atualizados
            enquanto o bot estiver ligado.
          </span>
        </div>
      )}

      <div className="bot-toolbar">
        <div className="bot-socket-status">
          <span className={`status-dot ${socketState === 'open' ? 'online' : 'reconectando'}`} />
          {socketState === 'open' ? 'Conectado ao bot' : 'Reconectando ao bot...'}
        </div>
        <button
          className={`bot-kill-switch ${killSwitchConfirm ? 'confirm' : ''}`}
          onClick={handleKillSwitch}
          disabled={killSwitchBusy}
        >
          {killSwitchConfirm ? 'Clique novamente para CONFIRMAR' : '⏻ Kill Switch — Parar tudo'}
        </button>
      </div>

      <BotPairConfigForm onConfigured={fetchHealth} />

      <div className="bot-pairs-table-wrap">
        <table className="bot-pairs-table">
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Par</th>
              <th style={{ textAlign: 'right' }}>Spread atual</th>
              <th style={{ textAlign: 'left' }}>Status</th>
              <th style={{ textAlign: 'left' }}>Ativo</th>
              <th style={{ textAlign: 'right' }}>Entrada %</th>
              <th style={{ textAlign: 'right' }}>Saída %</th>
              <th style={{ textAlign: 'right' }}>Tamanho (USDT)</th>
              <th style={{ textAlign: 'left' }}>Posição aberta</th>
              <th style={{ textAlign: 'right' }}>Ações</th>
            </tr>
          </thead>
          <tbody>
            {list.map((p) => {
              const stateInfo = STATE_LABELS[p.state] || { label: p.state, color: 'var(--text-secondary)' };
              const isOpen = p.state === 'OPEN' || p.state === 'ENTERING' || p.state === 'EXITING';
              const liveSpread = dashboardPairs[p.symbol]?.spread_pct;
              const spreadPositive = liveSpread !== null && liveSpread !== undefined && liveSpread >= 0;
              return (
                <tr key={p.symbol}>
                  <td className="col-symbol">{p.symbol}</td>
                  <td className="mono num-cell">
                    {liveSpread !== null && liveSpread !== undefined ? (
                      <span className={spreadPositive ? 'positive' : 'negative'}>{formatSpread(liveSpread)}</span>
                    ) : (
                      <span className="bot-error-text" style={{ margin: 0 }}>par não encontrado no dashboard</span>
                    )}
                  </td>
                  <td>
                    <span className="bot-state-badge" style={{ color: stateInfo.color, borderColor: stateInfo.color }}>
                      {stateInfo.label}
                    </span>
                    {p.last_error && <div className="bot-error-text">{p.last_error}</div>}
                  </td>
                  <td className="mono">
                    <button
                      className={`bot-toggle-btn ${p.config.enabled ? 'active' : ''}`}
                      onClick={() => handleToggleEnabled(p)}
                      disabled={p.state === 'MANUAL_HALT' || p.state === 'PAUSED_ERROR'}
                    >
                      {p.config.enabled ? 'Ligado' : 'Desligado'}
                    </button>
                  </td>
                  <td className="mono num-cell">{p.config.entry_spread_pct}%</td>
                  <td className="mono num-cell">{p.config.exit_spread_pct}%</td>
                  <td className="mono num-cell">{p.config.position_size_usdt}</td>
                  <td className="mono">
                    {isOpen ? (
                      <div className="bot-position-detail">
                        <div>Entrou: {formatTimeAgo(p.entry_ts)}</div>
                        <div>Spread entrada: {p.entry_spread_pct?.toFixed(2)}%</div>
                        <div>Notional: {p.entry_notional_usdt?.toFixed(2)} USDT</div>
                      </div>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    <div className="bot-row-actions">
                      {(p.state === 'MANUAL_HALT' || p.state === 'PAUSED_ERROR') && (
                        <button className="bot-action-btn" onClick={() => handleResume(p.symbol)}>
                          Retomar
                        </button>
                      )}
                      <button
                        className="bot-action-btn danger"
                        onClick={() => handleRemove(p.symbol)}
                        disabled={removingSymbol === p.symbol}
                      >
                        {removingSymbol === p.symbol ? 'Removendo...' : 'Remover'}
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
            {list.length === 0 && (
              <tr>
                <td colSpan={9} className="empty-row">
                  Nenhum par configurado ainda. Use o formulário acima para adicionar.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
