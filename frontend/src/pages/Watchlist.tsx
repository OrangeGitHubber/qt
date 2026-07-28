import { useCallback, useEffect, useMemo, useState } from "react";
import { addWatchlist, getWatchlist, removeWatchlist, WatchlistRow } from "../api";
import InfoTip from "../components/InfoTip";
import Sparkline from "../components/Sparkline";
import SymbolDetail from "../components/SymbolDetail";
import SymbolPicker from "../components/SymbolPicker";

const pct = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v}%`);

type SortKey = "symbol" | "asset_class" | "price" | "change_pct" | "change_30d_pct" | "atr_pct" | "vs_sma200_pct";
const STRING_KEYS: SortKey[] = ["symbol", "asset_class"];

export default function Watchlist() {
  const [rows, setRows] = useState<WatchlistRow[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [assetClass, setAssetClass] = useState<"stock" | "crypto">("stock");
  const [note, setNote] = useState<string | null>(null);
  const [extra, setExtra] = useState(true);
  const [detail, setDetail] = useState<WatchlistRow | null>(null);
  const [filter, setFilter] = useState<"all" | "stock" | "crypto">("all");
  const [sortKey, setSortKey] = useState<SortKey>("symbol");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  function toggleSort(k: SortKey) {
    if (k === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(k);
      setSortDir(STRING_KEYS.includes(k) ? "asc" : "desc"); // numbers default high→low
    }
  }

  const view = useMemo(() => {
    const filtered = (rows ?? []).filter((r) => filter === "all" || r.asset_class === filter);
    return [...filtered].sort((a, b) => {
      if (STRING_KEYS.includes(sortKey)) {
        const cmp = String(a[sortKey]).localeCompare(String(b[sortKey]));
        return sortDir === "asc" ? cmp : -cmp;
      }
      const an = a[sortKey] as number | null;
      const bn = b[sortKey] as number | null;
      if (an == null && bn == null) return 0;
      if (an == null) return 1; // nulls ("—") always sink to the bottom
      if (bn == null) return -1;
      return sortDir === "asc" ? an - bn : bn - an;
    });
  }, [rows, filter, sortKey, sortDir]);

  function SortHead({ k, label, tip }: { k: SortKey; label: string; tip?: string }) {
    const arrow = sortKey === k ? (sortDir === "asc" ? " ▲" : " ▼") : "";
    return (
      <th>
        <button type="button" className={`th-sort${sortKey === k ? " active" : ""}`} onClick={() => toggleSort(k)}>
          {label}
          {arrow}
        </button>
        {tip && <InfoTip k={tip} />}
      </th>
    );
  }

  const refresh = useCallback(() => {
    getWatchlist()
      .then((d) => {
        setRows(d.items);
        setErrors(d.errors);
      })
      .catch((e: Error) => setNote(e.message));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 45_000);
    return () => clearInterval(t);
  }, [refresh]);

  async function addPicked(symbols: string[]) {
    const chosen = symbols[0];
    if (!chosen) return;
    setNote(null);
    try {
      await addWatchlist(chosen, assetClass);
      refresh();
    } catch (err) {
      setNote((err as Error).message);
    }
  }

  async function remove(row: WatchlistRow) {
    await removeWatchlist(row.symbol, row.asset_class);
    refresh();
  }

  return (
    <>
      <div className="toolbar">
        <h2>Watchlist</h2>
        <div className="seg" role="group" aria-label="Filter by asset class">
          {(["all", "stock", "crypto"] as const).map((f) => (
            <button
              key={f}
              className={filter === f ? "active" : ""}
              onClick={() => setFilter(f)}
            >
              {f === "all" ? "All" : f === "stock" ? "Stocks" : "Crypto"}
            </button>
          ))}
        </div>
        <button className="small btn-ghost" onClick={() => setExtra((x) => !x)}>
          {extra ? "Hide extra columns" : "Show 30d / volatility / trend"}
        </button>
      </div>
      {detail && (
        <SymbolDetail symbol={detail.symbol} assetClass={detail.asset_class} onClose={() => setDetail(null)} />
      )}
      <div className="card addform">
        <select value={assetClass} onChange={(e) => setAssetClass(e.target.value as "stock" | "crypto")}>
          <option value="stock">Stock</option>
          <option value="crypto">Crypto</option>
        </select>
        <SymbolPicker assetClass={assetClass} value={[]} onChange={addPicked} />
        <span className="hint">Search by ticker or company name — picking adds it straight to the list.</span>
        {note && <span className="error">{note}</span>}
      </div>
      {errors.map((e) => (
        <div className="card error" key={e}>
          {e}
        </div>
      ))}
      <div className="card">
        {!rows ? (
          <p>Loading…</p>
        ) : rows.length === 0 ? (
          <p className="hint">
            Nothing pinned yet. Add symbols here, or hit "+ Watch" on the Scanner tab. Pinned symbols will always be
            considered by the trading engine (Phase 2), even when they don't show up in the scanner.
          </p>
        ) : view.length === 0 ? (
          <p className="hint">No {filter === "stock" ? "stocks" : "crypto"} in your watchlist.</p>
        ) : (
          <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHead k="symbol" label="Symbol" />
                <SortHead k="asset_class" label="Type" />
                <SortHead k="price" label="Price" />
                <SortHead k="change_pct" label="Today" />
                {extra && (
                  <>
                    <SortHead k="change_30d_pct" label="30 day" tip="change_30d" />
                    <SortHead k="atr_pct" label="Daily move" tip="atr" />
                    <SortHead k="vs_sma200_pct" label="vs 200d avg" tip="sma200" />
                  </>
                )}
                <th title="Last ~30 daily closes">Trend (30d)</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {view.map((r) => (
                <tr key={`${r.asset_class}:${r.symbol}`}>
                  <td>
                    <button className="linklike sym" onClick={() => setDetail(r)} title={`Price history for ${r.symbol}`}>
                      {r.symbol}
                    </button>
                  </td>
                  <td>{r.asset_class}</td>
                  <td>{r.price != null ? `$${r.price.toLocaleString(undefined, { maximumFractionDigits: 4 })}` : "—"}</td>
                  <td className={(r.change_pct ?? 0) >= 0 ? "up" : "down"}>
                    {r.change_pct != null ? `${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%` : "—"}
                  </td>
                  {extra && (
                    <>
                      <td className={(r.change_30d_pct ?? 0) >= 0 ? "up" : "down"}>{pct(r.change_30d_pct)}</td>
                      <td>{r.atr_pct != null ? `${r.atr_pct}%` : "—"}</td>
                      <td className={(r.vs_sma200_pct ?? 0) >= 0 ? "up" : "down"}>{pct(r.vs_sma200_pct)}</td>
                    </>
                  )}
                  <td>
                    <Sparkline symbol={r.symbol} assetClass={r.asset_class} />
                  </td>
                  <td>
                    <button className="small danger" onClick={() => remove(r)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </>
  );
}
