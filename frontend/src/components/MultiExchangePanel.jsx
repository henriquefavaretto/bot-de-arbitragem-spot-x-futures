import { useCallback, useMemo, useState } from 'react';
import MultiExchangeTable from './MultiExchangeTable';
import { useCombinationsSocket, useSymbolQuotes, useVenues } from '../hooks/useCombinationsSocket';
import { formatPrice, formatVolume } from '../utils/format';

/**
 * Dashboard multi-exchange: todas as combinações entre MEXC, Gate e BingX,
 * em Spot × Futures e Futures × Futures.
 *
 * Toda a filtragem acontece no SERVIDOR (ver /api/combinations). São ~5900
 * combinações monitoráveis; mandar todas para o navegador filtrar
 * desperdiçaria banda e travaria a tabela.
 */
export default function MultiExchangePanel() {
  const venues = useVenues();
  const [selectedVenues, setSelectedVenues] = useState([]); // vazio = todos
  const [kinds, setKinds] = useState(['spot_futures', 'futures_futures']);
  // Sem piso de spread nem de volume por padrao: o dashboard mostra TODAS as
  // moedas, e o filtro e uma ferramenta de quem quer estreitar, nao uma
  // barreira de entrada. O `limit` alto no servidor e quem protege o tamanho
  // da resposta.
  const [minNetSpread, setMinNetSpread] = useState('');
  const [minVolUsdt, setMinVolUsdt] = useState(0);
  const [search, setSearch] = useState('');
  const [minMaxEntry, setMinMaxEntry] = useState('');
  const [includeSuspect, setIncludeSuspect] = useState(false);
  const [detailSymbol, setDetailSymbol] = useState(null);
  const [onlyFavorites, setOnlyFavorites] = useState(false);
  // Favoritos moram no navegador: sao preferencia de leitura, nao estado de
  // negocio, e nao precisam sobreviver a troca de maquina.
  const [favorites, setFavorites] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem('arb-favorites') || '[]')); }
    catch { return new Set(); }
  });

  const toggleFavorite = useCallback((symbol) => {
    setFavorites((cur) => {
      const next = new Set(cur);
      next.has(symbol) ? next.delete(symbol) : next.add(symbol);
      localStorage.setItem('arb-favorites', JSON.stringify([...next]));
      return next;
    });
  }, []);

  const filters = useMemo(
    () => ({ venues: selectedVenues, kinds, minNetSpread, minVolUsdt, minMaxEntry, includeSuspect, limit: 2000 }),
    [selectedVenues, kinds, minNetSpread, minVolUsdt, minMaxEntry, includeSuspect],
  );
  const { snapshot, connected, getSparkline } = useCombinationsSocket(filters);

  const toggleVenue = (key) =>
    setSelectedVenues((cur) =>
      cur.includes(key) ? cur.filter((v) => v !== key) : [...cur, key],
    );

  const toggleKind = (k) =>
    setKinds((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]));

  const rows = snapshot?.rows || [];
  const termo = search.trim().toUpperCase();
  const visibleRows = rows.filter(
    (r) => (!onlyFavorites || favorites.has(r.symbol)) && (!termo || r.symbol.includes(termo)),
  );

  return (
    <div className="multi-panel">
      <div className="multi-filters">
        <div className="multi-filter-group">
          <span className="multi-filter-label">Exchanges e mercados</span>
          <div className="multi-chips">
            <button
              className={`multi-chip ${selectedVenues.length === 0 ? 'active' : ''}`}
              onClick={() => setSelectedVenues([])}
              title="Monitorar todos os venues disponíveis"
            >
              Todos
            </button>
            {venues.map((v) => (
              <button
                key={v.key}
                className={`multi-chip ${selectedVenues.includes(v.key) ? 'active' : ''} ${v.ok ? '' : 'offline'}`}
                onClick={() => toggleVenue(v.key)}
                title={
                  v.ok
                    ? `${v.symbols} símbolos · taker ${v.taker_fee_pct?.toFixed(3)}%`
                    : `Indisponível: ${v.error || 'sem resposta'}`
                }
              >
                {v.label}
                {!v.ok && <span className="multi-chip-dot" />}
              </button>
            ))}
          </div>
        </div>

        <div className="multi-filter-group">
          <span className="multi-filter-label">Tipo</span>
          <div className="multi-chips">
            <button
              className={`multi-chip ${kinds.includes('spot_futures') ? 'active' : ''}`}
              onClick={() => toggleKind('spot_futures')}
            >
              Spot × Futures
            </button>
            <button
              className={`multi-chip ${kinds.includes('futures_futures') ? 'active' : ''}`}
              onClick={() => toggleKind('futures_futures')}
            >
              Futures × Futures
            </button>
          </div>
        </div>

        <div className="multi-filter-group">
          <label className="multi-filter-label" htmlFor="combo-search">Buscar</label>
          <input
            id="combo-search"
            type="text"
            placeholder="ex: JIMOTHY"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="multi-input"
          />
        </div>

        <div className="multi-filter-group">
          <label className="multi-filter-label" htmlFor="min-spread">
            Spread líquido mín.
          </label>
          <input
            id="min-spread"
            type="number"
            step="0.1"
            value={minNetSpread}
            onChange={(e) => setMinNetSpread(e.target.value === '' ? '' : Number(e.target.value))}
            className="multi-input"
          />
        </div>

        <div className="multi-filter-group">
          <label className="multi-filter-label" htmlFor="min-vol">
            Volume 24h mín. (USDT)
          </label>
          <input
            id="min-vol"
            type="number"
            step="50000"
            value={minVolUsdt}
            onChange={(e) => setMinVolUsdt(Number(e.target.value) || 0)}
            className="multi-input"
          />
        </div>

        <div className="multi-filter-group">
          <label className="multi-filter-label" htmlFor="min-max-hist">
            Máx. histórico ≥ %
          </label>
          <input
            id="min-max-hist"
            type="number"
            step="0.5"
            placeholder="ex: 2"
            value={minMaxEntry}
            onChange={(e) => setMinMaxEntry(e.target.value === '' ? '' : Number(e.target.value))}
            className="multi-input"
            title="Só pares cujo spread de entrada JÁ atingiu este valor em algum momento — mesmo que agora esteja parado"
          />
        </div>

        <label className="multi-checkbox" title="Mostrar apenas os pares marcados com estrela">
          <input
            type="checkbox"
            checked={onlyFavorites}
            onChange={(e) => setOnlyFavorites(e.target.checked)}
          />
          Só favoritos
        </label>

        <label className="multi-checkbox" title="Linhas barradas pelo consenso de preço entre venues">
          <input
            type="checkbox"
            checked={includeSuspect}
            onChange={(e) => setIncludeSuspect(e.target.checked)}
          />
          Mostrar suspeitas
        </label>
      </div>

      <div className="multi-status">
        <span className={`multi-dot ${connected ? 'on' : 'off'}`} />
        {snapshot ? (
          <>
            <strong>{snapshot.total_matching}</strong> combinações
            {snapshot.returned < snapshot.total_matching && ` (mostrando ${snapshot.returned})`}
            {' · '}
            {visibleRows.length !== rows.length && ` · ${visibleRows.length} após busca/favoritos`}
            {' · '}
            {snapshot.symbols_tracked} símbolos
            {snapshot.suspect_filtered > 0 && (
              <span className="multi-suspect-count">
                {' · '}
                {snapshot.suspect_filtered} barradas por divergência de preço entre venues
              </span>
            )}
          </>
        ) : (
          'conectando…'
        )}
      </div>

      <MultiExchangeTable
        rows={visibleRows}
        getSparkline={getSparkline}
        favorites={favorites}
        onToggleFavorite={toggleFavorite}
        onOpenDetail={setDetailSymbol}
      />

      {detailSymbol && (
        <SymbolDetail symbol={detailSymbol} onClose={() => setDetailSymbol(null)} />
      )}
    </div>
  );
}

/**
 * Diagnóstico por símbolo: a cotação em CADA venue e o desvio de cada um em
 * relação à mediana.
 *
 * É a tela que explica por que uma combinação aparece barrada. Sem ela, o
 * operador só veria a linha sumir — e o caso VANRY (um venue 56% fora dos
 * outros cinco) ficaria indistinguível de um bug do dashboard.
 */
function SymbolDetail({ symbol, onClose }) {
  const data = useSymbolQuotes(symbol);

  return (
    <div className="multi-modal-backdrop" onClick={onClose}>
      <div className="multi-modal" onClick={(e) => e.stopPropagation()}>
        <div className="multi-modal-head">
          <h3>{symbol} — cotação por venue</h3>
          <button className="multi-modal-close" onClick={onClose}>×</button>
        </div>

        {!data ? (
          <p className="dim">Carregando…</p>
        ) : (
          <>
            <p className="multi-modal-ref">
              Preço de referência (mediana de {data.venues_considered} venues):{' '}
              <strong>{formatPrice(data.reference_price)}</strong>
              {!data.has_consensus && (
                <span className="dim">
                  {' '}— venues insuficientes para consenso confiável
                </span>
              )}
            </p>
            <table className="multi-table compact">
              <thead>
                <tr>
                  <th>Venue</th>
                  <th className="num">Bid</th>
                  <th className="num">Ask</th>
                  <th className="num">Desvio</th>
                  <th className="num">Volume 24h</th>
                  <th className="num">Funding</th>
                </tr>
              </thead>
              <tbody>
                {data.quotes.map((q) => (
                  <tr key={q.venue} className={q.is_outlier ? 'suspect' : ''}>
                    <td>
                      {q.venue}
                      {q.is_outlier && <span className="badge warn">fora do consenso</span>}
                    </td>
                    <td className="num">{formatPrice(q.bid)}</td>
                    <td className="num">{formatPrice(q.ask)}</td>
                    <td className={`num ${q.is_outlier ? 'neg strong' : 'dim'}`}>
                      {q.deviation_pct == null ? '—' : `${q.deviation_pct.toFixed(2)}%`}
                    </td>
                    <td className="num dim">{formatVolume(q.vol_usdt)}</td>
                    <td className="num dim">
                      {q.funding_rate ? `${(q.funding_rate * 100).toFixed(4)}%` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="multi-modal-note">
              Venues marcados como fora do consenso desviam mais de 5% da mediana. Quase sempre
              significa que o ticker é o mesmo mas o ativo não: redenominação, migração para
              token v2, ou mercado pré-lançamento. Operar contra eles compraria um ativo e
              venderia outro.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
