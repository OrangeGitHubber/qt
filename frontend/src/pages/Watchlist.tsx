import { useCallback, useEffect, useState } from "react";
import { addWatchlist, getWatchlist, removeWatchlist, WatchlistRow } from "../api";
import InfoTip from "../components/InfoTip";
import Sparkline from "../components/Sparkline";
import SymbolDetail from "../components/SymbolDetail";
import SymbolPicker from "../components/SymbolPicker";

const pct = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v}%`);

export default function Watchlist() {
  const [rows, setRows] = useState<WatchlistRow[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [assetClass, setAssetClass] = useState<"stock" | "crypto">("stock");
  const [note, setNote] = useState<string | null>(null);
  const [extra, setExtra] = useState(true);
  const [detail, setDetail] = useState<WatchlistRow | null>(null);

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
        <button className="small" onClick={() => setExtra((x) => !x)}>
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
        ) : (
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Type</th>
                <th>Price</th>
                <th>Today</th>
                {extra && (
                  <>
                    <th>
                      30 day <InfoTip k="change_30d" />
                    </th>
                    <th>
                      Daily move <InfoTip k="atr" />
                    </th>
                    <th>
                      vs 200d avg <InfoTip k="sma200" />
                    </th>
                  </>
                )}
                <th>Trend (15m bars)</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
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
        )}
      </div>
    </>
  );
}
