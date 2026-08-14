import { memo, useMemo, useState } from 'react';
import { useVirtualRows } from '../hooks/useVirtualRows';
import Sparkline from './Sparkline';
import FlashCell from './FlashCell';
import Tooltip from './Tooltip';
import {
  formatPrice,
  formatVolume,
  formatSpread,
  formatFunding,
  formatTimeAgo,
} from '../utils/format';

const COLUMNS = [
  { key: 'symbol', label: 'Moeda', sortable: true, align: 'left' },
  { key: 'spot_price', label: 'Preço Spot', sortable: true, align: 'right' },
  { key: 'futures_price', label: 'Preço Futuros', sortable: true, align: 'right' },
  { key: 'volatility_pct', label: 'Volatilidade 24h', sortable: true, align: 'right' },
  { key: 'volume', label: 'Volume', sortable: false, align: 'right' },
  { key: 'spread_pct', label: 'Spread entrada', sortable: true, align: 'right' },
  { key: 'exit_spread_pct', label: 'Spread saída', sortable: true, align: 'right' },
  { key: 'spread_range', label: 'Mín / Máx entrada', sortable: false, align: 'right' },
  { key: 'exit_spread_range', label: 'Mín / Máx saída', sortable: false, align: 'right' },
  { key: 'window_entry', label: 'Melhor entrada', sortable: true, align: 'right' },
  { key: 'window_exit', label: 'Melhor saída', sortable: true, align: 'right' },
  { key: 'funding_rate', label: 'Funding', sortable: true, align: 'right' },
  { key: 'crossings_count', label: 'Cruzamentos', sortable: true, align: 'center' },
  { key: 'sparkline', label: 'Gráfico', sortable: false, align: 'center' },
  { key: 'compare_link', label: 'Comparar', sortable: false, align: 'center' },
  { key: 'futures_link', label: 'Futuros', sortable: false, align: 'center' },
  { key: 'spot_link', label: 'Spot', sortable: false, align: 'center' },
];

/**
 * Janelas moveis de spread. Sao OITO numeros por par (4 janelas x 2 metricas);
 * como colunas fixas isso dobraria a largura da tabela. O seletor abaixo
 * mostra uma janela por vez -- mesmo padrao que o dashboard ja usa para
 * cruzamentos -- e o tooltip de cada celula traz as quatro de uma vez, para
 * comparar sem trocar de janela.
 */
const SPREAD_WINDOWS = [
  { min: 5, label: '5m' },
  { min: 15, label: '15m' },
  { min: 30, label: '30m' },
  { min: 60, label: '60m' },
];

/**
 * Altura de UMA linha da tabela do Dashboard, em px. Precisa bater com o CSS
 * (`.arb-table tbody tr`): a virtualizacao posiciona por multiplicacao.
 */
export const DASH_ROW_HEIGHT = 50;

const CROSSING_WINDOWS = [
  { key: 'crossings_1h', label: '1h' },
  { key: 'crossings_12h', label: '12h' },
  { key: 'crossings_24h', label: '24h' },
];

function buildTradingViewCompareUrl(symbol) {
  // Abre o gráfico do contrato perpétuo na MEXC via TradingView.
  // O TradingView não expõe um parâmetro de URL público/estável para pré-popular
  // o recurso "Compare" (isso só existe na Charting Library paga/embeddada).
  // Por isso o link abre o futuro, e o tooltip do ícone já diz o símbolo spot
  // exato para colar em "⊕ Compare" dentro do próprio TradingView.
  const tvFuturesSymbol = `MEXC:${symbol}USDT.P`;
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tvFuturesSymbol)}`;
}

export default function ArbitrageTable({ pairs, getFlash, getSparkline, hiddenPairs = 0 }) {
  const [search, setSearch] = useState('');
  const [minSpread, setMinSpread] = useState('');
  const [minVolSpot, setMinVolSpot] = useState('');
  const [minVolFutures, setMinVolFutures] = useState('');
  const [minMaxHistorical, setMinMaxHistorical] = useState('');
  const [sortKey, setSortKey] = useState('spread_pct');
  const [sortDir, setSortDir] = useState('desc'); // asc | desc
  const [spreadSortMode, setSpreadSortMode] = useState('abs'); // 'abs' | 'signed'
  const [crossingWindow, setCrossingWindow] = useState('crossings_24h');
  const [spreadWindow, setSpreadWindow] = useState(15); // crossings_1h | crossings_12h | crossings_24h
  const [resetExtremesConfirm, setResetExtremesConfirm] = useState(false);
  const [resettingExtremes, setResettingExtremes] = useState(false);

  const handleResetExtremes = async () => {
    if (!resetExtremesConfirm) {
      setResetExtremesConfirm(true);
      setTimeout(() => setResetExtremesConfirm(false), 4000);
      return;
    }
    setResettingExtremes(true);
    try {
      await fetch('/api/spread-extremes', { method: 'DELETE' });
    } finally {
      setResettingExtremes(false);
      setResetExtremesConfirm(false);
    }
  };

  const list = useMemo(() => Object.values(pairs), [pairs]);

  const filtered = useMemo(() => {
    let result = list;

    if (search.trim()) {
      const q = search.trim().toUpperCase();
      result = result.filter((p) => p.symbol.toUpperCase().includes(q));
    }

    const minSpreadVal = parseFloat(minSpread);
    if (!isNaN(minSpreadVal) && minSpread !== '') {
      result = result.filter((p) => p.spread_pct !== null && Math.abs(p.spread_pct) >= minSpreadVal);
    }

    const minVolSpotVal = parseFloat(minVolSpot);
    if (!isNaN(minVolSpotVal) && minVolSpot !== '') {
      result = result.filter((p) => (p.spot_vol ?? 0) >= minVolSpotVal);
    }

    const minVolFuturesVal = parseFloat(minVolFutures);
    if (!isNaN(minVolFuturesVal) && minVolFutures !== '') {
      result = result.filter((p) => (p.futures_vol ?? 0) >= minVolFuturesVal);
    }

    const minMaxHistoricalVal = parseFloat(minMaxHistorical);
    if (!isNaN(minMaxHistoricalVal) && minMaxHistorical !== '') {
      result = result.filter(
        (p) => p.max_spread_pct !== null && p.max_spread_pct !== undefined && p.max_spread_pct >= minMaxHistoricalVal
      );
    }

    return result;
  }, [list, search, minSpread, minVolSpot, minVolFutures, minMaxHistorical]);

  const sorted = useMemo(() => {
    const arr = [...filtered];
    arr.sort((a, b) => {
      let av, bv;
      if (sortKey === 'spread_pct' && spreadSortMode === 'abs') {
        av = a.spread_pct === null ? -Infinity : Math.abs(a.spread_pct);
        bv = b.spread_pct === null ? -Infinity : Math.abs(b.spread_pct);
      } else if (sortKey === 'crossings_count') {
        // Ordena pela janela de tempo selecionada, não pelo total acumulado
        av = a[crossingWindow];
        bv = b[crossingWindow];
      } else if (sortKey === 'window_entry') {
        av = a[`max_entry_${spreadWindow}m`];
        bv = b[`max_entry_${spreadWindow}m`];
      } else if (sortKey === 'window_exit') {
        // Na saída MENOR é melhor, então o sinal é invertido para que "desc"
        // continue significando "melhores primeiro", como nas outras colunas.
        av = -(a[`min_exit_${spreadWindow}m`] ?? Infinity);
        bv = -(b[`min_exit_${spreadWindow}m`] ?? Infinity);
      } else {
        av = a[sortKey];
        bv = b[sortKey];
      }
      if (av === null || av === undefined) av = -Infinity;
      if (bv === null || bv === undefined) bv = -Infinity;
      if (typeof av === 'string') {
        return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      return sortDir === 'asc' ? av - bv : bv - av;
    });
    return arr;
  }, [filtered, sortKey, sortDir, spreadSortMode, crossingWindow, spreadWindow]);

  const virtual = useVirtualRows({ total: sorted.length, rowHeight: DASH_ROW_HEIGHT });
  const janela = sorted.slice(virtual.startIndex, virtual.endIndex);

  const handleSort = (key) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  };

  const crossingWindowLabel = CROSSING_WINDOWS.find((w) => w.key === crossingWindow)?.label ?? '24h';

  return (
    <div className="table-panel">
      <div className="table-toolbar">
        <input
          type="text"
          placeholder="Buscar par... (ex: EWT)"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="search-input"
        />
        <div className="min-spread-filter">
          <label htmlFor="min-spread">Spread mín. %</label>
          <input
            id="min-spread"
            type="number"
            step="0.1"
            placeholder="0.5"
            value={minSpread}
            onChange={(e) => setMinSpread(e.target.value)}
            className="min-spread-input"
          />
        </div>
        <div className="min-spread-filter">
          <label htmlFor="min-vol-spot">Vol. spot mín.</label>
          <input
            id="min-vol-spot"
            type="number"
            step="1000"
            placeholder="ex: 50000"
            value={minVolSpot}
            onChange={(e) => setMinVolSpot(e.target.value)}
            className="min-spread-input min-vol-input"
          />
        </div>
        <div className="min-spread-filter">
          <label htmlFor="min-vol-futures">Vol. futuros mín.</label>
          <input
            id="min-vol-futures"
            type="number"
            step="1000"
            placeholder="ex: 50000"
            value={minVolFutures}
            onChange={(e) => setMinVolFutures(e.target.value)}
            className="min-spread-input min-vol-input"
          />
        </div>
        <div className="min-spread-filter">
          <label htmlFor="min-max-historical" title="Só mostra pares cujo maior spread já registrado (positivo) foi maior que este valor">
            Máx. histórico ≥ %
          </label>
          <input
            id="min-max-historical"
            type="number"
            step="1"
            placeholder="ex: 10"
            value={minMaxHistorical}
            onChange={(e) => setMinMaxHistorical(e.target.value)}
            className="min-spread-input"
          />
        </div>
        <div className="spread-mode-toggle">
          <span className="toggle-label">Ordenar spread por:</span>
          <button
            className={`toggle-btn ${spreadSortMode === 'abs' ? 'active' : ''}`}
            onClick={() => setSpreadSortMode('abs')}
            title="Maior oportunidade primeiro, seja o spread positivo ou negativo"
          >
            Magnitude
          </button>
          <button
            className={`toggle-btn ${spreadSortMode === 'signed' ? 'active' : ''}`}
            onClick={() => setSpreadSortMode('signed')}
            title="Ordena pelo valor real: positivos no topo (desc) ou negativos no topo (asc)"
          >
            Valor real
          </button>
        </div>
        <div className="spread-mode-toggle">
          <span className="toggle-label">Melhor entrada/saída em:</span>
          {SPREAD_WINDOWS.map((w) => (
            <button
              key={w.min}
              className={`toggle-btn ${spreadWindow === w.min ? 'active' : ''}`}
              onClick={() => setSpreadWindow(w.min)}
              title={`Maior spread de entrada e menor de saída observados nos últimos ${w.label}`}
            >
              {w.label}
            </button>
          ))}
        </div>
        <div className="spread-mode-toggle">
          <span className="toggle-label">Cruzamentos em:</span>
          {CROSSING_WINDOWS.map((w) => (
            <button
              key={w.key}
              className={`toggle-btn ${crossingWindow === w.key ? 'active' : ''}`}
              onClick={() => setCrossingWindow(w.key)}
              title={`Mostrar quantos cruzamentos de sinal ocorreram nas últimas ${w.label}`}
            >
              {w.label}
            </button>
          ))}
        </div>
        <button
          className={`bot-action-btn ${resetExtremesConfirm ? 'confirm' : ''}`}
          onClick={handleResetExtremes}
          disabled={resettingExtremes}
          title="Apaga os recordes de mín/máx histórico de spread. Útil para descartar picos registrados a partir de preços não-executáveis."
        >
          {resetExtremesConfirm ? 'Confirmar reset?' : '↺ Resetar mín/máx'}
        </button>
        <div className="result-count">
          {sorted.length} pares
          {hiddenPairs > 0 && (
            <span
              className="hidden-pairs-note"
              title="Pares cujo preço de futures não vem do book (sem ⚡) são ocultados: o último preço negociado pode estar muito descolado do executável, gerando spreads fictícios."
            >
              {' '}· {hiddenPairs} ocultos (sem book)
            </span>
          )}
        </div>
      </div>

      <div className="table-scroll virtualized" ref={virtual.containerRef} onScroll={virtual.onScroll}>
        <table className="arb-table">
          <thead>
            <tr>
              {COLUMNS.map((col) => (
                <th
                  key={col.key}
                  onClick={col.sortable ? () => handleSort(col.key) : undefined}
                  className={col.sortable ? 'sortable' : ''}
                  style={{ textAlign: col.align }}
                >
                  {col.key === 'crossings_count' ? `Cruzamentos (${crossingWindowLabel})` : col.label}
                  {col.key === 'spread_pct' && (
                    <span className="col-sub-hint"> ({spreadSortMode === 'abs' ? 'abs' : 'real'})</span>
                  )}
                  {sortKey === col.key && (
                    <span className="sort-arrow">{sortDir === 'asc' ? ' ▲' : ' ▼'}</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {/* Espacadores: linhas fora da tela viram altura pura. Sem isso,
                580 linhas x 15 colunas + 577 SVGs ficavam vivas no DOM e
                re-renderizavam a cada snapshot. */}
            {virtual.paddingTop > 0 && (
              <tr style={{ height: virtual.paddingTop }} aria-hidden="true" />
            )}
            {janela.map((p) => (
              <Row
                key={p.symbol}
                pair={p}
                getFlash={getFlash}
                getSparkline={getSparkline}
                crossingWindow={crossingWindow}
                crossingWindowLabel={crossingWindowLabel}
                spreadWindow={spreadWindow}
              />
            ))}
            {virtual.paddingBottom > 0 && (
              <tr style={{ height: virtual.paddingBottom }} aria-hidden="true" />
            )}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={COLUMNS.length} className="empty-row">
                  {list.length === 0 ? 'Aguardando dados da MEXC...' : 'Nenhum par corresponde ao filtro.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * Profundidade disponivel no topo do book, em USDT.
 *
 * O preco sozinho nao diz quanto cabe nele. Um spread de 2% sobre $2 de
 * profundidade nao e uma oportunidade de 2% -- vira prejuizo assim que a
 * ordem anda o book. Foi esse numero invisivel que consumiu 1,82 ponto
 * percentual na operacao de 03/08 (ver bot/depth.py).
 *
 * Vazio (nao "0") quando ainda nao foi medido: o ticker de futures da MEXC
 * nao traz quantidade, entao ela chega por consulta de profundidade, que e
 * priorizada e nao cobre todos os ~580 pares. "Nao medido" e "sem liquidez"
 * sao coisas diferentes e nao podem parecer a mesma na tela.
 */
function DepthHint({ usdt, lado }) {
  if (usdt === null || usdt === undefined) return <div className="depth-hint unknown" />;
  const classe = usdt < 50 ? 'thin' : usdt < 500 ? 'medium' : 'deep';
  const texto = usdt >= 1_000_000 ? `$${(usdt / 1_000_000).toFixed(2)}M`
    : usdt >= 1_000 ? `$${(usdt / 1_000).toFixed(2)}K`
    : `$${usdt.toFixed(2)}`;
  return (
    <Tooltip content={`Da para ${lado} cerca de ${texto} a este preco. Acima disso a ordem anda o book e o preco piora.`}>
      <div className={`depth-hint ${classe}`}>{texto}</div>
    </Tooltip>
  );
}

/**
 * Volatilidade = amplitude de 24h como percentual da minima.
 *
 * Responde "quanto este ativo andou hoje", que e o risco a que a perna
 * descoberta fica exposta se uma das pontas falhar -- o cenario do bug 15,
 * em que um short ficou 6 minutos sem hedge. Num ativo de 200% de amplitude
 * diaria, esses minutos valem muito mais que num de 1%.
 */
function VolatilityCell({ pct, futuresPct, high, low }) {
  if (pct === null || pct === undefined) return <span className="dim">—</span>;
  const classe = pct >= 50 ? 'vol-alta' : pct >= 15 ? 'vol-media' : 'vol-baixa';
  return (
    <Tooltip
      content={
        `Amplitude de 24h no spot: ${formatPrice(low)} ate ${formatPrice(high)}` +
        (futuresPct != null ? ` · Futuros: ${futuresPct.toFixed(1)}%` : '')
      }
    >
      <span className={classe}>{pct.toFixed(1)}%</span>
    </Tooltip>
  );
}

/**
 * Melhor spread observado na janela selecionada.
 *
 * `melhor` diz qual extremo interessa: na ENTRADA e o maximo (entra-se no
 * spread mais alto), na SAIDA e o minimo (sai-se no mais baixo). O tooltip
 * traz as quatro janelas de uma vez, para comparar sem trocar o seletor --
 * um par parado agora que bateu 4% ha meia hora e uma oportunidade dormindo,
 * e essa e a leitura que a coluna sozinha nao da.
 */
function WindowCell({ p, prefixo, janela, positivoQuando }) {
  const valor = p[`${prefixo}_${janela}m`];
  if (valor === null || valor === undefined) return <span className="dim">—</span>;

  const bom = positivoQuando === 'alto' ? valor > 0 : valor < 0;
  const linhas = SPREAD_WINDOWS.map((w) => {
    const v = p[`${prefixo}_${w.min}m`];
    return `${w.label}: ${v === null || v === undefined ? '—' : formatSpread(v)}`;
  }).join(' · ');

  return (
    <Tooltip content={linhas}>
      <span className={bom ? 'positive' : 'negative'}>{formatSpread(valor)}</span>
    </Tooltip>
  );
}

function Row({ pair: p, getFlash, getSparkline, crossingWindow, crossingWindowLabel, spreadWindow }) {
  const spreadPositive = p.spread_pct !== null && p.spread_pct >= 0;
  const sparklineData = getSparkline(p.symbol);
  const crossingsInWindow = p[crossingWindow] ?? 0;

  const hasRange = p.min_spread_pct !== null && p.min_spread_pct !== undefined &&
    p.max_spread_pct !== null && p.max_spread_pct !== undefined;

  const hasExitRange = p.min_exit_spread_pct !== null && p.min_exit_spread_pct !== undefined &&
    p.max_exit_spread_pct !== null && p.max_exit_spread_pct !== undefined;

  return (
    <tr className="table-row">
      <td className="col-symbol">{p.symbol}</td>

      <td className="mono num-cell">
        <FlashCell flash={getFlash(p.symbol, 'spot_price')}>{formatPrice(p.spot_price)}</FlashCell>
        {p.spot_price_source === 'websocket' && (
          <span className="spot-source-badge" title="Preço via WebSocket (tempo real, validado contra REST)">
            ⚡
          </span>
        )}
        <DepthHint usdt={p.spot_ask_usdt} lado="comprar" />
      </td>

      <td className="mono num-cell">
        <FlashCell flash={getFlash(p.symbol, 'futures_price')}>{formatPrice(p.futures_price)}</FlashCell>
        {p.futures_price_source === 'book' && (
          <span className="spot-source-badge" title="Preço de execução (bid do book, atualiza ~1s)">
            ⚡
          </span>
        )}
        <DepthHint usdt={p.futures_bid_usdt} lado="vender" />
      </td>

      <td className="mono num-cell">
        <VolatilityCell pct={p.volatility_pct} futuresPct={p.futures_volatility_pct}
                        high={p.spot_high_24h} low={p.spot_low_24h} />
      </td>

      <td className="mono num-cell volume-cell">
        {formatVolume(p.spot_vol)} / {formatVolume(p.futures_vol)}
      </td>

      <td className="mono num-cell">
        <FlashCell flash={getFlash(p.symbol, 'spread_pct')}>
          <span className={spreadPositive ? 'positive' : 'negative'}>{formatSpread(p.spread_pct)}</span>
        </FlashCell>
      </td>

      <td className="mono num-cell">
        {p.exit_spread_pct !== null && p.exit_spread_pct !== undefined ? (
          <span className={p.exit_spread_pct >= 0 ? 'positive' : 'negative'}>
            {formatSpread(p.exit_spread_pct)}
          </span>
        ) : (
          '—'
        )}
      </td>

      <td className="mono num-cell spread-range-cell">
        {hasRange ? (
          <Tooltip
            content={
              `Mínimo: ${formatTimeAgo(p.min_spread_ts)} · Máximo: ${formatTimeAgo(p.max_spread_ts)}`
            }
          >
            <span>
              <span className="negative">{formatSpread(p.min_spread_pct)}</span>
              {' / '}
              <span className="positive">{formatSpread(p.max_spread_pct)}</span>
            </span>
          </Tooltip>
        ) : (
          '—'
        )}
      </td>

      <td className="mono num-cell spread-range-cell">
        {hasExitRange ? (
          <Tooltip
            content={
              `Mínimo: ${formatTimeAgo(p.min_exit_spread_ts)} · Máximo: ${formatTimeAgo(p.max_exit_spread_ts)}`
            }
          >
            <span>
              <span className="negative">{formatSpread(p.min_exit_spread_pct)}</span>
              {' / '}
              <span className="positive">{formatSpread(p.max_exit_spread_pct)}</span>
            </span>
          </Tooltip>
        ) : (
          '—'
        )}
      </td>

      <td className="mono num-cell">
        <WindowCell p={p} prefixo="max_entry" janela={spreadWindow} positivoQuando="alto" />
      </td>
      <td className="mono num-cell">
        <WindowCell p={p} prefixo="min_exit" janela={spreadWindow} positivoQuando="baixo" />
      </td>

      <td className="mono num-cell">
        <span className={p.funding_rate >= 0 ? 'positive-soft' : 'negative-soft'}>
          {formatFunding(p.funding_rate)}
        </span>
      </td>

      <td className="mono num-cell crossings-cell">
        <Tooltip
          content={
            `Últimas ${crossingWindowLabel}: ${crossingsInWindow} cruzamento(s)` +
            (p.last_crossing_ts ? ` · Último: ${formatTimeAgo(p.last_crossing_ts)}` : ' · Nenhum cruzamento ainda')
          }
        >
          <span className="crossings-badge">{crossingsInWindow}</span>
        </Tooltip>
      </td>

      <td className="sparkline-cell">
        <Sparkline data={sparklineData} />
      </td>

      <td className="link-cell">
        <Tooltip content={`Abre o gráfico do futuro. Clique em "⊕ Compare" no TradingView e busque ${p.symbol}USDT para sobrepor o spot.`}>
          <a
            href={buildTradingViewCompareUrl(p.symbol)}
            target="_blank"
            rel="noopener noreferrer"
            title="Abrir no TradingView para comparar Spot x Futures"
          >
            📊
          </a>
        </Tooltip>
      </td>

      <td className="link-cell">
        <a href={p.futures_link} target="_blank" rel="noopener noreferrer" title="Abrir contrato futuro na MEXC">
          ⚡
        </a>
      </td>

      <td className="link-cell">
        <a href={p.spot_link} target="_blank" rel="noopener noreferrer" title="Abrir par spot na MEXC">
          💰
        </a>
      </td>
    </tr>
  );
}
