import { useCallback, useEffect, useState } from "react";
import {
  addWatchlist,
  getScanner,
  getScannerConfig,
  saveScannerConfig,
  ScannerConfig,
  ScannerResult,
  ScannerRow,
} from "../api";
import SymbolPicker from "../components/SymbolPicker";

function volume(v: number) {
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  return `$${Math.round(v / 1e3)}k`;
}

function MoversTable({ title, rows, onPin }: { title: string; rows: ScannerRow[]; onPin: (r: ScannerRow) => void }) {
  return (
    <div className="card">
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="hint">Nothing passes the filters right now.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Price</th>
              <th>Today</th>
              <th>$ Volume</th>
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

  function num(key: keyof ScannerConfig) {
    return {
      value: (cfg?.[key] as number) ?? 0,
      onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
        setCfg((c) => (c ? { ...c, [key]: Number(e.target.value) } : c)),
    };
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
      {note && (
        <div className="card note" onClick={() => setNote(null)}>
          {note}
        </div>
      )}
      {showFilters && cfg && (
        <form className="card filters" onSubmit={submitFilters}>
          <div className="filter-grid">
            <label>
              <input
                type="checkbox"
                checked={cfg.stocks_enabled}
                onChange={(e) => setCfg({ ...cfg, stocks_enabled: e.target.checked })}
              />{" "}
              Scan stocks
            </label>
            <label>
              <input
                type="checkbox"
                checked={cfg.crypto_enabled}
                onChange={(e) => setCfg({ ...cfg, crypto_enabled: e.target.checked })}
              />{" "}
              Scan crypto
            </label>
            <label>
              Rows per list
              <input type="number" min={1} max={50} {...num("top_n")} />
            </label>
            <label>
              Min price ($)
              <input type="number" min={0} step="0.01" {...num("min_price")} />
            </label>
            <label>
              Max price ($, 0 = none)
              <input type="number" min={0} step="0.01" {...num("max_price")} />
            </label>
            <label>
              Min gain today (%)
              <input type="number" min={0} step="0.1" {...num("min_change_pct")} />
            </label>
            <label>
              Min daily $ volume
              <input type="number" min={0} step="100000" {...num("min_dollar_volume")} />
            </label>
            <div className="field">
              Never trade these
              <SymbolPicker
                value={cfg.exclude_symbols}
                onChange={(symbols) => setCfg({ ...cfg, exclude_symbols: symbols })}
                multi
                placeholder="Search a symbol to exclude"
              />
            </div>
          </div>
          <button>Save filters</button>
          <p className="hint">
            The $ volume floor keeps you out of illiquid symbols that are hard to exit; the min price filters
            penny-stock pumps. These same filters will feed the trading engine in Phase 2.
          </p>
        </form>
      )}
      {result?.errors.map((e) => (
        <div className="card error" key={e}>
          {e}
        </div>
      ))}
      <div className="grid">
        {result && cfg?.stocks_enabled !== false && <MoversTable title="Stocks" rows={result.stocks} onPin={pin} />}
        {result && cfg?.crypto_enabled !== false && <MoversTable title="Crypto" rows={result.crypto} onPin={pin} />}
      </div>
      {!result && <div className="card">Scanning…</div>}
    </>
  );
}
