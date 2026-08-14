import { formatPrice } from '../utils/format';

/**
 * Cores de marca por exchange. Usadas no avatar e na etiqueta do mercado.
 *
 * Avatar é a inicial sobre a cor da marca, não o logo real: logos são ativos
 * de terceiros e teriam que ser baixados de fora — o que a política de
 * conteúdo da página bloqueia e traria uma dependência de rede para renderizar
 * uma tabela que precisa ser instantânea.
 */
const EXCHANGE_STYLE = {
  mexc: { color: '#00b897', short: 'M' },
  gate: { color: '#17a2f3', short: 'G' },
  bingx: { color: '#2b6cff', short: 'B' },
};

const TRADE_URL = {
  'mexc:spot': (s) => `https://www.mexc.com/exchange/${s}_USDT`,
  'mexc:futures': (s) => `https://futures.mexc.com/exchange/${s}_USDT`,
  'gate:spot': (s) => `https://www.gate.io/trade/${s}_USDT`,
  'gate:futures': (s) => `https://www.gate.io/futures/USDT/${s}_USDT`,
  'bingx:spot': (s) => `https://bingx.com/en/spot/${s}USDT`,
  'bingx:futures': (s) => `https://bingx.com/en/perpetual/${s}-USDT`,
};

/**
 * Um lado da operação: onde executar, a que preço, e QUANTO cabe nesse preço.
 *
 * O valor em dólares embaixo do preço é a informação que o preço sozinho não
 * dá. Um spread de 2,6% sobre $9,93 de profundidade não é uma oportunidade de
 * 2,6% — é uma oportunidade de alguns centavos que vai virar prejuízo assim
 * que a ordem andar o book. Foi exatamente esse número invisível que custou
 * 1,82 ponto percentual na operação de 03/08.
 */
export default function VenueBlock({ venueKey, label, symbol, price, topUsdt, side }) {
  const [exchange, market] = venueKey.split(':');
  const style = EXCHANGE_STYLE[exchange] || { color: '#7d8798', short: '?' };
  const marketTag = market === 'spot' ? 'S' : 'F';
  const url = TRADE_URL[venueKey]?.(symbol);

  return (
    <div className="venue-block">
      <div className="venue-head">
        <span className="venue-avatar" style={{ background: style.color }}>
          {style.short}
        </span>
        <span className="venue-name">{label.replace(/ (Spot|Futures)$/, '')}</span>
        <span className={`venue-market ${market}`}>({marketTag})</span>
        {url && (
          <a
            className="venue-link"
            href={url}
            target="_blank"
            rel="noreferrer noopener"
            onClick={(e) => e.stopPropagation()}
            title={`Abrir ${symbol} na ${exchange.toUpperCase()}`}
          >
            ↗
          </a>
        )}
      </div>
      <div className="venue-price">{formatPrice(price)}</div>
      <div className={`venue-depth ${depthClass(topUsdt)}`} title={depthTitle(topUsdt, side)}>
        {topUsdt == null ? '—' : formatUsd(topUsdt)}
      </div>
    </div>
  );
}

function formatUsd(v) {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(2)}K`;
  return `$${v.toFixed(2)}`;
}

/**
 * Profundidade minúscula é o sinal mais importante da linha, então ela ganha
 * cor: abaixo de $50 a "oportunidade" quase certamente não sobrevive às
 * taxas nem ao primeiro nível do book.
 */
function depthClass(v) {
  if (v == null) return 'unknown';
  if (v < 50) return 'thin';
  if (v < 500) return 'medium';
  return 'deep';
}

function depthTitle(v, side) {
  const lado = side === 'buy' ? 'comprar' : 'vender';
  if (v == null) return 'Profundidade ainda não medida para este venue';
  return `Dá para ${lado} cerca de ${formatUsd(v)} ao preço do topo do book. ` +
    'Acima disso a ordem anda o book e o preço piora.';
}
