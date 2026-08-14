import { useCallback, useEffect, useState } from 'react';

const API_BASE = '/api/bot';
const POLL_INTERVAL_MS = 5000;

export function useBotHistory() {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchEvents = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/events?limit=500`);
      if (!resp.ok) throw new Error('Falha ao buscar histórico');
      const data = await resp.json();
      setEvents(data.events || []);
      setError(null);
    } catch {
      setError('Não foi possível carregar o histórico. O backend está rodando?');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchEvents();
    const interval = setInterval(fetchEvents, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchEvents]);

  return { events, loading, error, refetch: fetchEvents };
}
