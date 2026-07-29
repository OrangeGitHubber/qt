import { Fragment, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { addWatchlist, getWatchlist, removeWatchlist, WatchlistRow } from "../api";
import type { InfoKey } from "../glossary";
import InfoTip from "../components/InfoTip";
import Sparkline from "../components/Sparkline";
import SymbolDetail from "../components/SymbolDetail";
import SymbolPicker from "../components/SymbolPicker";

const pct = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v}%`);
const upDown = (v: number | null) => ((v ?? 0) >= 0 ? "up" : "down");

type SortKey = "symbol" | "asset_class" | "price" | "change_pct" | "change_30d_pct" | "atr_pct" | "vs_sma200_pct" | "rsi";
const STRING_KEYS: SortKey[] = ["symbol", "asset_class"];

// The optional columns the user can show/hide (the always-on ones — symbol,
// type, price, today — aren't configurable). Each carries how to sort it (if
// numeric), its glossary tip, and how to render one cell. `key` is the stable
// id persisted in localStorage.
type ColKey = "change_30d_pct" | "atr_pct" | "vs_sma200_pct" | "rsi" | "trend";
interface OptCol {
  key: ColKey;
  label: string;
  tip?: InfoKey;
  sortKey?: SortKey; // omitted for non-sortable columns (the sparkline)
  render: (r: WatchlistRow) => ReactNode;
}
const OPTIONAL_COLUMNS: OptCol[] = [
  {
    key: "change_30d_pct",
    label: "30 day",
    tip: "change_30d",
    sortKey: "change_30d_pct",
    render: (r) => <td className={upDown(r.change_30d_pct)}>{pct(r.change_30d_pct)}</td>,
  },
  {
    key: "atr_pct",
    label: "Daily move",
    tip: "atr",
    sortKey: "atr_pct",
    render: (r) => <td>{r.atr_pct != null ? `${r.atr_pct}%` : "—"}</td>,
  },
  {
    key: "vs_sma200_pct",
    label: "vs 200d avg",
    tip: "sma200",
    sortKey: "vs_sma200_pct",
    render: (r) => <td className={upDown(r.vs_sma200_pct)}>{pct(r.vs_sma200_pct)}</td>,
  },
  {
    key: "rsi",
    label: "RSI",
    tip: "rsi",
    sortKey: "rsi",
    // Overbought (>70) / oversold (<30) get a subtle cue; otherwise neutral.
    render: (r) => (
      <td className={r.rsi == null ? "" : r.rsi >= 70 ? "down" : r.rsi <= 30 ? "up" : ""}>
        {r.rsi != null ? r.rsi : "—"}
      </td>
    ),
  },
  {
    key: "trend",
    label: "Trend (30d)",
    render: (r) => (
      <td>
        <Sparkline symbol={r.symbol} assetClass={r.asset_class} />
      </td>
    ),
  },
];
const COLS_STORAGE_KEY = "qt.watchlist.columns";
const DEFAULT_COLS: ColKey[] = OPTIONAL_COLUMNS.map((c) => c.key); // all shown by default

function loadCols(): Set<ColKey> {
  try {
    const raw = localStorage.getItem(COLS_STORAGE_KEY);
    if (raw) {
      const keys = (JSON.parse(raw) as string[]).filter((k) =>
        OPTIONAL_COLUMNS.some((c) => c.key === k),
      ) as ColKey[];
      return new Set(keys);
    }
  } catch {
    /* corrupt/unavailable storage: fall through to defaults */
  }
  return new Set(DEFAULT_COLS);
}

export default function Watchlist() {
  const [rows, setRows] = useState<WatchlistRow[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [assetClass, setAssetClass] = useState<"stock" | "crypto">("stock");
  const [note, setNote] = useState<string | null>(null);
  const [visibleCols, setVisibleCols] = useState<Set<ColKey>>(loadCols);
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

  // Persist the column choice so it sticks across reloads (per browser).
  useEffect(() => {
    try {
      localStorage.setItem(COLS_STORAGE_KEY, JSON.stringify([...visibleCols]));
    } catch {
      /* storage unavailable (private mode, etc.) — the session state still works */
    }
  }, [visibleCols]);

  function toggleCol(k: ColKey) {
    setVisibleCols((cur) => {
      const next = new Set(cur);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }

  const shownCols = OPTIONAL_COLUMNS.filter((c) => visibleCols.has(c.key));

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
        <details className="cols-menu">
          <summary className="small btn-ghost" title="Choose which columns to show">
            Columns
          </summary>
          <div className="cols-popover" role="group" aria-label="Choose columns">
            {OPTIONAL_COLUMNS.map((c) => (
              <label key={c.key} className="check">
                <input
                  type="checkbox"
                  checked={visibleCols.has(c.key)}
                  onChange={() => toggleCol(c.key)}
                />
                {c.label}
              </label>
            ))}
          </div>
        </details>
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
                {shownCols.map((c) =>
                  c.sortKey ? (
                    <SortHead key={c.key} k={c.sortKey} label={c.label} tip={c.tip} />
                  ) : (
                    <th key={c.key} title="Last ~30 daily closes">
                      {c.label}
                    </th>
                  ),
                )}
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
                  {shownCols.map((c) => (
                    <Fragment key={c.key}>{c.render(r)}</Fragment>
                  ))}
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
