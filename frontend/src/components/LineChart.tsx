import { useEffect, useMemo, useRef, useState } from "react";

interface Series {
  label: string;
  color: string;
  values: (number | null)[];
}

export interface ChartMarker {
  index: number;
  kind: "buy" | "sell";
  text: string;
  // Which series line the marker rides (default 0 — the first/strategy line).
  // Comparison charts put strategy B's trades on series 1 so each strategy's
  // markers sit on its own equity line.
  seriesIndex?: number;
  // Colour for this entry in the detail panel. Defaults to the kind (buys green,
  // sells red) — but a sell is red only because it's a sell, and a PROFITABLE
  // sell printed in red reads as a loss. Callers that know the outcome pass it.
  tone?: "up" | "down";
}

// One holding's contribution to a single day's move (qty 0 = closed that day),
// in PERCENTAGE POINTS of the account — the same unit as the chart's y-axis, so
// the day's holdings sum to the line's visible day-over-day move.
export interface DayHolding {
  symbol: string;
  qty: number;
  day_pnl_pct: number;
}

/** Multi-series % chart with a hover crosshair and optional trade markers.
 *  Hovering reports the date and every series' value at that point, so the
 *  lines don't have to be decoded from the legend alone. */
export default function LineChart({
  labels,
  series,
  markers,
  noTradeReasons,
  holdings,
  holdingsLabel,
  onZoomChange,
}: {
  labels: string[];
  series: Series[];
  markers?: ChartMarker[];
  // {day label -> why no entry} — shown on the panel when you land on a day the
  // strategy traded nothing, so a flat stretch is explained, not a mystery.
  noTradeReasons?: Record<string, string>;
  // {day label -> holdings}: what was held that day and each holding's dollar
  // contribution to the day's move — so a trade-less rise/fall is attributable.
  holdings?: Record<string, DayHolding[]>;
  // Noun for the holdings line ("2 open:" on a backtest, "2 strategies:" on the
  // scoreboard). When set, every row is listed and `qty` is ignored.
  holdingsLabel?: string;
  // Reports the visible index window [start, end] while zoomed (null = full
  // range), so the parent can show a trade log scoped to what's on screen.
  onZoomChange?: (range: [number, number] | null) => void;
}) {
  // A caller that passes no markers has NOT told us there were no trades — it
  // has told us nothing about trades. Conflating the two made the scoreboard,
  // which never passed any, announce "no trades this day" on every single day.
  const hasTradeData = markers !== undefined;
  const marks = markers ?? [];

  // `hover` is sticky: once you've moved over a day it stays selected after the
  // cursor leaves, so a value can be read without it blanking the instant you
  // reach for it. It only resets when new data arrives.
  const [hover, setHover] = useState<number | null>(null);
  // `zoom` is the visible index window [start, end] (null = full range). Drag a
  // horizontal range on the plot to set it; "Reset zoom" / double-click clears it.
  // `drag` holds the in-progress selection in viewBox x-coords while the pointer
  // is down, so we can draw the translucent selection rectangle.
  const [zoom, setZoom] = useState<[number, number] | null>(null);
  const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const W = 900;
  const H = 300;
  const padL = 52;
  const padR = 16;
  const padT = 14;
  const padB = 30;

  // Visible window, clamped so a stale zoom from a previous dataset can't point
  // past the current data. Everything below maps against [viewStart, viewEnd].
  const lastIdx = labels.length - 1;
  const viewStart = zoom ? Math.max(0, Math.min(zoom[0], lastIdx)) : 0;
  const viewEnd = zoom ? Math.min(lastIdx, Math.max(zoom[1], viewStart + 1)) : lastIdx;

  const model = useMemo(() => {
    const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
    if (labels.length < 2 || all.length === 0) return null;
    const min = Math.min(...all, 0);
    const max = Math.max(...all, 0);
    const span = max - min || 1;
    // Map the visible slice across the full plot width so a zoom fills the chart.
    const denom = viewEnd - viewStart || 1;
    const x = (i: number) => padL + ((i - viewStart) / denom) * (W - padL - padR);
    const y = (v: number) => H - padB - ((v - min) / span) * (H - padT - padB);
    return { min, max, x, y };
  }, [labels, series, viewStart, viewEnd]);

  // Report the visible window up so the parent can scope a trade log to it.
  useEffect(() => {
    onZoomChange?.(zoom ? [viewStart, viewEnd] : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zoom, viewStart, viewEnd]);

  // A new dataset (different day range) clears a stale zoom so it can't point at
  // the previous run's window.
  useEffect(() => {
    setZoom(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [labels.length, labels[0], labels[labels.length - 1]]);

  if (!model) return <p className="hint">Not enough data yet — the chart grows one point per day.</p>;

  // Only the points inside the window are drawn; the rest are clipped so a zoomed
  // line doesn't spill into the axis gutters.
  const path = (values: (number | null)[]) =>
    values.reduce(
      (d, v, i) =>
        v === null || i < viewStart || i > viewEnd
          ? d
          : `${d}${d ? "L" : "M"}${model.x(i).toFixed(1)},${model.y(v).toFixed(1)} `,
      "",
    );

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

  const hoverMarkers = hover !== null ? marks.filter((m) => m.index === hover) : [];

  return (
    <div className="pricechart">
      {zoom && (
        <button type="button" className="chart-zoom-reset" onClick={() => setZoom(null)}>
          Reset zoom
        </button>
      )}
      {/* Fixed strip above the plot: date + one slot per series. It is always
          exactly two rows (date line, series line) so its height is constant —
          the chart below never shifts — and only the digits inside the fixed
          slots change as the cursor sweeps. The variable-length TRADE detail is
          deliberately NOT here; it lives below the chart so it can wrap and be
          read in full without pushing these numbers (or the chart) around. */}
      <div className="chart-readout" aria-label="Chart readout">
        {/* Every line is rebased to zero on the window's FIRST day, so the same
            calendar date reads differently in a 150-day run and a 500-day one —
            +0% at the start of one, +22% partway through the other. Naming the
            baseline stops two runs being read against each other as if the
            numbers shared an origin. */}
        <div className="cr-date">
          {hover === null ? "Hover the chart for values" : labels[hover]}
          {hover !== null && labels.length > 1 && (
            <span className="cr-since"> since {labels[0]}</span>
          )}
        </div>
        <div className="cr-series" style={{ gridTemplateColumns: `repeat(${series.length}, minmax(0, 1fr))` }}>
          {series.map((s) => {
            const v = hover === null ? null : s.values[hover];
            return (
              <div key={s.label} className="cr-slot">
                <span className="swatch" style={{ background: s.color }} />
                <span className="cr-label" title={s.label}>
                  {s.label}
                </span>
                <span className={`cr-val ${v == null ? "" : v >= 0 ? "up" : "down"}`}>
                  {v == null ? "—" : `${v >= 0 ? "+" : ""}${v}%`}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="linechart"
        onPointerMove={onMove}
        onPointerDown={onDown}
        onPointerUp={onUp}
        onDoubleClick={() => setZoom(null)}
        role="img"
        aria-label="Performance comparison — drag to zoom, double-click to reset"
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

        {/* Trade markers ride their own series line (default the first).
            Buy vs sell is carried by SHAPE and PLACEMENT — triangle pointing up
            above the line, pointing down below it — never by colour. Green and
            red mean profit and loss everywhere else here, and a marker can't
            honestly claim either: a buy has no result yet, and a day holding
            several buys and sells has no single outcome to colour. Colour is
            spent on WHICH LINE the trade belongs to instead, which is the thing
            you actually can't otherwise tell in compare mode. */}
        {marks.map((m, i) => {
          if (m.index < viewStart || m.index > viewEnd) return null;
          const s = series[m.seriesIndex ?? 0];
          const v = s?.values[m.index];
          if (v == null) return null;
          const cx = model.x(m.index);
          const cy = model.y(v);
          const up = m.kind === "buy";
          const d = up
            ? `M${cx},${cy - 9} l4,7 l-8,0 Z`
            : `M${cx},${cy + 9} l4,-7 l-8,0 Z`;
          return (
            <path
              key={`${m.kind}-${m.index}-${i}`}
              d={d}
              fill={up ? s.color : "none"}
              stroke={s.color}
              strokeWidth="1.5"
              strokeLinejoin="round"
            />
          );
        })}

        {hover !== null && hover >= viewStart && hover <= viewEnd && (
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

        {/* translucent drag-to-zoom selection rectangle */}
        {drag && Math.abs(drag.x1 - drag.x0) > 1 && (
          <rect x={Math.min(drag.x0, drag.x1)} y={padT} width={Math.abs(drag.x1 - drag.x0)} height={H - padT - padB}
            fill="var(--accent)" opacity="0.15" />
        )}

        <text x={padL} y={H - 8} className="chart-label">{labels[viewStart]}</text>
        <text x={W - padR} y={H - 8} textAnchor="end" className="chart-label">{labels[viewEnd]}</text>
      </svg>

      {/* Trade detail for the hovered day. Below the chart, so it can wrap to as
          many lines as the day's trades need and be read in FULL — its growth
          pushes the legend down, never the chart. */}
      {(hasTradeData || holdings) && (
        <div className="chart-trade-detail">
          {hover === null ? (
            <span className="cr-trade-empty">
              {hasTradeData ? "Hover a day to see the trades made that day." : "Hover a day for detail."}
            </span>
          ) : hoverMarkers.length === 0 ? (
            <span className="cr-trade-empty">
              {/* Only claim there were no trades when we were actually given the
                  day's trades to look at. */}
              {labels[hover]}
              {hasTradeData ? ": no trades this day." : ":"}
              {noTradeReasons?.[labels[hover]] ? ` ${noTradeReasons[labels[hover]]}` : ""}
            </span>
          ) : (
            // Buys and sells on their OWN labelled rows. Interleaved behind bare
            // ▲/▼ glyphs, a busy day read as one undifferentiated wall and you
            // couldn't tell trades from the day's contributors below.
            <>
              <div className="td-line">
                <span className="td-day">{labels[hover]}</span>
              </div>
              {(["buy", "sell"] as const).map((kind) => {
                const of = hoverMarkers.filter((m) => m.kind === kind);
                if (!of.length) return null;
                return (
                  <div className="td-line" key={kind}>
                    {/* A plain label, so it stays plain. Green/red mean profit
                        and loss everywhere else in this app, and "Bought 13" is
                        neither — the arrow already carries the direction. */}
                    <span className="td-kind">
                      {kind === "buy" ? `▲ Bought ${of.length}` : `▼ Sold ${of.length}`}
                    </span>
                    {/* Same principle: a BUY has no profit or loss yet, so it
                        gets no colour. Sells are tinted by what they made (or by
                        kind, for callers that don't supply an outcome). */}
                    {of.map((m, i) => (
                      <span key={i} className={`td-item ${m.tone ?? (kind === "buy" ? "" : "down")}`}>
                        {m.text}
                      </span>
                    ))}
                  </div>
                );
              })}
            </>
          )}
          {/* What was HELD that day and what each holding did to the equity —
              this is how a big trade-less rise or fall explains itself. */}
          {hover !== null &&
            (holdings?.[labels[hover]]?.length ?? 0) > 0 &&
            (() => {
              const hs = holdings![labels[hover]];
              const total = hs.reduce((sum, h) => sum + h.day_pnl_pct, 0);
              // Lead with the day's move — that's the thing being explained —
              // then what accounts for it. The old order buried the total at the
              // end of the list it belonged to, behind a bare "3 strategies:"
              // that didn't say what the numbers next to them were.
              return (
                <div className="td-line td-holdings">
                  <span className={`td-kind ${total >= 0 ? "up" : "down"}`}>
                    Day {total >= 0 ? "+" : "−"}
                    {Math.abs(total).toFixed(2)}%
                  </span>
                  <span className="td-from">
                    {holdingsLabel ?? `from ${hs.filter((h) => h.qty > 0).length} open position(s)`}:
                  </span>{" "}
                  {hs.map((h) => (
                    <span key={h.symbol} className={`td-item ${h.day_pnl_pct >= 0 ? "up" : "down"}`}>
                      {h.symbol}
                      {!holdingsLabel && h.qty === 0 ? " (sold)" : ""} {h.day_pnl_pct >= 0 ? "+" : "−"}
                      {Math.abs(h.day_pnl_pct).toFixed(2)}%
                    </span>
                  ))}
                </div>
              );
            })()}
        </div>
      )}

      <div className="legend">
        {series.map((s) => (
          <span key={s.label}>
            <span className="swatch" style={{ background: s.color }} /> {s.label}
          </span>
        ))}
        {marks.length > 0 && (
          <>
            {/* Match the plot: filled up-triangle = bought, hollow down-triangle
                = sold, both in the line's own colour. */}
            <span>
              <span className="marker-key">▲</span> bought
            </span>
            <span>
              <span className="marker-key">▽</span> sold
            </span>
          </>
        )}
      </div>
    </div>
  );
}
