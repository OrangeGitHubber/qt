// Daily per-strategy P&L as stacked bars: one bar per day, each segment a
// strategy's realized P&L that day — positives stack up from the zero line,
// negatives stack down. Answers "who contributed what, and when".

export type PnlSeries = { name: string; color: string; values: number[] };

function fmt(v: number): string {
  return `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`;
}

export default function StackedPnlBars({ days, series }: { days: string[]; series: PnlSeries[] }) {
  const W = 720;
  const H = 200;
  const padL = 10;
  const padR = 10;
  const padT = 12;
  const padB = 22;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = days.length;

  // Scale so the tallest positive stack and the deepest negative stack both fit.
  let maxPos = 0;
  let maxNeg = 0;
  for (let d = 0; d < n; d++) {
    let pos = 0;
    let neg = 0;
    for (const s of series) {
      const v = s.values[d] || 0;
      if (v > 0) pos += v;
      else neg += -v;
    }
    maxPos = Math.max(maxPos, pos);
    maxNeg = Math.max(maxNeg, neg);
  }
  const span = maxPos + maxNeg || 1;
  const zeroY = padT + (maxPos / span) * plotH;
  const px = (v: number) => (v / span) * plotH; // dollars → pixels
  const slot = plotW / Math.max(n, 1);
  const barW = Math.max(2, Math.min(slot * 0.72, 26));

  // Label at most ~6 days so the axis never crowds.
  const step = Math.max(1, Math.ceil(n / 6));
  const labelIdx = days.map((_, i) => i).filter((i) => i % step === 0 || i === n - 1);

  return (
    <svg className="stacked-bars" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Daily realized P&L by strategy">
      <line x1={padL} x2={W - padR} y1={zeroY} y2={zeroY} className="axis-line" />
      {days.map((day, d) => {
        const x = padL + slot * d + slot / 2 - barW / 2;
        let up = zeroY;
        let down = zeroY;
        return (
          <g key={day}>
            {series.map((s) => {
              const v = s.values[d] || 0;
              if (!v) return null;
              const h = px(Math.abs(v));
              let y: number;
              if (v > 0) {
                up -= h;
                y = up;
              } else {
                y = down;
                down += h;
              }
              return (
                <rect key={s.name} x={x} y={y} width={barW} height={h} fill={s.color} opacity={v > 0 ? 1 : 0.85}>
                  <title>{`${day} · ${s.name}: ${fmt(v)}`}</title>
                </rect>
              );
            })}
          </g>
        );
      })}
      {labelIdx.map((d) => (
        <text key={d} x={padL + slot * d + slot / 2} y={H - 6} className="chart-label" textAnchor="middle">
          {days[d].slice(5)}
        </text>
      ))}
    </svg>
  );
}
