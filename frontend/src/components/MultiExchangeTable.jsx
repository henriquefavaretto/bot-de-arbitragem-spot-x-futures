import { memo, useMemo, useState } from 'react';
import Sparkline from './Sparkline';
import { useVirtualRows } from '../hooks/useVirtualRows';
import Tooltip from './Tooltip';
import {
  formatPrice,
  formatVolume,
  formatSpread,
  formatFunding,
  formatTimeAgo,
} from '../utils/format';

/**
 * Tabela do dashboard multi-exchange.
 *
 * Deliberadamente com a MESMA estrutura de colunas da aba Dashboard: quem já
 * lê uma não precisa reaprender a outra. As diferenças são só as que o
 * multi-exchange realmente exige — as colunas de venue (onde comprar / onde
 * vender) no lugar de "Spot/Futuros" fixos, e a profundidade executável
 * embaixo de cada preço.
 */
const COLUMNS = [
  { key: 'symbol', label: 'Moeda', sortable: true, align: 'left' },
  { key: 'buy_venue', label: 'Comprar em', sortable: true, align: 'left' },
  { key: 'sell_venue', label: 'Vender em', sortable: true, align: 'left' },
  { key: 'buy_ask', label: 'Preço compra', sortable: true, align: 'right' },
  { key: 'sell_bid', label: 'Preço venda', sortable: true, align: 'right' },
  { key: 'vol_usdt', label: 'Volume', sortable: true, align: 'right' },
  { key: 'entry_spread_pct', label: 'Spread entrada', sortable: true, align: 'right' },
  { key: 'exit_spread_pct', label: 'Spread saída', sortable: true, align: 'right' },
  { key: 'net_spread_pct', label: 'Líquido', sortable: true, align: 'right' },
  { key: 'entry_range', label: 'Mín / Máx entrada', sortable: false, align: 'right' },
  { key: 'exit_range', label: 'Mín / Máx saída', sortable: false, align: 'right' },
  { key: 'funding', label: 'Funding', sortable: false, align: 'right' },
  { key: 'crossings', label: 'Cruzamentos', sortable: true, align: 'center' },
  { key: 'sparkline', label: 'Gráfico', sortable: false, align: 'center' },
  { key: 'compare_link', label: 'Comparar', sortable: false, align: 'center' },
  { key: 'sell_link', label: 'Venda', sortable: false, align: 'center' },
  { key: 'buy_link', label: 'Compra', sortable: false, align: 'center' },
];

const EXCHANGE_COLOR = { mexc: '#00b897', gate: '#17a2f3', bingx: '#2b6cff' };

/**
 * Altura de UMA linha, em px. Precisa bater com `.arb-table tbody tr` no CSS:
 * a virtualizacao posiciona as linhas por multiplicacao, entao um valor
 * dessincronizado faz o scroll "pular" sem quebrar nada visivelmente -- o
 * tipo de bug que passa despercebido. Ha um teste que compara os dois.
 */
export const ROW_HEIGHT = 50;

const TRADE_URL = {
  'mexc:spot': (s) => `https://www.mexc.com/exchange/${s}_USDT`,
  'mexc:futures': (s) => `https://futures.mexc.com/exchange/${s}_USDT`,
  'gate:spot': (s) => `https://www.gate.io/trade/${s}_USDT`,
  'gate:futures': (s) => `https://www.gate.io/futures/USDT/${s}_USDT`,
  'bingx:spot': (s) => `https://bingx.com/en/spot/${s}USDT`,
  'bingx:futures': (s) => `https://bingx.com/en/perpetual/${s}-USDT`,
};

/**
 * O TradingView não expõe parâmetro público para pré-popular o "Compare"
 * (isso só existe na Charting Library paga). O link abre a perna de VENDA e
 * o tooltip diz qual símbolo colar em "⊕ Compare" — mesma solução já adotada
 * na aba Dashboard.
 */
function buildCompareUrl(venueKey, symbol) {
  const [exchange, market] = venueKey.split(':');
  const sufixo = market === 'futures' ? '.P' : '';
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(
    `${exchange.toUpperCase()}:${symbol}USDT${sufixo}`,
  )}`;
}

export default function MultiExchangeTable({
  rows, getSparkline, favorites, onToggleFavorite, onOpenDetail,
}) {
  const [sortKey, setSortKey] = useState('net_spread_pct');
  const [sortDir, setSortDir] = useState('desc');

  const sorted = useMemo(() => {
    const copia = [...rows];
    copia.sort((a, b) => {
      let av = a[sortKey];
      let bv = b[sortKey];
      if (sortKey === 'crossings') {
        av = a.crossings ?? 0;
        bv = b.crossings ?? 0;
      }
      if (typeof av === 'string' || typeof bv === 'string') {
        const cmp = String(av ?? '').localeCompare(String(bv ?? ''));
        return sortDir === 'asc' ? cmp : -cmp;
      }
      av = av ?? -Infinity;
      bv = bv ?? -Infinity;
      return sortDir === 'asc' ? av - bv : bv - av;
    });
    return copia;
  }, [rows, sortKey, sortDir]);

  const handleSort = (key) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  };

  const virtual = useVirtualRows({ total: sorted.length, rowHeight: ROW_HEIGHT });
  const janela = sorted.slice(virtual.startIndex, virtual.endIndex);

  return (
    <div
      className="table-wrapper virtualized"
      ref={virtual.containerRef}
      onScroll={virtual.onScroll}
    >
      <table className="arb-table">
        <thead>
          <tr>
            <th className="col-fav" />
            {COLUMNS.map((c) => (
              <th
                key={c.key}
                className={`${c.sortable ? 'sortable' : ''} align-${c.align}`}
                onClick={c.sortable ? () => handleSort(c.key) : undefined}
              >
                {c.label}
                {sortKey === c.key && <span className="sort-arrow">{sortDir === 'desc' ? ' ▼' : ' ▲'}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {/* Espacadores: substituem as linhas fora da tela por altura pura,
              mantendo a barra de rolagem coerente com a lista completa. */}
          {virtual.paddingTop > 0 && (
            <tr style={{ height: virtual.paddingTop }} aria-hidden="true" />
          )}
          {janela.map((r) => (
            <Row
              key={r.key}
              r={r}
              sparkline={getSparkline(r.key)}
              favorite={favorites.has(r.symbol)}
              onToggleFavorite={onToggleFavorite}
              onOpenDetail={onOpenDetail}
            />
          ))}
          {virtual.paddingBottom > 0 && (
            <tr style={{ height: virtual.paddingBottom }} aria-hidden="true" />
          )}
          {sorted.length === 0 && (
            <tr>
              <td colSpan={COLUMNS.length + 1} className="empty-row">
                Aguardando dados das exchanges ou nenhuma combinação corresponde ao filtro.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function VenueCell({ venueKey, label }) {
  const [exchange, market] = venueKey.split(':');
  return (
    <span className="venue-cell">
      <span className="venue-dot" style={{ background: EXCHANGE_COLOR[exchange] || '#7d8798' }} />
      {label.replace(/ (Spot|Futures)$/, '')}
      <span className={`venue-market ${market}`}>{market === 'spot' ? '(S)' : '(F)'}</span>
    </span>
  );
}

/**
 * Preço com a profundidade executável logo abaixo.
 *
 * O valor em dólares responde a pergunta que o preço sozinho não responde:
 * quanto cabe NESTE preço. Um spread de 2% sobre $2 de profundidade não é
 * uma oportunidade de 2% — vira prejuízo assim que a ordem anda o book.
 */
function PriceCell({ price, topUsdt }) {
  return (
    <>
      <div>{formatPrice(price)}</div>
      <div className={`depth-hint ${depthClass(topUsdt)}`}>
        {topUsdt == null ? '' : formatUsd(topUsdt)}
      </div>
    </>
  );
}

function formatUsd(v) {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(2)}K`;
  return `$${v.toFixed(2)}`;
}

function depthClass(v) {
  if (v == null) return 'unknown';
  if (v < 50) return 'thin';
  if (v < 500) return 'medium';
  return 'deep';
}

function RangeCell({ min, max, minTs, maxTs }) {
  if (min == null || max == null) return '—';
  return (
    <Tooltip content={`Mínimo: ${formatTimeAgo(minTs)} · Máximo: ${formatTimeAgo(maxTs)}`}>
      <span>
        <span className="negative">{formatSpread(min)}</span>
        {' / '}
        <span className="positive">{formatSpread(max)}</span>
      </span>
    </Tooltip>
  );
}

/**
 * Memoizada: o snapshot chega a cada ~5s e recria o array inteiro de linhas.
 * Sem `memo`, toda linha visivel reconcilia 17 celulas + um SVG a cada ciclo,
 * mesmo quando nada nela mudou. A comparacao abaixo olha so os campos que a
 * linha realmente desenha.
 */
const Row = memo(function Row({ r, sparkline, favorite, onToggleFavorite, onOpenDetail }) {
  const buyUrl = TRADE_URL[r.buy_venue]?.(r.symbol);
  const sellUrl = TRADE_URL[r.sell_venue]?.(r.symbol);
  // O funding relevante é o da perna VENDIDA: é ela que fica short e paga (ou
  // recebe) enquanto a posição está aberta.
  const funding = r.funding_sell ?? r.funding_buy ?? 0;

  return (
    <tr className={`table-row ${r.tradeable ? '' : 'suspect-row'}`}>
      <td className="col-fav">
        <button
          className={`combo-star ${favorite ? 'on' : ''}`}
          onClick={() => onToggleFavorite(r.symbol)}
          title={favorite ? 'Remover dos favoritos' : 'Marcar como favorito'}
        >
          ★
        </button>
      </td>

      <td className="col-symbol">
        <span className="symbol-click" onClick={() => onOpenDetail(r.symbol)}>{r.symbol}</span>
        {r.cross_exchange && <span className="badge cross" title="Pernas em exchanges diferentes">cross</span>}
        {r.kind === 'futures_futures' && <span className="badge fxf">F×F</span>}
        {!r.tradeable && (
          <Tooltip content={r.suspect_reason}>
            <span className="badge warn">suspeita</span>
          </Tooltip>
        )}
      </td>

      <td><VenueCell venueKey={r.buy_venue} label={r.buy_venue_label} /></td>
      <td><VenueCell venueKey={r.sell_venue} label={r.sell_venue_label} /></td>

      <td className="mono num-cell"><PriceCell price={r.buy_ask} topUsdt={r.buy_top_usdt} /></td>
      <td className="mono num-cell"><PriceCell price={r.sell_bid} topUsdt={r.sell_top_usdt} /></td>

      <td className="mono num-cell volume-cell">{formatVolume(r.vol_usdt)}</td>

      <td className="mono num-cell">
        <span className={r.entry_spread_pct >= 0 ? 'positive' : 'negative'}>
          {formatSpread(r.entry_spread_pct)}
        </span>
      </td>

      <td className="mono num-cell">
        <span className={r.exit_spread_pct >= 0 ? 'positive' : 'negative'}>
          {formatSpread(r.exit_spread_pct)}
        </span>
      </td>

      <td className="mono num-cell">
        <Tooltip content={`Entrada ${formatSpread(r.entry_spread_pct)} menos ${r.fee_round_trip_pct.toFixed(3)}% de taxas das quatro pernas`}>
          <strong className={r.net_spread_pct >= 0 ? 'positive' : 'negative'}>
            {formatSpread(r.net_spread_pct)}
          </strong>
        </Tooltip>
      </td>

      <td className="mono num-cell spread-range-cell">
        <RangeCell min={r.min_entry_pct} max={r.max_entry_pct} minTs={r.min_entry_ts} maxTs={r.max_entry_ts} />
      </td>
      <td className="mono num-cell spread-range-cell">
        <RangeCell min={r.min_exit_pct} max={r.max_exit_pct} minTs={r.min_exit_ts} maxTs={r.max_exit_ts} />
      </td>

      <td className="mono num-cell">
        <span className={funding >= 0 ? 'positive-soft' : 'negative-soft'}>{formatFunding(funding)}</span>
      </td>

      <td className="mono num-cell crossings-cell">
        <Tooltip
          content={
            r.last_crossing_ts
              ? `Último cruzamento: ${formatTimeAgo(r.last_crossing_ts)}`
              : 'Nenhum cruzamento de sinal registrado ainda'
          }
        >
          <span className="crossings-badge">{r.crossings ?? 0}</span>
        </Tooltip>
      </td>

      <td className="sparkline-cell"><Sparkline data={sparkline} /></td>

      <td className="link-cell">
        <Tooltip content={`Abre ${r.symbol} na perna de venda. Use "⊕ Compare" no TradingView e busque o outro venue para sobrepor.`}>
          <a href={buildCompareUrl(r.sell_venue, r.symbol)} target="_blank" rel="noopener noreferrer">📊</a>
        </Tooltip>
      </td>
      <td className="link-cell">
        {sellUrl && <a href={sellUrl} target="_blank" rel="noopener noreferrer" title={`Abrir em ${r.sell_venue_label}`}>⚡</a>}
      </td>
      <td className="link-cell">
        {buyUrl && <a href={buyUrl} target="_blank" rel="noopener noreferrer" title={`Abrir em ${r.buy_venue_label}`}>💰</a>}
      </td>
    </tr>
  );
}, (a, b) => (
  a.favorite === b.favorite &&
  a.sparkline === b.sparkline &&
  a.r.buy_ask === b.r.buy_ask &&
  a.r.sell_bid === b.r.sell_bid &&
  a.r.entry_spread_pct === b.r.entry_spread_pct &&
  a.r.exit_spread_pct === b.r.exit_spread_pct &&
  a.r.net_spread_pct === b.r.net_spread_pct &&
  a.r.buy_top_usdt === b.r.buy_top_usdt &&
  a.r.sell_top_usdt === b.r.sell_top_usdt &&
  a.r.min_entry_pct === b.r.min_entry_pct &&
  a.r.max_entry_pct === b.r.max_entry_pct &&
  a.r.min_exit_pct === b.r.min_exit_pct &&
  a.r.max_exit_pct === b.r.max_exit_pct &&
  a.r.crossings === b.r.crossings &&
  a.r.tradeable === b.r.tradeable
));
