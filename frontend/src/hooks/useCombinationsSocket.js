import { useEffect, useRef, useState } from 'react';

const WS_BASE = (import.meta.env.VITE_WS_BASE || 'ws://localhost:8000');
const API_BASE = (import.meta.env.VITE_API_BASE || 'http://localhost:8000') + '/api';

const SPARKLINE_MAX_POINTS = 40;

/**
 * WebSocket do dashboard multi-exchange.
 *
 * Os filtros viajam como query params da CONEXÃO, não como mensagem: são
 * ~5900 combinações monitoráveis, e filtrar no servidor é o que mantém o
 * payload pequeno. Quando os filtros mudam, o socket é reconectado — é
 * barato e evita ter que sincronizar estado de filtro dos dois lados.
 */
export function useCombinationsSocket(filters) {
  const [snapshot, setSnapshot] = useState(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);
  // Historico do sparkline acumulado no NAVEGADOR, indexado pela chave da
  // combinacao. Guardar isso no banco seria ~1180 escritas por segundo com
  // 5900 combinacoes a cada 5s - o mesmo antipadrao que ja custou 4,82s por
  // ciclo neste projeto (bug 9). A aba Dashboard ja funciona assim.
  const sparkRef = useRef({});

  // Serializa os filtros para uma string estável: assim o efeito só
  // reconecta quando um filtro REALMENTE muda, e não a cada render por o
  // objeto ser recriado.
  const query = buildQuery(filters);

  useEffect(() => {
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(`${WS_BASE}/ws/combinations?${query}`);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onmessage = (evt) => {
        try {
          const data = JSON.parse(evt.data);
          if (data.type === 'multi_snapshot') {
            for (const row of data.rows || []) {
              const serie = sparkRef.current[row.key] || [];
              if (serie[serie.length - 1] !== row.entry_spread_pct) {
                sparkRef.current[row.key] = [...serie, row.entry_spread_pct].slice(-SPARKLINE_MAX_POINTS);
              }
            }
            setSnapshot(data);
          }
        } catch {
          /* mensagem malformada: o próximo snapshot corrige */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) reconnectRef.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      cancelled = true;
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [query]);

  const getSparkline = (comboKey) => sparkRef.current[comboKey] || [];

  return { snapshot, connected, getSparkline };
}

function buildQuery(filters) {
  const p = new URLSearchParams();
  if (filters.venues?.length) p.set('venues', filters.venues.join(','));
  if (filters.kinds?.length) p.set('kinds', filters.kinds.join(','));
  if (filters.minNetSpread !== '' && filters.minNetSpread != null) {
    p.set('min_net_spread_pct', String(filters.minNetSpread));
  }
  if (filters.minVolUsdt) p.set('min_vol_usdt', String(filters.minVolUsdt));
  if (filters.minMaxEntry !== '' && filters.minMaxEntry != null) {
    p.set('min_max_entry_pct', String(filters.minMaxEntry));
  }
  if (filters.includeSuspect) p.set('include_suspect', 'true');
  p.set('limit', String(filters.limit || 300));
  return p.toString();
}

/** Lista de venues disponíveis, vinda do backend (nunca escrita à mão aqui). */
export function useVenues() {
  const [venues, setVenues] = useState([]);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const resp = await fetch(`${API_BASE}/venues`);
        const data = await resp.json();
        if (!cancelled) setVenues(data.venues || []);
      } catch {
        /* backend fora do ar: a próxima tentativa cobre */
      }
    };
    load();
    const id = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  return venues;
}

/** Diagnóstico de um símbolo: cotação em cada venue e veredito do consenso. */
export function useSymbolQuotes(symbol) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!symbol) { setData(null); return; }
    let cancelled = false;
    fetch(`${API_BASE}/combinations/${symbol}/quotes`)
      .then((r) => r.json())
      .then((d) => { if (!cancelled) setData(d); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [symbol]);
  return data;
}
