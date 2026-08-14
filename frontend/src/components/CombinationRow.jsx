import VenueBlock from './VenueBlock';
import { formatSpread, formatVolume } from '../utils/format';

/**
 * Uma linha do dashboard multi-exchange: a moeda, onde comprar, onde vender,
 * e quanto sobra depois das taxas.
 *
 * A leitura é da esquerda para a direita como uma frase: "compre CATE na MEXC
 * Spot a 0,0169 (cabem $2,20), venda na MEXC Futures a 0,0174 (cabem $5,38),
 * sobram +1,60%".
 */
export default function CombinationRow({ row, favorite, onToggleFavorite, onOpenDetail }) {
  const positivo = row.net_spread_pct > 0;

  return (
    <div
      className={`combo-row ${row.tradeable ? '' : 'suspect'}`}
      onClick={() => onOpenDetail(row.symbol)}
      title={row.suspect_reason || 'Clique para ver a cotação em cada venue'}
    >
      <button
        className={`combo-star ${favorite ? 'on' : ''}`}
        onClick={(e) => { e.stopPropagation(); onToggleFavorite(row.symbol); }}
        title={favorite ? 'Remover dos favoritos' : 'Marcar como favorito'}
      >
        ★
      </button>

      <div className="combo-symbol">
        <span className="combo-ticker">{row.symbol}</span>
        <div className="combo-tags">
          {row.cross_exchange && (
            <span className="badge cross" title="As duas pernas ficam em exchanges diferentes: exige saldo pré-posicionado nas duas e não dá para netar as posições">
              cross
            </span>
          )}
          {row.kind === 'futures_futures' && (
            <span className="badge fxf" title="Futures × Futures: as duas pernas são contratos perpétuos">
              F×F
            </span>
          )}
          {!row.tradeable && (
            <span className="badge warn" title={row.suspect_reason}>
              suspeita
            </span>
          )}
        </div>
      </div>

      <VenueBlock
        venueKey={row.buy_venue}
        label={row.buy_venue_label}
        symbol={row.symbol}
        price={row.buy_ask}
        topUsdt={row.buy_top_usdt}
        side="buy"
      />

      <span className="combo-arrow">→</span>

      <VenueBlock
        venueKey={row.sell_venue}
        label={row.sell_venue_label}
        symbol={row.symbol}
        price={row.sell_bid}
        topUsdt={row.sell_top_usdt}
        side="sell"
      />

      <div className="combo-metrics">
        <div
          className="combo-metric"
          title="Spread ao DESFAZER a operação (lados opostos do book). O lucro é entrada menos saída."
        >
          <span className="combo-metric-label">saída</span>
          <span className="combo-metric-value dim">{formatSpread(row.exit_spread_pct)}</span>
        </div>
        <div className="combo-metric" title="Taxa de taker das quatro pernas da operação completa">
          <span className="combo-metric-label">taxas</span>
          <span className="combo-metric-value dim">{row.fee_round_trip_pct.toFixed(3)}%</span>
        </div>
        <div className="combo-metric" title="Volume 24h da perna menos líquida">
          <span className="combo-metric-label">vol 24h</span>
          <span className="combo-metric-value dim">{formatVolume(row.vol_usdt)}</span>
        </div>
      </div>

      <div
        className={`combo-spread ${positivo ? 'pos' : 'neg'}`}
        title={`Spread de entrada ${formatSpread(row.entry_spread_pct)} menos ${row.fee_round_trip_pct.toFixed(3)}% de taxas`}
      >
        <span className="combo-spread-value">{formatSpread(row.net_spread_pct)}</span>
        <span className="combo-spread-gross">bruto {formatSpread(row.entry_spread_pct)}</span>
      </div>
    </div>
  );
}
