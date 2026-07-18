import { useCallback, useEffect, useState } from "react";
import {
  addWatchlist,
  getScanner,
  getScannerConfig,
  saveScannerConfig,
  ScannerClassFilters,
  ScannerConfig,
  ScannerMeta,
  ScannerResult,
  ScannerRow,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";
import SymbolPicker from "../components/SymbolPicker";

function volume(v: number) {
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  return `$${Math.round(v / 1e3)}k`;
}

// Name the FIRST floor the top mover tripped, in the same order the backend
// filters — so the message points at the real blocker, not just "min gain".
function blockedReason(meta: ScannerMeta, f: ScannerClassFilters): string {
  const price = meta.best_price ?? 0;
  const vol = meta.best_dollar_volume ?? 0;
  if (f.min_price && price < f.min_price) return `below your $${f.min_price} min price`;
  if (f.max_price && price > f.max_price) return `above your $${f.max_price} max price`;
  if ((meta.best_change_pct ?? 0) < f.min_change_pct) return `below your ${f.min_change_pct}% min gain`;
  if (vol < f.min_dollar_volume) return `below your ${volume(f.min_dollar_volume)} min volume`;
  return "on your exclude list";
}

function EmptyReason({ meta, filters }: { meta: ScannerMeta | null; filters: ScannerClassFilters }) {
  if (meta && meta.scanned > 0 && meta.best_symbol) {
    return (
      <p className="hint">
        Scanned {meta.scanned} symbol{meta.scanned === 1 ? "" : "s"} — the strongest was{" "}
        <strong>{meta.best_symbol}</strong> at {(meta.best_change_pct ?? 0) >= 0 ? "+" : ""}
        {meta.best_change_pct}% (${meta.best_price?.toLocaleString(undefined, { maximumFractionDigits: 4 })},{" "}
        {volume(meta.best_dollar_volume ?? 0)} vol), but it's <strong>{blockedReason(meta, filters)}</strong>. No
        symbol currently clears every filter at once — not an error.
      </p>
    );
  }
  return <p className="hint">No symbols returned data to scan right now.</p>;
}

function MoversTable({
  title,
  rows,
  meta,
  filters,
  marketClosed,
  onPin,
}: {
  title: string;
  rows: ScannerRow[];
  meta: ScannerMeta | null;
  filters: ScannerClassFilters;
  marketClosed?: boolean;
  onPin: (r: ScannerRow) => void;
}) {
  return (
    <div className="card">
      <h3>{title}</h3>
      {marketClosed && (
        <p className="stale-note">
          ⏸ Market closed — these are the <strong>last trading session's</strong> movers, not live prices.
        </p>
      )}
      {rows.length === 0 ? (
        <EmptyReason meta={meta} filters={filters} />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Price</th>
              <th>Today</th>
              <th>
                $ Vol (feed) <InfoTip k="dollar_volume" />
              </th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol}>
                <td className="sym">{r.symbol}</td>
                <td>${r.price.toLocaleString(undefined, { maximumFractionDigits: 4 })}</td>
                <td className={r.change_pct >= 0 ? "up" : "down"}>
                  {r.change_pct >= 0 ? "+" : ""}
                  {r.change_pct}%
                </td>
                <td>{volume(r.dollar_volume)}</td>
                <td>
                  <button className="small" title="Pin to watchlist" onClick={() => onPin(r)}>
                    + Watch
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function Scanner() {
  const [result, setResult] = useState<ScannerResult | null>(null);
  const [cfg, setCfg] = useState<ScannerConfig | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getScanner().then(setResult).catch((e: Error) => setNote(e.message));
  }, []);

  useEffect(() => {
    refresh();
    getScannerConfig().then(setCfg);
    const t = setInterval(refresh, 45_000);
    return () => clearInterval(t);
  }, [refresh]);

  async function pin(row: ScannerRow) {
    try {
      await addWatchlist(row.symbol, row.asset_class);
      setNote(`${row.symbol} added to watchlist.`);
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function submitFilters(e: React.FormEvent) {
    e.preventDefault();
    if (!cfg) return;
    try {
      const saved = await saveScannerConfig(cfg);
      setCfg(saved);
      setNote("Filters saved.");
      refresh();
    } catch (err) {
      setNote((err as Error).message);
    }
  }

  function classBlock(cls: "stocks" | "crypto", title: string, hint: string) {
    if (!cfg) return null;
    const f = cfg[cls];
    const setF = (key: keyof ScannerConfig["stocks"], val: number | boolean) =>
      setCfg((c) => (c ? { ...c, [cls]: { ...c[cls], [key]: val } } : c));
    return (
      <fieldset className="scanner-class">
        <legend>
          <label className="check">
            <input type="checkbox" checked={f.enabled} onChange={(e) => setF("enabled", e.target.checked)} /> {title}
          </label>
        </legend>
        <div className="filter-grid">
          <label>
            Min price ($)
            <NumberField min={0} step="any" value={f.min_price} onChange={(n) => setF("min_price", n)} />
          </label>
          <label>
            Max price ($, 0 = none)
            <NumberField min={0} step="any" value={f.max_price} onChange={(n) => setF("max_price", n)} />
          </label>
          <label>
            Min gain today (%)
            <NumberField min={0} step="0.1" value={f.min_change_pct} onChange={(n) => setF("min_change_pct", n)} />
          </label>
          <label>
            Min daily $ volume
            <NumberField min={0} step="any" value={f.min_dollar_volume} onChange={(n) => setF("min_dollar_volume", n)} />
          </label>
        </div>
        <p className="hint">{hint}</p>
      </fieldset>
    );
  }

  return (
    <>
      <div className="toolbar">
        <h2>Today's risers</h2>
        <button className="small" onClick={() => setShowFilters((s) => !s)}>
          {showFilters ? "Hide filters" : "Edit filters"}
        </button>
        <button className="small" onClick={refresh}>
          Refresh
        </button>
      </div>
      <p className="page-intro">
        The scanner is where trade ideas start. It continuously ranks today's <strong>rising</strong> stocks and
        crypto and keeps only the ones that clear your filters — a liquidity-and-quality gate that keeps illiquid,
        hard-to-exit pumps out. Any strategy whose universe is set to the scanner draws its candidates from this
        list; the strategy's own entry rules and the safety rails then decide what actually trades. Think of it as a
        live shortlist of what's moving — <strong>not</strong> a buy list.
      </p>
      {note && (
        <div className="card note" onClick={() => setNote(null)}>
          {note}
        </div>
      )}
      {showFilters && cfg && (
        <form className="card filters" onSubmit={submitFilters}>
          <div className="filter-grid">
            <label>
              Rows per list
              <NumberField
                min={1}
                max={50}
                step={1}
                value={cfg.top_n}
                onChange={(n) => setCfg((c) => (c ? { ...c, top_n: n } : c))}
              />
            </label>
            <div className="field">
              Never trade these (both markets)
              <SymbolPicker
                value={cfg.exclude_symbols}
                onChange={(symbols) => setCfg({ ...cfg, exclude_symbols: symbols })}
                multi
                placeholder="Search a symbol to exclude"
              />
            </div>
          </div>
          <div className="scanner-classes">
            {classBlock(
              "stocks",
              "Scan stocks",
              "The $5M volume floor and $1 price floor keep you out of illiquid penny-stock pumps that are hard to exit.",
            )}
            {classBlock(
              "crypto",
              "Scan crypto",
              "Crypto trades 24/7 and resets volume at 00:00 UTC, so lower floors are normal — and there's no $1 price floor (coins like DOGE trade well under $1).",
            )}
          </div>
          <button>Save filters</button>
          <p className="hint">
            Stocks and crypto have <strong>separate</strong> filters because they behave differently. These same
            filters feed the trading engine when a strategy's universe is the scanner.
          </p>
        </form>
      )}
      {result?.errors.map((e) => (
        <div className="card error" key={e}>
          {e}
        </div>
      ))}
      <div className="grid">
        {result && cfg && cfg.stocks.enabled !== false && (
          <MoversTable
            title="Stocks"
            rows={result.stocks}
            meta={result.stocks_meta}
            filters={cfg.stocks}
            marketClosed={result.market_open === false}
            onPin={pin}
          />
        )}
        {result && cfg && cfg.crypto.enabled !== false && (
          <MoversTable
            title="Crypto"
            rows={result.crypto}
            meta={result.crypto_meta}
            filters={cfg.crypto}
            onPin={pin}
          />
        )}
      </div>
      {!result && <div className="card">Scanning…</div>}
    </>
  );
}
