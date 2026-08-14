import { useCallback, useEffect, useState } from 'react';
import { formatDateTime } from '../utils/format';

const API_BASE = '/api/bot';
const POLL_MS = 4000;

const LEVEL_COLORS = {
  INFO: 'var(--text-secondary)',
  WARNING: 'var(--accent)',
  ERROR: 'var(--negative)',
  CRITICAL: 'var(--negative)',
};

export default function BotLogsPanel() {
  const [logs, setLogs] = useState([]);
  const [levelFilter, setLevelFilter] = useState('all'); // all | INFO | WARNING | ERROR | CRITICAL
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [clearConfirm, setClearConfirm] = useState(false);

  const fetchLogs = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: '500' });
      if (levelFilter !== 'all') params.set('level', levelFilter);
      const resp = await fetch(`${API_BASE}/logs?${params}`);
      if (!resp.ok) throw new Error('Falha ao buscar logs');
      const data = await resp.json();
      setLogs(data.logs || []);
      setError(null);
    } catch {
      setError('Não foi possível carregar os logs. O backend está rodando?');
    } finally {
      setLoading(false);
    }
  }, [levelFilter]);

  useEffect(() => {
    fetchLogs();
    if (!autoRefresh) return;
    const interval = setInterval(fetchLogs, POLL_MS);
    return () => clearInterval(interval);
  }, [fetchLogs, autoRefresh]);

  const handleClearLogs = async () => {
    if (!clearConfirm) {
      setClearConfirm(true);
      setTimeout(() => setClearConfirm(false), 4000);
      return;
    }
    setClearing(true);
    try {
      await fetch(`${API_BASE}/logs`, { method: 'DELETE' });
      await fetchLogs();
    } finally {
      setClearing(false);
      setClearConfirm(false);
    }
  };

  return (
    <div className="bot-logs-panel">
      <div className="bot-simulation-banner">
        <span>
          Log técnico interno do bot: conexões, decisões de entrada/saída, erros e reconexões.
          Diferente da aba "Histórico" (que mostra só operações concluídas), aqui aparece tudo que o bot processa.
        </span>
      </div>

      <div className="table-panel">
        <div className="table-toolbar">
          <div className="spread-mode-toggle">
            <span className="toggle-label">Nível:</span>
            {['all', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((lvl) => (
              <button
                key={lvl}
                className={`toggle-btn ${levelFilter === lvl ? 'active' : ''}`}
                onClick={() => setLevelFilter(lvl)}
              >
                {lvl === 'all' ? 'Tudo' : lvl}
              </button>
            ))}
          </div>
          <button
            className={`toggle-btn ${autoRefresh ? 'active' : ''}`}
            onClick={() => setAutoRefresh((v) => !v)}
            title="Atualizar automaticamente a cada poucos segundos"
          >
            {autoRefresh ? '⏸ Pausar auto-atualização' : '▶ Retomar auto-atualização'}
          </button>
          <button className="bot-action-btn" onClick={fetchLogs}>
            ⟳ Atualizar
          </button>
          <button
            className={`bot-action-btn danger ${clearConfirm ? 'confirm' : ''}`}
            onClick={handleClearLogs}
            disabled={clearing || logs.length === 0}
          >
            {clearConfirm ? 'Confirmar apagar tudo?' : '🗑 Apagar logs'}
          </button>
          <div className="result-count">{logs.length} linhas</div>
        </div>

        <div className="logs-scroll">
          {loading && <div className="empty-row">Carregando logs...</div>}
          {!loading && error && <div className="empty-row">{error}</div>}
          {!loading && !error && logs.length === 0 && (
            <div className="empty-row">
              Nenhum log registrado ainda. Os logs aparecem conforme o bot processa atualizações de preço e decisões.
            </div>
          )}
          {!loading && !error && logs.map((log, idx) => (
            <div key={idx} className="log-line">
              <span className="log-ts mono">{formatDateTime(log.ts)}</span>
              <span
                className="log-level mono"
                style={{ color: LEVEL_COLORS[log.level] || 'var(--text-secondary)' }}
              >
                {log.level}
              </span>
              <span className="log-logger mono">{log.logger}</span>
              <span className="log-message">{log.message}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
