export function formatVolume(value) {
  if (value === null || value === undefined || value === 0) return '0';
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return (value / 1_000_000_000).toFixed(1) + 'B';
  if (abs >= 1_000_000) return (value / 1_000_000).toFixed(1) + 'M';
  if (abs >= 1_000) return (value / 1_000).toFixed(0) + 'K';
  return value.toFixed(0);
}

export function formatPrice(value) {
  if (value === null || value === undefined) return '—';
  const abs = Math.abs(value);
  if (abs === 0) return '0';
  if (abs < 0.0001) return value.toFixed(8);
  if (abs < 0.01) return value.toFixed(6);
  if (abs < 1) return value.toFixed(5);
  if (abs < 100) return value.toFixed(4);
  return value.toFixed(2);
}

export function formatSpread(value) {
  if (value === null || value === undefined) return '—';
  return (value >= 0 ? '+' : '') + value.toFixed(2) + '%';
}

export function formatFunding(value) {
  if (value === null || value === undefined) return '—';
  return (value * 100 >= 0 ? '+' : '') + (value * 100).toFixed(4) + '%';
}

export function formatTimeAgo(ts) {
  if (!ts) return 'nunca';
  const diffSec = Math.max(0, (Date.now() / 1000) - ts);
  if (diffSec < 60) return `${Math.floor(diffSec)}s atrás`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}min atrás`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h atrás`;
  return `${Math.floor(diffSec / 86400)}d atrás`;
}

export function formatClockTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function formatDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}
