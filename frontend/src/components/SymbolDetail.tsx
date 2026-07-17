import { useEffect, useState } from "react";
import { getHistory, HistoryResponse } from "../api";
import InfoTip from "./InfoTip";
import PriceChart from "./PriceChart";

const RANGES: { label: string; days: number | null }[] = [
  { label: "1M", days: 30 },
  { label: "6M", days: 182 },
  { label: "1Y", days: 365 },
  { label: "5Y", days: 1825 },
  { label: "Max", days: null },
];

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

  useEffect(() => {
    setData(null);
    setError(null);
    getHistory(symbol, assetClass)
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, [symbol, assetClass]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const all = data?.bars ?? [];
  const points = range === null ? all : all.slice(Math.max(0, all.length - Math.round(range * 0.7)));
  const pct = (v: number | null | undefined) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v}%`);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="toolbar">
          <h2>
            {symbol} <span className="subtitle">{assetClass}</span>
          </h2>
          <button className="small" onClick={onClose}>
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
              <span className="hint">
                {all.length.toLocaleString()} trading days available · hover the line for price and date
              </span>
            </div>

            <PriceChart points={points} />
          </>
        )}
      </div>
    </div>
  );
}
