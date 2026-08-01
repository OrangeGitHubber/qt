import { Fragment, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { addWatchlist, AssetRow, getWatchlist, removeWatchlist, WatchlistRow } from "../api";
import type { InfoKey } from "../glossary";
import InfoTip from "../components/InfoTip";
import Sparkline from "../components/Sparkline";
import SymbolDetail from "../components/SymbolDetail";
import ColumnPicker, { useColumnPrefs } from "../components/ColumnPicker";
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
    // Overbought (>=70) / oversold (<=30) get BOTH a colour and a text tag, so the
    // signal doesn't depend on colour alone (red/green is ambiguous to ~8% of men).
    render: (r) => {
      const v = r.rsi;
      if (v == null) return <td>—</td>;
      const zone = v >= 70 ? "ob" : v <= 30 ? "os" : "";
      return (
        <td className={zone === "ob" ? "down" : zone === "os" ? "up" : ""}>
          {v}
          {zone && <span className="rsi-tag">{zone === "ob" ? "OB" : "OS"}</span>}
        </td>
      );
    },
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
const ALL_COLS = OPTIONAL_COLUMNS.map((c) => c.key);

export default function Watchlist() {
  const [rows, setRows] = useState<WatchlistRow[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [visibleCols, toggleCol] = useColumnPrefs<ColKey>(COLS_STORAGE_KEY, ALL_COLS);
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
    const active = sortKey === k;
    const arrow = active ? (sortDir === "asc" ? " ▲" : " ▼") : "";
    // aria-sort lets a screen reader announce which column is sorted and which way.
    return (
      <th aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
        <button type="button" className={`th-sort${active ? " active" : ""}`} onClick={() => toggleSort(k)}>
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

  // The picked row carries its OWN asset class, so no class selector is needed —
  // a stock and a crypto can be added from the same search.
  async function addPicked(row: AssetRow) {
    setNote(null);
    try {
      await addWatchlist(row.symbol, row.asset_class);
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
        <ColumnPicker columns={OPTIONAL_COLUMNS} visible={visibleCols} onToggle={toggleCol} />
      </div>
      {detail && (
        <SymbolDetail symbol={detail.symbol} assetClass={detail.asset_class} onClose={() => setDetail(null)} />
      )}
      <div className="card addform">
        <SymbolPicker value={[]} onChange={() => {}} onPick={addPicked} />
        <span className="hint">
          Search any ticker or company name — stocks and crypto together; the icon shows which. Picking adds it straight
          to the list.
        </span>
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
