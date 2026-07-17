import { useMemo, useRef, useState } from "react";

export interface PricePoint {
  t: string;
  c: number;
}

/** Daily price history with a hover crosshair: move the cursor and the date
 *  and price track the line. Pointer events cover mouse and touch. */
export default function PriceChart({ points, height = 320 }: { points: PricePoint[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const W = 900;
  const H = height;
  const padL = 58;
  const padR = 16;
  const padT = 14;
  const padB = 30;

  const model = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.map((p) => p.c);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const x = (i: number) => padL + (i / (points.length - 1)) * (W - padL - padR);
    const y = (v: number) => H - padB - ((v - min) / span) * (H - padT - padB);
    const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join(" ");
    const area = `${path} L${x(points.length - 1).toFixed(1)},${H - padB} L${padL},${H - padB} Z`;
    return { min, max, x, y, path, area };
  }, [points, H]);

  if (!model) return <p className="hint">Not enough history to chart.</p>;

  const first = points[0].c;
  const last = points[points.length - 1].c;
  const up = last >= first;
  const stroke = up ? "var(--ok)" : "var(--err)";

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current!.getBoundingClientRect();
    // the SVG scales via viewBox — convert screen px back to viewBox units
    const vx = ((e.clientX - rect.left) / rect.width) * W;
    const ratio = (vx - padL) / (W - padL - padR);
    const idx = Math.round(ratio * (points.length - 1));
    setHover(Math.max(0, Math.min(points.length - 1, idx)));
  }

  const hp = hover !== null ? points[hover] : null;
  const hoverChange = hp ? ((hp.c / first - 1) * 100).toFixed(2) : null;

  return (
    <div className="pricechart">
      {/* readout sits above the plot so it can't cover the line */}
      <div className="chart-readout">
        {!hp ? (
          <span className="hint">Hover the line for price and date.</span>
        ) : (
          <>
            <strong>${hp.c.toLocaleString(undefined, { maximumFractionDigits: 4 })}</strong>
            <span>
              {new Date(hp.t).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}
            </span>
            <span className={Number(hoverChange) >= 0 ? "up" : "down"}>
              {Number(hoverChange) >= 0 ? "+" : ""}
              {hoverChange}% from start of window
            </span>
          </>
        )}
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
        role="img"
        aria-label="Price history"
      >
        <defs>
          <linearGradient id="pcfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.22" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>

        {[0, 0.25, 0.5, 0.75, 1].map((f) => {
          const v = model.min + (model.max - model.min) * (1 - f);
          const yy = padT + f * (H - padT - padB);
          return (
            <g key={f}>
              <line x1={padL} x2={W - padR} y1={yy} y2={yy} stroke="var(--border)" strokeWidth="1" />
              <text x={padL - 8} y={yy + 4} textAnchor="end" className="chart-label">
                ${v >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(2)}
              </text>
            </g>
          );
        })}

        <path d={model.area} fill="url(#pcfill)" />
        <path d={model.path} fill="none" stroke={stroke} strokeWidth="1.8" />

        {hp && (
          <g>
            <line x1={model.x(hover!)} x2={model.x(hover!)} y1={padT} y2={H - padB} stroke="var(--accent)" strokeDasharray="3 3" />
            <circle cx={model.x(hover!)} cy={model.y(hp.c)} r="4" fill="var(--accent)" stroke="var(--bg)" strokeWidth="2" />
          </g>
        )}

        <text x={padL} y={H - 8} className="chart-label">
          {new Date(points[0].t).toLocaleDateString()}
        </text>
        <text x={W - padR} y={H - 8} textAnchor="end" className="chart-label">
          {new Date(points[points.length - 1].t).toLocaleDateString()}
        </text>
      </svg>
    </div>
  );
}
