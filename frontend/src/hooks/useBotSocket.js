import { useEffect, useRef, useState, useCallback } from 'react';

const BOT_WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/bot`;
const RECONNECT_DELAY_MS = 2000;

export function useBotSocket() {
  const [pairs, setPairs] = useState({}); // { symbol: pairBotState }
  const [socketState, setSocketState] = useState('connecting'); // connecting | open | closed
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);

  const connect = useCallback(() => {
    const ws = new WebSocket(BOT_WS_URL);
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

      if (msg.type === 'bot_snapshot') {
        const newPairs = {};
        for (const p of msg.pairs) {
          newPairs[p.symbol] = p;
        }
        setPairs(newPairs);
      }
    };

    ws.onclose = () => {
      setSocketState('closed');
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

  return { pairs, socketState };
}
