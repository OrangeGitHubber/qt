import { FormEvent, useCallback, useEffect, useState } from "react";
import { addWatchlist, getWatchlist, removeWatchlist, WatchlistRow } from "../api";
import Sparkline from "../components/Sparkline";

export default function Watchlist() {
  const [rows, setRows] = useState<WatchlistRow[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [symbol, setSymbol] = useState("");
  const [assetClass, setAssetClass] = useState<"stock" | "crypto">("stock");
  const [note, setNote] = useState<string | null>(null);

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

  async function add(e: FormEvent) {
    e.preventDefault();
    setNote(null);
    try {
      await addWatchlist(symbol.trim(), assetClass);
      setSymbol("");
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
      </div>
      <form className="card addform" onSubmit={add}>
        <input
          placeholder={assetClass === "crypto" ? "e.g. BTC/USD" : "e.g. NVDA"}
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          required
        />
        <select value={assetClass} onChange={(e) => setAssetClass(e.target.value as "stock" | "crypto")}>
          <option value="stock">Stock</option>
          <option value="crypto">Crypto</option>
        </select>
        <button>Add</button>
        {note && <span className="error">{note}</span>}
      </form>
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
                <th>Trend (15m bars)</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.asset_class}:${r.symbol}`}>
                  <td className="sym">{r.symbol}</td>
                  <td>{r.asset_class}</td>
                  <td>{r.price != null ? `$${r.price.toLocaleString(undefined, { maximumFractionDigits: 4 })}` : "—"}</td>
                  <td className={(r.change_pct ?? 0) >= 0 ? "up" : "down"}>
                    {r.change_pct != null ? `${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%` : "—"}
                  </td>
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
