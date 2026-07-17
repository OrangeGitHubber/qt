import { useMemo, useRef, useState } from "react";

interface Series {
  label: string;
  color: string;
  values: (number | null)[];
}

export interface ChartMarker {
  index: number;
  kind: "buy" | "sell";
  text: string;
}

/** Multi-series % chart with a hover crosshair and optional trade markers.
 *  Hovering reports the date and every series' value at that point, so the
 *  lines don't have to be decoded from the legend alone. */
export default function LineChart({
  labels,
  series,
  markers = [],
}: {
  labels: string[];
  series: Series[];
  markers?: ChartMarker[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const W = 900;
  const H = 300;
  const padL = 52;
  const padR = 16;
  const padT = 14;
  const padB = 30;

  const model = useMemo(() => {
    const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
    if (labels.length < 2 || all.length === 0) return null;
    const min = Math.min(...all, 0);
    const max = Math.max(...all, 0);
    const span = max - min || 1;
    const x = (i: number) => padL + (i / (labels.length - 1)) * (W - padL - padR);
    const y = (v: number) => H - padB - ((v - min) / span) * (H - padT - padB);
    return { min, max, x, y };
  }, [labels, series]);

  if (!model) return <p className="hint">Not enough data yet — the chart grows one point per day.</p>;

  const path = (values: (number | null)[]) =>
    values.reduce((d, v, i) => (v === null ? d : `${d}${d ? "L" : "M"}${model.x(i).toFixed(1)},${model.y(v).toFixed(1)} `), "");

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current!.getBoundingClientRect();
    const vx = ((e.clientX - rect.left) / rect.width) * W;
    const idx = Math.round(((vx - padL) / (W - padL - padR)) * (labels.length - 1));
    setHover(Math.max(0, Math.min(labels.length - 1, idx)));
  }

  const hoverMarkers = hover !== null ? markers.filter((m) => m.index === hover) : [];
  const tipLeftPct = hover !== null ? (model.x(hover) / W) * 100 : 0;
  const flip = tipLeftPct > 60;

  return (
    <div className="pricechart">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="linechart"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
        role="img"
        aria-label="Performance comparison"
      >
        <line x1={padL} x2={W - padR} y1={model.y(0)} y2={model.y(0)} stroke="var(--border)" strokeDasharray="4 4" />
        <text x={padL - 6} y={model.y(0) + 4} textAnchor="end" className="chart-label">0%</text>
        <text x={padL - 6} y={model.y(model.max) + 4} textAnchor="end" className="chart-label">
          {model.max.toFixed(1)}%
        </text>
        <text x={padL - 6} y={model.y(model.min) + 4} textAnchor="end" className="chart-label">
          {model.min.toFixed(1)}%
        </text>

        {series.map((s) => (
          <path key={s.label} d={path(s.values)} fill="none" stroke={s.color} strokeWidth="2" />
        ))}

        {/* trade markers ride the first series (the strategy's own equity) */}
        {markers.map((m, i) => {
          const v = series[0]?.values[m.index];
          if (v == null) return null;
          const cx = model.x(m.index);
          const cy = model.y(v);
          const up = m.kind === "buy";
          const d = up
            ? `M${cx},${cy - 9} l4,7 l-8,0 Z`
            : `M${cx},${cy + 9} l4,-7 l-8,0 Z`;
          return <path key={`${m.kind}-${m.index}-${i}`} d={d} fill={up ? "var(--ok)" : "var(--err)"} />;
        })}

        {hover !== null && (
          <g>
            <line x1={model.x(hover)} x2={model.x(hover)} y1={padT} y2={H - padB} stroke="var(--accent)" strokeDasharray="3 3" />
            {series.map((s) =>
              s.values[hover] == null ? null : (
                <circle key={s.label} cx={model.x(hover)} cy={model.y(s.values[hover]!)} r="3.5"
                  fill={s.color} stroke="var(--bg)" strokeWidth="1.5" />
              ),
            )}
          </g>
        )}

        <text x={padL} y={H - 8} className="chart-label">{labels[0]}</text>
        <text x={W - padR} y={H - 8} textAnchor="end" className="chart-label">{labels[labels.length - 1]}</text>
      </svg>

      {hover !== null && (
        <div className={`pc-tip ${flip ? "flip" : ""}`} style={{ left: `${tipLeftPct}%` }}>
          <strong>{labels[hover]}</strong>
          {series.map((s) => (
            <span key={s.label} className="tip-row">
              <span className="swatch" style={{ background: s.color }} />
              {s.label}: <b className={(s.values[hover] ?? 0) >= 0 ? "up" : "down"}>
                {s.values[hover] == null ? "—" : `${s.values[hover]! >= 0 ? "+" : ""}${s.values[hover]}%`}
              </b>
            </span>
          ))}
          {hoverMarkers.map((m, i) => (
            <span key={i} className={m.kind === "buy" ? "up" : "down"}>
              {m.kind === "buy" ? "▲ " : "▼ "}
              {m.text}
            </span>
          ))}
        </div>
      )}

      <div className="legend">
        {series.map((s) => (
          <span key={s.label}>
            <span className="swatch" style={{ background: s.color }} /> {s.label}
          </span>
        ))}
        {markers.length > 0 && (
          <>
            <span><span className="marker-key up">▲</span> bought</span>
            <span><span className="marker-key down">▼</span> sold</span>
          </>
        )}
      </div>
    </div>
  );
}
