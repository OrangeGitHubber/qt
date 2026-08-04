import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { FeeSummary, getFeeSummary, getJournal, getStrategies, JournalRow, JOURNAL_LIMIT, StrategyRow } from "../api";
import ColumnPicker, { useColumnPrefs } from "../components/ColumnPicker";
import AccountSelect from "../components/AccountSelect";
import { fmtDateTime as when, instantMs, zoneAbbr } from "../lib/datetime";

function money(v: number | null) {
  if (v === null || v === undefined) return "—";
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: 4 })}`;
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
//
// "Fees" is deliberately always "—" today, and there is no "Net P&L" column to
// go with it. Alpaca posts crypto fees end of day as activities that carry no
// order id and only a DATE — so nothing says which fill a fee belongs to, and a
// per-trade split would be a guess wearing a dollar sign. What the broker
// charged the ACCOUNT is real, and that total is shown under the table instead.
type JCol = "cost" | "proceeds" | "qty" | "mode" | "strategy" | "status" | "fees";
const JOURNAL_COLUMNS: { key: JCol; label: string }[] = [
  { key: "mode", label: "Mode" },
  { key: "strategy", label: "Strategy" },
  { key: "qty", label: "Qty" },
  { key: "cost", label: "Cost (what went in)" },
  { key: "proceeds", label: "Proceeds (what came back)" },
  { key: "fees", label: "Fees (broker)" },
  { key: "status", label: "Status" },
];
const JOURNAL_COLS_KEY = "qt.journal.columns";
const JOURNAL_DEFAULT_COLS: JCol[] = ["mode", "strategy", "cost", "status"];

export default function Journal() {
  const [rows, setRows] = useState<JournalRow[] | null>(null);
  const [mode, setMode] = useState<string>("");
  // Defaults to TRADES. On "All" the row cap is swallowed whole by rejected
  // candidates — the engine logs hundreds a day — so the page came up looking
  // like nothing had ever traded. A journal should open on what happened.
  const [status, setStatus] = useState<"" | "trades" | "rejected">("trades");
  const [assetClass, setAssetClass] = useState<"" | "stock" | "crypto">("");
  const [account, setAccount] = useState<string>("");
  // Symbol is free text rather than a dropdown: the options could only be built
  // from the rows already fetched, so a picker would silently omit any symbol
  // whose trades fell outside the current page — exactly the ones you open this
  // filter to find. The datalist below suggests what IS loaded without limiting
  // the choice to it.
  const [symbol, setSymbol] = useState<string>("");
  const [strategyId, setStrategyId] = useState<number | "">("");
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [feeSummary, setFeeSummary] = useState<FeeSummary | null>(null);
  const [cols, toggleCol] = useColumnPrefs<JCol>(
    JOURNAL_COLS_KEY,
    JOURNAL_COLUMNS.map((c) => c.key),
    JOURNAL_DEFAULT_COLS,
  );

  const refresh = useCallback(() => {
    getJournal(
      mode || undefined, status || undefined, assetClass || undefined, account || undefined,
      undefined, symbol.trim() || undefined, strategyId === "" ? undefined : strategyId,
    ).then(setRows);
    // Fees are account-scoped, not row-scoped, so they follow the account
    // filter only. A failure leaves it null, which renders as "unknown" — the
    // journal itself must still load.
    getFeeSummary(account || undefined)
      .then(setFeeSummary)
      .catch(() => setFeeSummary(null));
  }, [mode, status, assetClass, account, symbol, strategyId]);

  // HAS THE READER NARROWED ANYTHING? The symbol and strategy filters run on the
  // SERVER, so an empty result is a real state and not a rendering accident —
  // and "nothing matched what you asked for" is a different sentence from
  // "nothing has ever happened". Only the controls that ADD a restriction count:
  // "Trades" is the page's own default and "All" is the widest view, so neither
  // should make a fresh install accuse itself of having a filter on.
  const narrowed =
    !!symbol.trim() || strategyId !== "" || !!mode || !!assetClass || !!account || status === "rejected";

  const clearFilters = useCallback(() => {
    setMode("");
    setStatus("");
    setAssetClass("");
    setAccount("");
    setSymbol("");
    setStrategyId("");
  }, []);

  // The strategy list is small and static enough to fetch once; a name is far
  // more useful in the picker than the id the API filters on.
  useEffect(() => {
    getStrategies().then(setStrategies).catch(() => undefined);
  }, []);

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
    out.sort((a, b) => instantMs(b.at) - instantMs(a.at));
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
        <input
          className="filter-symbol"
          list="journal-symbols"
          value={symbol}
          placeholder="Symbol…"
          aria-label="Filter by symbol"
          onChange={(e) => setSymbol(e.target.value)}
        />
        <datalist id="journal-symbols">
          {[...new Set((rows ?? []).map((r) => r.symbol))].sort().map((sym) => (
            <option key={sym} value={sym} />
          ))}
        </datalist>
        <select
          value={strategyId}
          aria-label="Filter by strategy"
          onChange={(e) => setStrategyId(e.target.value ? Number(e.target.value) : "")}
        >
          <option value="">All strategies</option>
          {strategies.map((st) => (
            <option key={st.id} value={st.id}>
              {st.name}
            </option>
          ))}
        </select>
        <AccountSelect value={account} onChange={setAccount} />
        <button className="small btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>
      {(rows?.length ?? 0) >= JOURNAL_LIMIT && (
        <p className="hint">
          Showing the most recent <strong>{JOURNAL_LIMIT}</strong> rows — older activity is hidden. The{" "}
          <strong>All</strong> view includes every rejected candidate and the engine logs hundreds a day, so
          narrow with the filters to see further back.
        </p>
      )}
      <div className="card">
        {!rows ? (
          <p>Loading…</p>
        ) : events.length === 0 ? (
          narrowed ? (
            <p className="hint">
              No journal entry matches these filters — and because the symbol and strategy filters are applied by
              the server, that is the whole journal's answer, not just this page of it.{" "}
              <button type="button" className="small btn-ghost" onClick={clearFilters}>
                Clear filters
              </button>
            </p>
          ) : (
            <p className="hint">
              Nothing yet. Every decision the engine makes — including trades it wanted to make but a safety rail
              blocked — will appear here with its full reasoning.
            </p>
          )
        ) : (
          <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time ({zoneAbbr()})</th>
                <th></th>
                {cols.has("mode") && <th>Mode</th>}
                {cols.has("strategy") && <th>Strategy</th>}
                <th>Symbol</th>
                <th>Action</th>
                {cols.has("qty") && <th>Qty</th>}
                <th>Price</th>
                {cols.has("cost") && <th>Cost</th>}
                {cols.has("proceeds") && <th>Proceeds</th>}
                {cols.has("fees") && <th>Fees</th>}
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
                      {/* The broker's fee for THIS trade, which Alpaca does not
                          tell us — its fee activities have no order id and no
                          time. So this is "—" (unknown), not $0.00 (charged
                          nothing). It reads a real value the day a broker
                          reports fees per fill. */}
                      {cols.has("fees") && (
                        <td title={r.fees == null ? "Alpaca reports fees per day, not per trade — see the account total below" : undefined}>
                          {r.fees == null ? "—" : `$${r.fees.toFixed(2)}`}
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
                                {when(r.entry_at)} ·{" "}
                                {/* A label and its number must not be split across a line —
                                    keep them welded whatever the column width. */}
                                <span className="nowrap">
                                  P&amp;L {r.pnl == null ? "—" : `$${r.pnl.toFixed(2)}`}
                                </span>{" "}
                                · config v{r.config_version_id ?? "?"}
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
        {/* Account-level fees. Kept OUT of the table on purpose: this is the
            granularity Alpaca actually reports, and putting it in a per-row
            column would imply an attribution that does not exist. */}
        <FeesNote summary={feeSummary} />
      </div>
    </>
  );
}

/** What the broker really charged, said plainly — including the parts we don't
    know. Never prints $0.00 for an unknown: zero would claim the account traded
    for free. */
function FeesNote({ summary }: { summary: FeeSummary | null }) {
  if (!summary) return null;
  const { total_usd, is_estimate, unvalued, synced_through } = summary;
  return (
    <p className="hint table-footnote" style={{ marginTop: "0.75rem" }}>
      <strong>Broker fees (this account):</strong>{" "}
      {total_usd == null ? (
        <>
          — · nothing synced yet.{" "}
          {synced_through
            ? `Checked through ${synced_through}; Alpaca reported no fees for that period.`
            : "US stocks are commission-free; crypto fees post end of day and are picked up the next morning."}
        </>
      ) : (
        <>
          ${total_usd.toFixed(2)}
          {is_estimate && " (estimated)"}
          {synced_through && ` · synced through ${synced_through}`}
        </>
      )}
      {unvalued > 0 && ` · ${unvalued} fee(s) Alpaca reported in a form we couldn't value in dollars, so the total is short by that much.`}
      {is_estimate && (
        <>
          {" "}
          Alpaca charges crypto fees in the coin you receive, so part of this is that coin valued at the
          broker's mark — an estimate, not a dollar figure Alpaca quoted.
        </>
      )}{" "}
      Fees are reported per day, not per trade, so the P&amp;L column above is gross.
    </p>
  );
}
