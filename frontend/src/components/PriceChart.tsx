import { useMemo, useRef, useState } from "react";
import type { MacdSeries, Num } from "../lib/indicators";

export interface PricePoint {
  t: string;
  c: number;
  h?: number | null;
  l?: number | null;
  v?: number | null;
}

export interface OverlayLine {
  key: string;
  label: string;
  color: string;
  values: Num[]; // aligned 1:1 with points
  dash?: boolean;
}

export interface ChartMarker {
  index: number; // bar index the marker sits on
  kind: "buy" | "sell";
  price: number;
  label: string; // tooltip text (reason, P&L…)
}

export interface ChartOverlays {
  priceLines?: OverlayLine[]; // MAs / EMAs / ATR-stop drawn ON the price panel
  bollinger?: { upper: Num[]; lower: Num[]; mid: Num[] } | null;
  markers?: ChartMarker[];
  volume?: boolean;
  macd?: MacdSeries | null;
  rsi?: Num[] | null;
  rs?: { values: Num[]; label: string } | null;
}

// Data-viz series colors, legible on both light and dark grounds. These are
// intentionally NOT the app accent — they're categorical encodings.
export const SERIES_COLORS = {
  ma50: "#e0a13b", // warm gold
  ma200: "#3ec8c8", // cyan — deliberately a different hue from MA50 so the two don't blur
  ema9: "#3b9ae0",
  ema21: "#8a5cf6",
  atrStop: "#e04b6a",
  bollinger: "#7f8c9b",
  macd: "#3b9ae0",
  signal: "#e0a13b",
  rs: "#2fae7a",
};

/** Daily price chart with optional, independently-toggled review overlays:
 *  moving-average / EMA / ATR-stop lines and Bollinger bands on the price panel,
 *  buy/sell journal markers, and stacked sub-panels (volume, MACD, RSI,
 *  relative-strength). All panels share one x-axis and one hover crosshair.
 *  Everything is DISPLAY-ONLY — nothing here drives a trade. */
export default function PriceChart({
  points,
  overlays = {},
  height = 320,
}: {
  points: PricePoint[];
  overlays?: ChartOverlays;
  height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  // `zoom` is the visible index window [start, end] (null = full range). Drag a
  // horizontal range on the plot to set it; "Reset zoom" / double-click clears it.
  // Every panel (price + volume/MACD/RSI/RS) shares this window via the x mapping.
  const [zoom, setZoom] = useState<[number, number] | null>(null);
  const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const W = 900;
  const padL = 58;
  const padR = 16;
  const padT = 14;

  // Panel stack: the price panel plus whichever sub-panels are enabled. Each has
  // its own height and vertical offset; they share the x mapping.
  const subPanels: { key: string; h: number }[] = [];
  if (overlays.volume) subPanels.push({ key: "volume", h: 66 });
  if (overlays.macd) subPanels.push({ key: "macd", h: 96 });
  if (overlays.rsi) subPanels.push({ key: "rsi", h: 90 });
  if (overlays.rs) subPanels.push({ key: "rs", h: 90 });

  const GAP = 26; // room for a sub-panel's bottom date/axis + spacing
  const priceH = height;
  const totalH = padT + priceH + subPanels.reduce((s, p) => s + p.h + GAP, 0) + 30;

  const n = points.length;
  // Visible window, clamped so a stale zoom from a previous symbol/range can't
  // point past the current data. All panels map indices against this window.
  const viewStart = zoom ? Math.max(0, Math.min(zoom[0], n - 1)) : 0;
  const viewEnd = zoom ? Math.min(n - 1, Math.max(zoom[1], viewStart + 1)) : n - 1;
  const visN = viewEnd - viewStart + 1; // number of visible bars — sets bar widths
  // Map the visible slice across the full plot width so a zoom fills the chart.
  const x = (i: number) => padL + (n <= 1 ? 0 : ((i - viewStart) / (viewEnd - viewStart || 1)) * (W - padL - padR));

  const price = useMemo(() => {
    if (n < 2) return null;
    // The price panel's y-range must contain the price AND any overlay lines /
    // bands drawn on it, or an MA/band would clip off the top or bottom.
    const extra: number[] = [];
    for (const ln of overlays.priceLines ?? []) for (const v of ln.values) if (v != null) extra.push(v);
    if (overlays.bollinger) {
      for (const v of overlays.bollinger.upper) if (v != null) extra.push(v);
      for (const v of overlays.bollinger.lower) if (v != null) extra.push(v);
    }
    const vals = points.map((p) => p.c).concat(extra);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || 1;
    const top = padT;
    const y = (v: number) => top + priceH - ((v - min) / span) * priceH;
    // Draw only the visible slice; the y-scale stays fixed to the full range so
    // gridlines and overlays keep the same price levels as you zoom the x-axis.
    let path = "";
    for (let i = viewStart; i <= viewEnd; i++) path += `${i === viewStart ? "M" : "L"}${x(i).toFixed(1)},${y(points[i].c).toFixed(1)} `;
    path = path.trim();
    const area = `${path} L${x(viewEnd).toFixed(1)},${top + priceH} L${x(viewStart).toFixed(1)},${top + priceH} Z`;
    return { min, max, top, y, path, area, bottom: top + priceH };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, overlays, priceH, viewStart, viewEnd]);

  if (!price) return <p className="hint">Not enough history to chart.</p>;

  const first = points[0].c;
  const last = points[n - 1].c;
  const up = last >= first;
  const stroke = up ? "var(--ok)" : "var(--err)";

  // Vertical offset where each sub-panel starts.
  let cursor = price.bottom + GAP;
  const panelBox: Record<string, { top: number; h: number; bottom: number }> = {};
  for (const p of subPanels) {
    panelBox[p.key] = { top: cursor, h: p.h, bottom: cursor + p.h };
    cursor += p.h + GAP;
  }

  // pointer x (viewBox units) → nearest visible index
  const idxAt = (vx: number) =>
    Math.max(viewStart, Math.min(viewEnd, viewStart + Math.round(((vx - padL) / (W - padL - padR)) * (viewEnd - viewStart))));

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current!.getBoundingClientRect();
    const vx = ((e.clientX - rect.left) / rect.width) * W;
    setHover(idxAt(vx));
    if (drag) setDrag({ ...drag, x1: Math.max(padL, Math.min(W - padR, vx)) });
  }

  function onDown(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current!.getBoundingClientRect();
    const vx = Math.max(padL, Math.min(W - padR, ((e.clientX - rect.left) / rect.width) * W));
    svgRef.current!.setPointerCapture(e.pointerId);
    setDrag({ x0: vx, x1: vx });
  }

  function onUp(e: React.PointerEvent<SVGSVGElement>) {
    if (drag) {
      const rect = svgRef.current!.getBoundingClientRect();
      // A negligible drag is a click, not a zoom — leaves hover behaviour intact.
      const screenDx = Math.abs(drag.x1 - drag.x0) * (rect.width / W);
      if (screenDx >= 6) {
        const a = idxAt(drag.x0);
        const b = idxAt(drag.x1);
        const lo = Math.min(a, b);
        const hi = Math.max(a, b);
        if (hi - lo >= 1) setZoom([lo, hi]);
      }
      try {
        svgRef.current!.releasePointerCapture(e.pointerId);
      } catch {
        /* pointer already released */
      }
      setDrag(null);
    }
  }

  const hp = hover !== null ? points[hover] : null;
  const hoverChange = hp ? ((hp.c / first - 1) * 100).toFixed(2) : null;

  // Build an overlay path helper: skips null gaps (starts a new sub-path).
  const linePath = (values: Num[], y: (v: number) => number) => {
    let d = "";
    let pen = false;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (v == null || i < viewStart || i > viewEnd) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
      pen = true;
    }
    return d.trim();
  };

  // ----- sub-panel scale helpers -----
  const rangeOf = (values: Num[], extra: number[] = []) => {
    const nums = values.filter((v): v is number => v != null).concat(extra);
    if (!nums.length) return { min: 0, max: 1 };
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    return { min, max: max === min ? min + 1 : max };
  };
  const scaleY = (box: { top: number; h: number }, min: number, max: number) => (v: number) =>
    box.top + box.h - ((v - min) / (max - min || 1)) * box.h;

  // Active-legend values at the hovered index (or the last bar when not hovering).
  const li = hover ?? n - 1;

  return (
    <div className="pricechart">
      {zoom && (
        <button type="button" className="chart-zoom-reset" onClick={() => setZoom(null)}>
          Reset zoom
        </button>
      )}
      <div className="chart-readout pc-readout" aria-label="Price readout">
        <div className="cr-price">
          {!hp ? (
            <span className="hint">Hover the chart for values and date.</span>
          ) : (
            `$${hp.c.toLocaleString(undefined, { maximumFractionDigits: 4 })}`
          )}
        </div>
        <div className="cr-meta">
          <span className="cr-date-slot">
            {hp
              ? new Date(hp.t).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
              : "—"}
          </span>
          <span className={`cr-change ${hp ? (Number(hoverChange) >= 0 ? "up" : "down") : ""}`}>
            {hp ? `${Number(hoverChange) >= 0 ? "+" : ""}${hoverChange}% from start of window` : "—"}
          </span>
        </div>
      </div>

      {/* Legend: active price-panel overlays + their value at the hovered bar. */}
      {(overlays.priceLines?.length || overlays.bollinger) && (
        <div className="chart-legend">
          {(overlays.priceLines ?? []).map((ln) => (
            <span key={ln.key} className="legend-item">
              <span className="legend-swatch" style={{ background: ln.color }} />
              {ln.label}
              {ln.values[li] != null ? ` ${(ln.values[li] as number).toFixed(2)}` : ""}
            </span>
          ))}
          {overlays.bollinger && (
            <span className="legend-item">
              <span className="legend-swatch" style={{ background: SERIES_COLORS.bollinger }} />
              Bollinger (20, 2σ)
            </span>
          )}
        </div>
      )}

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${totalH}`}
        onPointerMove={onMove}
        onPointerDown={onDown}
        onPointerUp={onUp}
        onDoubleClick={() => setZoom(null)}
        role="img"
        aria-label="Price history with overlays — drag to zoom, double-click to reset"
      >
        <defs>
          <linearGradient id="pcfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.22" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* ---- price panel gridlines + axis labels ---- */}
        {[0, 0.25, 0.5, 0.75, 1].map((f) => {
          const v = price.max - (price.max - price.min) * f;
          const yy = price.top + f * priceH;
          return (
            <g key={f}>
              <line x1={padL} x2={W - padR} y1={yy} y2={yy} stroke="var(--border)" strokeWidth="1" />
              <text x={padL - 8} y={yy + 4} textAnchor="end" className="chart-label">
                ${v >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(2)}
              </text>
            </g>
          );
        })}

        {/* ---- Bollinger band fill (behind the price line) ---- */}
        {overlays.bollinger &&
          (() => {
            const { upper, lower } = overlays.bollinger!;
            let top = "";
            let pen = false;
            for (let i = 0; i < upper.length; i++) {
              const v = upper[i];
              if (v == null || i < viewStart || i > viewEnd) { pen = false; continue; }
              top += `${pen ? "L" : "M"}${x(i).toFixed(1)},${price.y(v).toFixed(1)} `;
              pen = true;
            }
            let bot = "";
            for (let i = lower.length - 1; i >= 0; i--) {
              const v = lower[i];
              if (v != null && i >= viewStart && i <= viewEnd) bot += `L${x(i).toFixed(1)},${price.y(v).toFixed(1)} `;
            }
            if (!top) return null;
            return (
              <>
                <path d={`${top} ${bot} Z`} fill={SERIES_COLORS.bollinger} fillOpacity="0.10" stroke="none" />
                <path d={linePath(overlays.bollinger!.upper, price.y)} fill="none" stroke={SERIES_COLORS.bollinger} strokeWidth="1" strokeOpacity="0.6" strokeDasharray="2 2" />
                <path d={linePath(overlays.bollinger!.lower, price.y)} fill="none" stroke={SERIES_COLORS.bollinger} strokeWidth="1" strokeOpacity="0.6" strokeDasharray="2 2" />
              </>
            );
          })()}

        <path d={price.area} fill="url(#pcfill)" />
        <path d={price.path} fill="none" stroke={stroke} strokeWidth="1.8" />

        {/* ---- price-panel overlay lines (MA / EMA / ATR-stop) ---- */}
        {(overlays.priceLines ?? []).map((ln) => (
          <path
            key={ln.key}
            d={linePath(ln.values, price.y)}
            fill="none"
            stroke={ln.color}
            strokeWidth="1.4"
            strokeDasharray={ln.dash ? "5 3" : undefined}
            opacity="0.95"
          />
        ))}

        {/* ---- buy/sell markers ---- */}
        {(overlays.markers ?? []).map((m, i) => {
          if (m.index < viewStart || m.index > viewEnd) return null;
          const cx = x(m.index);
          const cy = price.y(m.price);
          const c = m.kind === "buy" ? "var(--ok)" : "var(--err)";
          const tri =
            m.kind === "buy"
              ? `${cx},${cy - 9} ${cx - 6},${cy + 2} ${cx + 6},${cy + 2}` // up triangle above the point
              : `${cx},${cy + 9} ${cx - 6},${cy - 2} ${cx + 6},${cy - 2}`; // down triangle below
          return (
            <g key={i}>
              <polygon points={tri} fill={c} stroke="var(--bg)" strokeWidth="1" />
              <title>{m.label}</title>
            </g>
          );
        })}

        {/* ---- volume sub-panel ---- */}
        {overlays.volume &&
          panelBox.volume &&
          (() => {
            const box = panelBox.volume;
            const vols = points.map((p) => p.v ?? 0);
            const max = Math.max(1, ...vols);
            return (
              <g>
                <text x={padL - 8} y={box.top + 10} textAnchor="end" className="chart-label">Vol</text>
                <line x1={padL} x2={W - padR} y1={box.bottom} y2={box.bottom} stroke="var(--border)" />
                {points.map((p, i) => {
                  if (i < viewStart || i > viewEnd) return null;
                  const h = ((p.v ?? 0) / max) * box.h;
                  const barUp = i === 0 ? true : p.c >= points[i - 1].c;
                  const bw = Math.max(1, (W - padL - padR) / visN - 0.5);
                  return (
                    <rect
                      key={i}
                      x={x(i) - bw / 2}
                      y={box.bottom - h}
                      width={bw}
                      height={h}
                      fill={barUp ? "var(--ok)" : "var(--err)"}
                      opacity="0.5"
                    />
                  );
                })}
              </g>
            );
          })()}

        {/* ---- MACD sub-panel ---- */}
        {overlays.macd &&
          panelBox.macd &&
          (() => {
            const box = panelBox.macd;
            const m = overlays.macd!;
            const { min, max } = rangeOf([...m.macd, ...m.signal, ...m.hist], [0]);
            const y = scaleY(box, min, max);
            const zero = y(0);
            const bw = Math.max(1, (W - padL - padR) / visN - 0.5);
            return (
              <g>
                <text x={padL - 8} y={box.top + 10} textAnchor="end" className="chart-label">MACD</text>
                <line x1={padL} x2={W - padR} y1={zero} y2={zero} stroke="var(--border)" />
                {m.hist.map((v, i) =>
                  v == null || i < viewStart || i > viewEnd ? null : (
                    <rect
                      key={i}
                      x={x(i) - bw / 2}
                      y={Math.min(zero, y(v))}
                      width={bw}
                      height={Math.abs(y(v) - zero)}
                      fill={v >= 0 ? "var(--ok)" : "var(--err)"}
                      opacity="0.45"
                    />
                  ),
                )}
                <path d={linePath(m.macd, y)} fill="none" stroke={SERIES_COLORS.macd} strokeWidth="1.4" />
                <path d={linePath(m.signal, y)} fill="none" stroke={SERIES_COLORS.signal} strokeWidth="1.4" />
              </g>
            );
          })()}

        {/* ---- RSI sub-panel ---- */}
        {overlays.rsi &&
          panelBox.rsi &&
          (() => {
            const box = panelBox.rsi;
            const y = scaleY(box, 0, 100);
            return (
              <g>
                {/* The 50–70 band is the healthy-uptrend zone (bullish momentum,
                    not yet overbought) — shaded green as the "good area to be in". */}
                <rect x={padL} y={y(70)} width={W - padL - padR} height={y(50) - y(70)} fill="var(--ok)" opacity="0.12" />
                <text x={padL - 8} y={box.top + 10} textAnchor="end" className="chart-label">RSI</text>
                {[30, 50, 70].map((lv) => (
                  <g key={lv}>
                    <line x1={padL} x2={W - padR} y1={y(lv)} y2={y(lv)} stroke="var(--border)" strokeDasharray={lv === 50 ? undefined : "3 3"} />
                    <text x={W - padR + 2} y={y(lv) + 3} className="chart-label">{lv}</text>
                  </g>
                ))}
                <path d={linePath(overlays.rsi!, y)} fill="none" stroke={SERIES_COLORS.ema9} strokeWidth="1.4" />
              </g>
            );
          })()}

        {/* ---- relative-strength sub-panel ---- */}
        {overlays.rs &&
          panelBox.rs &&
          (() => {
            const box = panelBox.rs;
            const { min, max } = rangeOf(overlays.rs!.values, [1]);
            const y = scaleY(box, min, max);
            return (
              <g>
                {/* Above 1.0 = out-performing the S&P 500 — the "good" zone, shaded green. */}
                <rect x={padL} y={box.top} width={W - padL - padR} height={Math.max(0, y(1) - box.top)} fill="var(--ok)" opacity="0.12" />
                <text x={padL - 8} y={box.top + 10} textAnchor="end" className="chart-label">RS</text>
                <line x1={padL} x2={W - padR} y1={y(1)} y2={y(1)} stroke="var(--border)" strokeDasharray="3 3" />
                <text x={W - padR + 2} y={y(1) + 3} className="chart-label">1.0</text>
                <path d={linePath(overlays.rs!.values, y)} fill="none" stroke={SERIES_COLORS.rs} strokeWidth="1.4" />
              </g>
            );
          })()}

        {/* ---- shared hover crosshair spanning every panel ---- */}
        {hp && hover! >= viewStart && hover! <= viewEnd && (
          <g>
            <line
              x1={x(hover!)}
              x2={x(hover!)}
              y1={price.top}
              y2={subPanels.length ? panelBox[subPanels[subPanels.length - 1].key].bottom : price.bottom}
              stroke="var(--accent)"
              strokeDasharray="3 3"
            />
            <circle cx={x(hover!)} cy={price.y(hp.c)} r="4" fill="var(--accent)" stroke="var(--bg)" strokeWidth="2" />
          </g>
        )}

        {/* ---- translucent drag-to-zoom selection rectangle (all panels) ---- */}
        {drag && Math.abs(drag.x1 - drag.x0) > 1 && (
          <rect
            x={Math.min(drag.x0, drag.x1)}
            y={price.top}
            width={Math.abs(drag.x1 - drag.x0)}
            height={(subPanels.length ? panelBox[subPanels[subPanels.length - 1].key].bottom : price.bottom) - price.top}
            fill="var(--accent)"
            opacity="0.15"
          />
        )}

        {/* ---- x-axis date labels at the very bottom (reflect the visible window) ---- */}
        <text x={padL} y={totalH - 8} className="chart-label">{new Date(points[viewStart].t).toLocaleDateString()}</text>
        <text x={W - padR} y={totalH - 8} textAnchor="end" className="chart-label">
          {new Date(points[viewEnd].t).toLocaleDateString()}
        </text>
      </svg>
    </div>
  );
}
