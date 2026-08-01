import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { getJournal, JournalRow } from "../api";
import ColumnPicker, { useColumnPrefs } from "../components/ColumnPicker";
import AccountSelect from "../components/AccountSelect";

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

// Optional journal columns. "Cost" is what went IN (qty x entry) and "Proceeds"
// what came back OUT (qty x exit) — both derived from data the journal already
// carries, so no extra fetch. A sell row's P&L is the difference between them;
// showing the two sides makes the size of the bet visible, not just its outcome.
type JCol = "cost" | "proceeds" | "qty" | "mode" | "strategy" | "status";
const JOURNAL_COLUMNS: { key: JCol; label: string }[] = [
  { key: "mode", label: "Mode" },
  { key: "strategy", label: "Strategy" },
  { key: "qty", label: "Qty" },
  { key: "cost", label: "Cost (what went in)" },
  { key: "proceeds", label: "Proceeds (what came back)" },
  { key: "status", label: "Status" },
];
const JOURNAL_COLS_KEY = "qt.journal.columns";
const JOURNAL_DEFAULT_COLS: JCol[] = ["mode", "strategy", "cost", "status"];

export default function Journal() {
  const [rows, setRows] = useState<JournalRow[] | null>(null);
  const [mode, setMode] = useState<string>("");
  const [status, setStatus] = useState<"" | "trades" | "rejected">("");
  const [assetClass, setAssetClass] = useState<"" | "stock" | "crypto">("");
  const [account, setAccount] = useState<string>("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [cols, toggleCol] = useColumnPrefs<JCol>(
    JOURNAL_COLS_KEY,
    JOURNAL_COLUMNS.map((c) => c.key),
    JOURNAL_DEFAULT_COLS,
  );

  const refresh = useCallback(() => {
    getJournal(mode || undefined, status || undefined, assetClass || undefined, account || undefined).then(setRows);
  }, [mode, status, assetClass, account]);

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
        <ColumnPicker columns={JOURNAL_COLUMNS} visible={cols} onToggle={toggleCol} />
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
        <AccountSelect value={account} onChange={setAccount} />
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
          <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th></th>
                {cols.has("mode") && <th>Mode</th>}
                {cols.has("strategy") && <th>Strategy</th>}
                <th>Symbol</th>
                <th>Action</th>
                {cols.has("qty") && <th>Qty</th>}
                <th>Price</th>
                {cols.has("cost") && <th>Cost</th>}
                {cols.has("proceeds") && <th>Proceeds</th>}
                <th>P&L</th>
                {cols.has("status") && <th>Status</th>}
              </tr>
            </thead>
            <tbody>
              {events.map((e) => {
                const r = e.trade;
                return (
                  // The fragment IS the array element, so the key belongs here —
                  // a bare <> can't carry one, which is why it needs Fragment.
                  <Fragment key={e.key}>
                    <tr className="clickable" onClick={() => setExpanded(expanded === e.key ? null : e.key)}>
                      <td className="hint nowrap">{when(e.at)}</td>
                      <td>{expanded === e.key ? "▾" : "▸"}</td>
                      {cols.has("mode") && (
                        <td>
                          <span className={`pill ${r.mode === "shadow" ? "muted" : "ok"}`}>{r.mode}</span>
                        </td>
                      )}
                      {cols.has("strategy") && <td>{r.strategy}</td>}
                      <td className="sym">{r.symbol}</td>
                      {/* A buy has no result yet, so it stays neutral; a sell is
                          tinted by what it made. Rejected keeps its warning
                          colour — that IS a status, not an outcome. Colouring by
                          buy/sell printed every profitable exit in loss-red. */}
                      <td
                        className={
                          e.action === "Rejected"
                            ? "warn-text"
                            : e.action === "Sold" && e.pnl != null
                              ? e.pnl >= 0
                                ? "up"
                                : "down"
                              : ""
                        }
                      >
                        {e.action}
                      </td>
                      {cols.has("qty") && <td>{r.qty || "—"}</td>}
                      <td>{money(e.price)}</td>
                      {/* Cost is what you put in; proceeds is what came back.
                          A rejected row never bought, so both are blank rather
                          than $0.00 — nothing was spent, and zero would read as
                          a free trade. */}
                      {cols.has("cost") && (
                        <td>
                          {r.status === "rejected" || !r.entry_price || !r.qty
                            ? "—"
                            : `$${(r.entry_price * r.qty).toFixed(2)}`}
                        </td>
                      )}
                      {cols.has("proceeds") && (
                        <td>
                          {r.exit_price && r.qty ? `$${(r.exit_price * r.qty).toFixed(2)}` : "—"}
                        </td>
                      )}
                      <td className={e.pnl == null ? "" : e.pnl >= 0 ? "up" : "down"}>
                        {e.pnl == null ? "—" : `$${e.pnl.toFixed(2)}`}
                      </td>
                      {cols.has("status") && (
                        <td>
                          <span className={`pill ${r.status === "open" ? "ok" : r.status === "rejected" ? "warn" : "muted"}`}>
                            {r.status}
                          </span>
                        </td>
                      )}
                    </tr>
                    {expanded === e.key && (
                      <tr>
                        {/* Spans whatever is on screen. Six columns are always present
                            (time, chevron, symbol, action, price, P&L) plus the
                            optional ones — a fixed 9 broke the moment columns
                            became configurable. */}
                        <td colSpan={6 + cols.size} className="detail">
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
                  </Fragment>
                );
              })}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </>
  );
}
