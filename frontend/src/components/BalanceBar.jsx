import { useCallback, useEffect, useState } from 'react';

const API_BASE = '/api/bot';
const POLL_MS = 10000;

export default function BalanceBar() {
  const [balance, setBalance] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchBalance = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/balance`);
      if (resp.ok) setBalance(await resp.json());
    } catch {
      // silencioso - mantém o último valor conhecido
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchBalance();
    const interval = setInterval(fetchBalance, POLL_MS);
    return () => clearInterval(interval);
  }, [fetchBalance]);

  if (loading && balance === null) {
    return null; // evita flash de layout enquanto carrega a primeira vez
  }

  if (balance && !balance.available) {
    return (
      <div className="balance-bar balance-bar-unavailable">
        <span>💰 Saldo indisponível — configure as credenciais da MEXC no <code>.env</code> do backend para exibir.</span>
      </div>
    );
  }

  const spotFree = balance?.spot?.free;
  const futuresAvailable = balance?.futures?.available_balance;
  const futuresMargin = balance?.futures?.position_margin;
  const hasErrors = balance?.errors?.length > 0;

  return (
    <div className="balance-bar">
      <div className="balance-item">
        <span className="balance-label">Spot (USDT)</span>
        <span className="balance-value mono">
          {spotFree !== undefined && spotFree !== null ? spotFree.toFixed(4) : '—'}
        </span>
      </div>
      <div className="balance-divider" />
      <div className="balance-item">
        <span className="balance-label">Futures disponível (USDT)</span>
        <span className="balance-value mono">
          {futuresAvailable !== undefined && futuresAvailable !== null ? futuresAvailable.toFixed(4) : '—'}
        </span>
      </div>
      <div className="balance-divider" />
      <div className="balance-item">
        <span className="balance-label">Futures em margem (USDT)</span>
        <span className="balance-value mono">
          {futuresMargin !== undefined && futuresMargin !== null ? futuresMargin.toFixed(4) : '—'}
        </span>
      </div>
      {hasErrors && (
        <div className="balance-item balance-error" title={balance.errors.join(' | ')}>
          ⚠ Erro ao consultar parte do saldo
        </div>
      )}
    </div>
  );
}
