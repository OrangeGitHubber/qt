interface Series {
  label: string;
  color: string;
  values: (number | null)[];
}

export default function LineChart({ labels, series }: { labels: string[]; series: Series[] }) {
  const w = 640;
  const h = 220;
  const pad = 36;
  const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  if (labels.length < 2 || all.length === 0) {
    return <p className="hint">Not enough data yet — the scoreboard grows one point per day.</p>;
  }
  const min = Math.min(...all, 0);
  const max = Math.max(...all, 0);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (labels.length - 1)) * (w - pad * 2);
  const y = (v: number) => h - pad - ((v - min) / span) * (h - pad * 2);

  const path = (values: (number | null)[]) => {
    let d = "";
    values.forEach((v, i) => {
      if (v === null) return;
      d += `${d ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    });
    return d;
  };

  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} className="linechart" role="img" aria-label="Performance comparison">
        <line x1={pad} x2={w - pad} y1={y(0)} y2={y(0)} stroke="var(--border)" strokeDasharray="4 4" />
        <text x={pad - 6} y={y(0) + 4} textAnchor="end" className="chart-label">
          0%
        </text>
        <text x={pad - 6} y={y(max) + 4} textAnchor="end" className="chart-label">
          {max.toFixed(1)}%
        </text>
        <text x={pad - 6} y={y(min) + 4} textAnchor="end" className="chart-label">
          {min.toFixed(1)}%
        </text>
        {series.map((s) => (
          <path key={s.label} d={path(s.values)} fill="none" stroke={s.color} strokeWidth="2" />
        ))}
        <text x={pad} y={h - 8} className="chart-label">
          {labels[0]}
        </text>
        <text x={w - pad} y={h - 8} textAnchor="end" className="chart-label">
          {labels[labels.length - 1]}
        </text>
      </svg>
      <div className="legend">
        {series.map((s) => (
          <span key={s.label}>
            <span className="swatch" style={{ background: s.color }} /> {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}
