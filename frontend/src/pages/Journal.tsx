import { useCallback, useEffect, useState } from "react";
import { getJournal, JournalRow } from "../api";

function money(v: number | null) {
  if (v === null || v === undefined) return "—";
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: 4 })}`;
}

function when(iso: string | null) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export default function Journal() {
  const [rows, setRows] = useState<JournalRow[] | null>(null);
  const [mode, setMode] = useState<string>("");
  const [expanded, setExpanded] = useState<number | null>(null);

  const refresh = useCallback(() => {
    getJournal(mode || undefined).then(setRows);
  }, [mode]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <>
      <div className="toolbar">
        <h2>Trade journal</h2>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="">All modes</option>
          <option value="shadow">Shadow</option>
          <option value="paper">Paper</option>
        </select>
        <button className="small" onClick={refresh}>
          Refresh
        </button>
      </div>
      <div className="card">
        {!rows ? (
          <p>Loading…</p>
        ) : rows.length === 0 ? (
          <p className="hint">
            Nothing yet. Every decision the engine makes — including trades it wanted to make but a safety rail
            blocked — will appear here with its full reasoning.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th></th>
                <th>Mode</th>
                <th>Strategy</th>
                <th>Symbol</th>
                <th>Status</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>P&L</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <>
                  <tr key={r.id} className="clickable" onClick={() => setExpanded(expanded === r.id ? null : r.id)}>
                    <td>{expanded === r.id ? "▾" : "▸"}</td>
                    <td>
                      <span className={`pill ${r.mode === "shadow" ? "muted" : "ok"}`}>{r.mode}</span>
                    </td>
                    <td>{r.strategy}</td>
                    <td className="sym">{r.symbol}</td>
                    <td>
                      <span
                        className={`pill ${
                          r.status === "open" ? "ok" : r.status === "rejected" ? "warn" : "muted"
                        }`}
                      >
                        {r.status}
                      </span>
                    </td>
                    <td>{money(r.entry_price)}</td>
                    <td>{money(r.exit_price)}</td>
                    <td className={r.pnl == null ? "" : r.pnl >= 0 ? "up" : "down"}>
                      {r.pnl == null ? "—" : `$${r.pnl.toFixed(2)}`}
                    </td>
                  </tr>
                  {expanded === r.id && (
                    <tr key={`${r.id}-detail`}>
                      <td colSpan={8} className="detail">
                        <p>
                          <strong>Why it {r.status === "rejected" ? "was rejected" : "bought"}:</strong>{" "}
                          {r.entry_reason || "—"}
                        </p>
                        {r.exit_reason && (
                          <p>
                            <strong>Why it sold:</strong> {r.exit_reason}
                          </p>
                        )}
                        <p className="hint">
                          {r.qty ? `${r.qty} × ${r.symbol} (${money(r.notional)}) · ` : ""}
                          entered {when(r.entry_at)}
                          {r.exit_at ? ` · exited ${when(r.exit_at)}` : ""} · config v{r.config_version_id ?? "?"}
                        </p>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
