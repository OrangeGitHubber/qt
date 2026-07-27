import { useCallback, useEffect, useMemo, useState } from "react";
import { getJournal, JournalRow } from "../api";

function money(v: number | null) {
  if (v === null || v === undefined) return "—";
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: 4 })}`;
}

function when(iso: string | null) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// One position (a Trade) becomes up to two rows: a Bought and, once it exits, a
// Sold — so buys and sells read as their own entries. Rejected decisions are a
// single row (no buy actually happened).
type Action = "Bought" | "Sold" | "Rejected";
type JEvent = { key: string; at: string; action: Action; price: number | null; pnl: number | null; trade: JournalRow };

export default function Journal() {
  const [rows, setRows] = useState<JournalRow[] | null>(null);
  const [mode, setMode] = useState<string>("");
  const [status, setStatus] = useState<"" | "trades" | "rejected">("");
  const [assetClass, setAssetClass] = useState<"" | "stock" | "crypto">("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getJournal(mode || undefined, status || undefined, assetClass || undefined).then(setRows);
  }, [mode, status, assetClass]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, [refresh]);

  const events = useMemo<JEvent[]>(() => {
    const out: JEvent[] = [];
    for (const r of rows ?? []) {
      if (r.status === "rejected") {
        out.push({ key: `${r.id}-r`, at: r.logged_at ?? r.entry_at ?? "", action: "Rejected", price: r.entry_price, pnl: null, trade: r });
      } else {
        out.push({ key: `${r.id}-b`, at: r.entry_at ?? r.logged_at ?? "", action: "Bought", price: r.entry_price, pnl: null, trade: r });
        if (r.status === "closed" && r.exit_at) {
          out.push({ key: `${r.id}-s`, at: r.exit_at, action: "Sold", price: r.exit_price, pnl: r.pnl, trade: r });
        }
      }
    }
    out.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime());
    return out;
  }, [rows]);

  return (
    <>
      <div className="toolbar">
        <h2>Trade journal</h2>
        <div className="seg" role="group" aria-label="Filter by outcome">
          {(["", "trades", "rejected"] as const).map((s) => (
            <button key={s || "all"} className={status === s ? "active" : ""} onClick={() => setStatus(s)}>
              {s === "" ? "All" : s === "trades" ? "Trades" : "Rejected"}
            </button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Filter by asset class">
          {(["", "stock", "crypto"] as const).map((a) => (
            <button key={a || "all"} className={assetClass === a ? "active" : ""} onClick={() => setAssetClass(a)}>
              {a === "" ? "All" : a === "stock" ? "Stocks" : "Crypto"}
            </button>
          ))}
        </div>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="">All modes</option>
          <option value="shadow">Shadow</option>
          <option value="paper">Paper</option>
        </select>
        <button className="small btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>
      <div className="card">
        {!rows ? (
          <p>Loading…</p>
        ) : events.length === 0 ? (
          <p className="hint">
            Nothing yet. Every decision the engine makes — including trades it wanted to make but a safety rail
            blocked — will appear here with its full reasoning.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th></th>
                <th>Mode</th>
                <th>Strategy</th>
                <th>Symbol</th>
                <th>Action</th>
                <th>Price</th>
                <th>P&L</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => {
                const r = e.trade;
                return (
                  <>
                    <tr key={e.key} className="clickable" onClick={() => setExpanded(expanded === e.key ? null : e.key)}>
                      <td className="hint nowrap">{when(e.at)}</td>
                      <td>{expanded === e.key ? "▾" : "▸"}</td>
                      <td>
                        <span className={`pill ${r.mode === "shadow" ? "muted" : "ok"}`}>{r.mode}</span>
                      </td>
                      <td>{r.strategy}</td>
                      <td className="sym">{r.symbol}</td>
                      <td className={e.action === "Bought" ? "up" : e.action === "Sold" ? "down" : "warn-text"}>
                        {e.action === "Bought" ? "▲ Bought" : e.action === "Sold" ? "▼ Sold" : "⊘ Rejected"}
                      </td>
                      <td>{money(e.price)}</td>
                      <td className={e.pnl == null ? "" : e.pnl >= 0 ? "up" : "down"}>
                        {e.pnl == null ? "—" : `$${e.pnl.toFixed(2)}`}
                      </td>
                      <td>
                        <span className={`pill ${r.status === "open" ? "ok" : r.status === "rejected" ? "warn" : "muted"}`}>
                          {r.status}
                        </span>
                      </td>
                    </tr>
                    {expanded === e.key && (
                      <tr key={`${e.key}-detail`}>
                        <td colSpan={9} className="detail">
                          {e.action === "Sold" ? (
                            <>
                              <p>
                                <strong>Why it sold:</strong> {r.exit_reason || "—"}
                              </p>
                              <p className="hint">
                                Closes the buy of {r.qty} {r.symbol} @ {money(r.entry_price)} entered{" "}
                                {when(r.entry_at)} · P&L {r.pnl == null ? "—" : `$${r.pnl.toFixed(2)}`} · config v
                                {r.config_version_id ?? "?"}
                              </p>
                            </>
                          ) : (
                            <>
                              <p>
                                <strong>Why it {e.action === "Rejected" ? "was rejected" : "bought"}:</strong>{" "}
                                {r.entry_reason || "—"}
                              </p>
                              <p className="hint">
                                {r.qty ? `${r.qty} × ${r.symbol} (${money(r.notional)}) · ` : ""}
                                {r.entry_at ? `entered ${when(r.entry_at)} · ` : ""}config v{r.config_version_id ?? "?"}
                                {e.action === "Bought" && r.status === "closed" ? " · position closed — see the Sold row" : ""}
                              </p>
                            </>
                          )}
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
