import { useMemo } from 'react';

export default function Sparkline({ data, width = 84, height = 28 }) {
  const { path, areaPath, isPositive, hasCrossing } = useMemo(() => {
    if (!data || data.length < 2) {
      return { path: '', areaPath: '', isPositive: true, hasCrossing: false };
    }

    const min = Math.min(...data, 0);
    const max = Math.max(...data, 0);
    const range = max - min || 1;

    const stepX = width / (data.length - 1);
    const points = data.map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / range) * height;
      return [x, y];
    });

    const path = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const areaPath = `${path} L${width},${height} L0,${height} Z`;

    const isPositive = data[data.length - 1] >= 0;

    let hasCrossing = false;
    for (let i = 1; i < data.length; i++) {
      if ((data[i - 1] > 0 && data[i] < 0) || (data[i - 1] < 0 && data[i] > 0)) {
        hasCrossing = true;
        break;
      }
    }

    return { path, areaPath, isPositive, hasCrossing };
  }, [data, width, height]);

  if (!path) {
    return (
      <svg width={width} height={height} aria-hidden="true">
        <line x1="0" y1={height / 2} x2={width} y2={height / 2} stroke="var(--border-strong)" strokeWidth="1" strokeDasharray="2,2" />
      </svg>
    );
  }

  const color = isPositive ? 'var(--positive)' : 'var(--negative)';

  return (
    <svg width={width} height={height} aria-hidden="true" style={{ overflow: 'visible' }}>
      <path d={areaPath} fill={color} opacity="0.12" />
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      {hasCrossing && (
        <line x1="0" y1={height / 2} x2={width} y2={height / 2} stroke="var(--text-tertiary)" strokeWidth="1" strokeDasharray="2,2" opacity="0.5" />
      )}
    </svg>
  );
}
