import { useEffect, useRef, useState, useCallback } from 'react';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
const RECONNECT_DELAY_MS = 2000;
const SPARKLINE_MAX_POINTS = 60;

export function useArbitrageSocket() {
  const [pairs, setPairs] = useState({}); // { symbol: pairData }
  const [connectionStatus, setConnectionStatus] = useState('reconectando');
  const [hiddenPairs, setHiddenPairs] = useState(0);
  const [socketState, setSocketState] = useState('connecting'); // connecting | open | closed
  const sparklineRef = useRef({}); // { symbol: [spread_pct, ...] }
  const flashRef = useRef({}); // { "symbol:field": timestamp } - para animação de flash
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const prevValuesRef = useRef({}); // para detectar mudança de valor por par/campo

  const [flashTick, setFlashTick] = useState(0); // força re-render leve quando algo flasha

  const connect = useCallback(() => {
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    setSocketState('connecting');

    ws.onopen = () => {
      setSocketState('open');
    };

    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }

      if (msg.type === 'heartbeat') {
        setConnectionStatus(msg.connection_status);
        return;
      }

      if (msg.type === 'snapshot') {
        setConnectionStatus(msg.connection_status);
        setHiddenPairs(msg.hidden_pairs_without_book ?? 0);

        const newPairs = {};
        const changedFlashKeys = [];

        for (const p of msg.pairs) {
          const prev = prevValuesRef.current[p.symbol];
          if (prev) {
            if (prev.spot_price !== p.spot_price) changedFlashKeys.push(`${p.symbol}:spot_price:${p.spot_price > prev.spot_price ? 'up' : 'down'}`);
            if (prev.futures_price !== p.futures_price) changedFlashKeys.push(`${p.symbol}:futures_price:${p.futures_price > prev.futures_price ? 'up' : 'down'}`);
            if (prev.spread_pct !== p.spread_pct) changedFlashKeys.push(`${p.symbol}:spread_pct:${p.spread_pct > prev.spread_pct ? 'up' : 'down'}`);
          }
          newPairs[p.symbol] = p;

          // Atualiza sparkline em memória
          if (p.spread_pct !== null && p.spread_pct !== undefined) {
            const arr = sparklineRef.current[p.symbol] || [];
            const last = arr[arr.length - 1];
            if (last !== p.spread_pct) {
              const updated = [...arr, p.spread_pct].slice(-SPARKLINE_MAX_POINTS);
              sparklineRef.current[p.symbol] = updated;
            }
          }
        }

        prevValuesRef.current = newPairs;

        if (changedFlashKeys.length > 0) {
          const now = Date.now();
          for (const key of changedFlashKeys) {
            flashRef.current[key] = now;
          }
          setFlashTick((t) => t + 1);
        }

        setPairs(newPairs);
      }
    };

    ws.onclose = () => {
      setSocketState('closed');
      setConnectionStatus('offline');
      reconnectTimerRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const getFlash = useCallback((symbol, field) => {
    const upKey = `${symbol}:${field}:up`;
    const downKey = `${symbol}:${field}:down`;
    const upTs = flashRef.current[upKey];
    const downTs = flashRef.current[downKey];
    const now = Date.now();
    if (upTs && now - upTs < 600) return 'up';
    if (downTs && now - downTs < 600) return 'down';
    return null;
  }, []);

  const getSparkline = useCallback((symbol) => {
    return sparklineRef.current[symbol] || [];
  }, []);

  return {
    pairs,
    connectionStatus,
    hiddenPairs,
    socketState,
    getFlash,
    getSparkline,
    flashTick,
  };
}
