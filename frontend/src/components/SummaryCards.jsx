import { useMemo } from 'react';
import { formatSpread } from '../utils/format';

export default function SummaryCards({ pairs, connectionStatus }) {
  const stats = useMemo(() => {
    const list = Object.values(pairs);
    const total = list.length;

    let maxSpread = null;
    let maxSpreadSymbol = null;
    let topCrossingsSymbol = null;
    let topCrossingsCount = -1;

    for (const p of list) {
      if (p.spread_pct !== null && p.spread_pct !== undefined) {
        if (maxSpread === null || Math.abs(p.spread_pct) > Math.abs(maxSpread)) {
          maxSpread = p.spread_pct;
          maxSpreadSymbol = p.symbol;
        }
      }
      if (p.crossings_count > topCrossingsCount) {
        topCrossingsCount = p.crossings_count;
        topCrossingsSymbol = p.symbol;
      }
    }

    return { total, maxSpread, maxSpreadSymbol, topCrossingsSymbol, topCrossingsCount };
  }, [pairs]);

  const statusConfig = {
    online: { label: 'Online', color: 'var(--online)' },
    offline: { label: 'Offline', color: 'var(--offline)' },
    reconectando: { label: 'Reconectando', color: 'var(--reconnecting)' },
  };
  const status = statusConfig[connectionStatus] || statusConfig.reconectando;

  return (
    <div className="summary-cards">
      <div className="card">
        <div className="card-label">Pares monitorados</div>
        <div className="card-value">{stats.total}</div>
      </div>

      <div className="card">
        <div className="card-label">Maior spread</div>
        <div className={`card-value mono ${stats.maxSpread >= 0 ? 'positive' : 'negative'}`}>
          {formatSpread(stats.maxSpread)}
        </div>
        {stats.maxSpreadSymbol && <div className="card-sub">{stats.maxSpreadSymbol}</div>}
      </div>

      <div className="card">
        <div className="card-label">Mais cruzamentos</div>
        <div className="card-value mono">{stats.topCrossingsCount >= 0 ? stats.topCrossingsCount : '—'}</div>
        {stats.topCrossingsSymbol && <div className="card-sub">{stats.topCrossingsSymbol}</div>}
      </div>

      <div className="card status-card">
        <div className="card-label">Conexão</div>
        <div className="card-value status-value">
          <span className={`status-dot ${connectionStatus}`} style={{ background: status.color }} />
          {status.label}
        </div>
      </div>
    </div>
  );
}
