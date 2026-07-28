import { useEffect, useMemo, useState } from "react";
import { getHistory, getJournal, HistoryResponse, JournalRow } from "../api";
import { atrStopLevel, bollinger, ema, macd, relativeStrength, rsi, sma } from "../lib/indicators";
import InfoTip from "./InfoTip";
import PriceChart, { ChartMarker, ChartOverlays, OverlayLine, SERIES_COLORS } from "./PriceChart";

const RANGES: { label: string; days: number | null }[] = [
  { label: "1M", days: 30 },
  { label: "6M", days: 182 },
  { label: "1Y", days: 365 },
  { label: "5Y", days: 1825 },
  { label: "Max", days: null },
];

// The overlays the user can toggle. Each has a ? tip explaining what it measures
// and how to read it. `stockOnly` ones are hidden for crypto.
type Tip = Parameters<typeof InfoTip>[0]["k"];
const OVERLAYS: { key: OverlayKey; label: string; tip: Tip; stockOnly?: boolean }[] = [
  { key: "markers", label: "Buy / sell markers", tip: "chart_markers" },
  { key: "ma", label: "50 & 200-day MA", tip: "ma_overlay" },
  { key: "ema", label: "EMA 9 & 21", tip: "ema_overlay" },
  { key: "bb", label: "Bollinger Bands", tip: "bollinger_overlay" },
  { key: "atrStop", label: "ATR-stop level", tip: "atr_stop_line" },
  { key: "volume", label: "Volume", tip: "volume_overlay" },
  { key: "macd", label: "MACD", tip: "macd" },
  { key: "rsi", label: "RSI", tip: "rsi" },
  { key: "rs", label: "Rel. strength vs SPY", tip: "rs_ratio", stockOnly: true },
];

type OverlayKey = "markers" | "ma" | "ema" | "bb" | "atrStop" | "volume" | "macd" | "rsi" | "rs";

function Stat({ label, value, tip }: { label: string; value: string; tip?: Parameters<typeof InfoTip>[0]["k"] }) {
  return (
    <div className="stat">
      <div className="stat-label">
        {label} {tip && <InfoTip k={tip} />}
      </div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

/** Nearest bar index to an ISO timestamp (daily bars → snap a trade to its day). */
function nearestIndex(times: number[], isoMs: number): number {
  let best = 0;
  let bestDiff = Infinity;
  for (let i = 0; i < times.length; i++) {
    const d = Math.abs(times[i] - isoMs);
    if (d < bestDiff) {
      bestDiff = d;
      best = i;
    }
  }
  return best;
}

export default function SymbolDetail({
  symbol,
  assetClass,
  onClose,
}: {
  symbol: string;
  assetClass: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState<number | null>(null); // null = max
  const [spy, setSpy] = useState<HistoryResponse | null>(null);
  const [journal, setJournal] = useState<JournalRow[]>([]);
  const [on, setOn] = useState<Record<OverlayKey, boolean>>({
    markers: true, ma: false, ema: false, bb: false, atrStop: false, volume: false, macd: false, rsi: false, rs: false,
  });

  useEffect(() => {
    setData(null);
    setError(null);
    getHistory(symbol, assetClass)
      .then(setData)
      .catch((e: Error) => setError(e.message));
    // Journal rows for this symbol power the buy/sell markers.
    getJournal(undefined, undefined, assetClass)
      .then((rows) => setJournal(rows.filter((r) => r.symbol === symbol)))
      .catch(() => setJournal([]));
  }, [symbol, assetClass]);

  // SPY history for the relative-strength overlay — stocks only, fetched lazily
  // the first time the RS overlay is switched on.
  useEffect(() => {
    if (assetClass === "stock" && on.rs && !spy) {
      getHistory("SPY", "stock").then(setSpy).catch(() => setSpy(null));
    }
  }, [on.rs, assetClass, spy]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const all = useMemo(() => data?.bars ?? [], [data]);
  const startIdx = range === null ? 0 : Math.max(0, all.length - Math.round(range * 0.7));
  const points = useMemo(() => all.slice(startIdx), [all, startIdx]);
  const pct = (v: number | null | undefined) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v}%`);

  // Compute every enabled indicator on the FULL history, then slice to the
  // visible window — so a 50/200-day average still shows on a 1-month view.
  const overlays: ChartOverlays = useMemo(() => {
    if (all.length < 2) return {};
    const closes = all.map((b) => b.c);
    const slice = <T,>(arr: T[]) => arr.slice(startIdx);
    const o: ChartOverlays = {};

    const priceLines: OverlayLine[] = [];
    if (on.ma) {
      priceLines.push({ key: "ma50", label: "MA50", color: SERIES_COLORS.ma50, values: slice(sma(closes, 50)) });
      priceLines.push({ key: "ma200", label: "MA200", color: SERIES_COLORS.ma200, values: slice(sma(closes, 200)) });
    }
    if (on.ema) {
      priceLines.push({ key: "ema9", label: "EMA9", color: SERIES_COLORS.ema9, values: slice(ema(closes, 9)) });
      priceLines.push({ key: "ema21", label: "EMA21", color: SERIES_COLORS.ema21, values: slice(ema(closes, 21)) });
    }
    if (on.atrStop) {
      priceLines.push({
        key: "atrStop", label: "ATR stop (2×)", color: SERIES_COLORS.atrStop, dash: true,
        values: slice(atrStopLevel(all, 14, 2)),
      });
    }
    if (priceLines.length) o.priceLines = priceLines;

    if (on.bb) {
      const bb = bollinger(closes, 20, 2);
      o.bollinger = { upper: slice(bb.upper), lower: slice(bb.lower), mid: slice(bb.mid) };
    }
    if (on.volume) o.volume = true;
    if (on.macd) {
      const m = macd(closes, 12, 26, 9);
      o.macd = { macd: slice(m.macd), signal: slice(m.signal), hist: slice(m.hist) };
    }
    if (on.rsi) o.rsi = slice(rsi(closes, 14));

    if (on.rs && assetClass === "stock" && spy) {
      // Align SPY closes to this symbol's dates (daily), then rebase both to the
      // window start. Missing SPY days → null (skipped in the line).
      const spyByDay = new Map(spy.bars.map((b) => [b.t.slice(0, 10), b.c]));
      const winCloses = points.map((b) => b.c);
      const benchAligned = points.map((b) => spyByDay.get(b.t.slice(0, 10)) ?? null);
      o.rs = { values: relativeStrength(winCloses, benchAligned), label: "vs SPY" };
    }

    if (on.markers) {
      const times = points.map((b) => new Date(b.t).getTime());
      const markers: ChartMarker[] = [];
      for (const r of journal) {
        if (r.entry_at && r.entry_price != null) {
          const t = new Date(r.entry_at).getTime();
          if (t >= times[0] - 2 * 864e5) {
            markers.push({
              index: nearestIndex(times, t), kind: "buy", price: r.entry_price,
              label: `BUY ${r.qty} @ $${r.entry_price} · ${r.entry_reason || r.strategy} (${r.mode})`,
            });
          }
        }
        if (r.exit_at && r.exit_price != null) {
          const t = new Date(r.exit_at).getTime();
          if (t >= times[0] - 2 * 864e5) {
            const pnlTxt = r.pnl != null ? ` · P&L ${r.pnl >= 0 ? "+" : ""}$${r.pnl.toFixed(2)}` : "";
            markers.push({
              index: nearestIndex(times, t), kind: "sell", price: r.exit_price,
              label: `SELL ${r.qty} @ $${r.exit_price} · ${r.exit_reason || "exit"}${pnlTxt} (${r.mode})`,
            });
          }
        }
      }
      if (markers.length) o.markers = markers;
    }
    return o;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all, points, startIdx, on, journal, spy, assetClass]);

  const toggle = (k: OverlayKey) => setOn((s) => ({ ...s, [k]: !s[k] }));
  const visibleOverlays = OVERLAYS.filter((o) => !(o.stockOnly && assetClass !== "stock"));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="toolbar">
          <h2>
            {symbol} <span className="subtitle">{assetClass}</span>
          </h2>
          <button className="small btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>

        {error && <div className="card error">{error}</div>}
        {!data && !error && <p>Loading price history…</p>}

        {data && (
          <>
            <div className="stats">
              <Stat label="Last" value={all.length ? `$${all[all.length - 1].c.toLocaleString()}` : "—"} />
              <Stat label="30 days" value={pct(data.stats.change_30d_pct)} tip="change_30d" />
              <Stat label="Typical daily move" value={pct(data.stats.atr_pct)} tip="atr" />
              <Stat label="vs 200-day avg" value={pct(data.stats.vs_sma200_pct)} tip="sma200" />
            </div>

            <div className="range-buttons">
              {RANGES.map((r) => (
                <button
                  key={r.label}
                  className={`small ${range === r.days ? "mode-active" : ""}`}
                  onClick={() => setRange(r.days)}
                  disabled={r.days !== null && all.length < 5}
                >
                  {r.label}
                </button>
              ))}
              <span className="hint">{all.length.toLocaleString()} trading days available</span>
            </div>

            {/* Overlay toggles — each adds/removes its data on the chart below. */}
            <div className="overlay-toggles">
              {visibleOverlays.map((o) => (
                <span key={o.key} className="chip-with-tip">
                  <label className={`chip-toggle ${on[o.key] ? "chip-on" : ""}`}>
                    <input type="checkbox" checked={on[o.key]} onChange={() => toggle(o.key)} />
                    {o.label}
                  </label>
                  <InfoTip k={o.tip} />
                </span>
              ))}
            </div>
            <p className="hint">
              Overlays are <strong>display-only</strong> — they annotate history so you can eyeball what the signals were
              doing; they never change a trade. The ATR-stop line is illustrative (close − 2×ATR), not a specific
              position's live stop. Markers come from the trade journal for this symbol.
            </p>

            <PriceChart points={points} overlays={overlays} />
          </>
        )}
      </div>
    </div>
  );
}
