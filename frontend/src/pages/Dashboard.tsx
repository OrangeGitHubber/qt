import { useCallback, useEffect, useMemo, useState } from "react";
import {
  EngineState,
  getEngine,
  getOpenPositions,
  getScoreboard,
  getStrategies,
  getStrategyPnl,
  getStrategyPnlDaily,
  OpenPositionsResponse,
  Scoreboard,
  setEngineMode,
  StatusResponse,
  StrategyPnl,
  StrategyPnlDaily,
} from "../api";
import { requestNav } from "../lib/nav";
import AccountSelect from "../components/AccountSelect";
import InfoTip from "../components/InfoTip";
import LineChart, { DayHolding } from "../components/LineChart";
import StackedPnlBars, { PnlSeries } from "../components/StackedPnlBars";
import { IconWarn } from "../components/icons";

// Categorical palette for per-strategy colors (chart + table swatch share it).
const PALETTE = ["#4f8cff", "#2ecc71", "#f39c12", "#a78bfa", "#22d3ee", "#f472b6", "#e74c3c", "#94a3b8"];

// Lookback windows for the daily-contribution chart. 0 = all time (no cutoff).
const PNL_RANGES: { label: string; days: number }[] = [
  { label: "7D", days: 7 },
  { label: "30D", days: 30 },
  { label: "90D", days: 90 },
  { label: "All", days: 0 },
];

function money(v: string | undefined, currency = "USD") {
  if (v === undefined) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(v));
}

function when(iso: string | undefined) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function heartbeat(iso: string | null): { label: string; stale: boolean } {
  if (!iso) return { label: "no tick yet", stale: true };
  const ageMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(ageMs / 60000);
  const label = mins < 1 ? "just now" : mins === 1 ? "1 min ago" : `${mins} min ago`;
  return { label, stale: ageMs > 5 * 60_000 };
}

// Signed dollars: "+$12.30" / "−$4.50" with a real minus sign.
function signed(n: number): string {
  return `${n >= 0 ? "+" : "−"}$${Math.abs(n).toFixed(2)}`;
}

export default function Dashboard({ status }: { status: StatusResponse; onRefresh?: () => void }) {
  const { broker, market, error } = status;
  const hb = heartbeat(status.last_tick_at);
  return (
    <>
      {error && <div className="card error">{error}</div>}
      <GettingStartedCard />
      <div className="grid">
        <div className="card">
          <h3>Broker — Alpaca (paper)</h3>
          {broker ? (
            <dl>
              <dt>Account</dt>
              <dd>
                {broker.account_number} <span className={`pill ${broker.status === "ACTIVE" ? "ok" : "warn"}`}>{broker.status}</span>
              </dd>
              <dt>Equity</dt>
              <dd>{money(broker.equity, broker.currency)}</dd>
              <dt>Cash</dt>
              <dd>{money(broker.cash, broker.currency)}</dd>
              <dt>Buying power</dt>
              <dd>{money(broker.buying_power, broker.currency)}</dd>
            </dl>
          ) : (
            <p>No data.</p>
          )}
        </div>
        <div className="card">
          <h3>Market</h3>
          {market ? (
            <dl>
              <dt>US stock market</dt>
              <dd>
                <span className={`pill ${market.is_open ? "ok" : "muted"}`}>{market.is_open ? "OPEN" : "CLOSED"}</span>
              </dd>
              <dt>{market.is_open ? "Closes" : "Next open"}</dt>
              <dd>{when(market.is_open ? market.next_close : market.next_open)}</dd>
              <dt>Crypto market</dt>
              <dd>
                <span className="pill ok">OPEN 24/7</span>
              </dd>
            </dl>
          ) : (
            <p>No data.</p>
          )}
        </div>
        <EngineCard heartbeat={hb} />
      </div>
      <OpenPositionsCard />
      <ScoreboardCard />
      <StrategyContributionsCard />
    </>
  );
}

// First-run guide: shown only until the FIRST strategy exists, then it retires
// itself. Walks the whole road (universe → strategy → backtest → optimize →
// shadow → paper) with jump buttons, so nobody needs the GitHub instructions.
function GettingStartedCard() {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    getStrategies()
      .then((rows) => setCount(rows.length))
      .catch(() => setCount(null));
  }, []);
  if (count !== 0) return null;
  const go = (tab: string, label: string) => (
    <button type="button" className="small btn-ghost" onClick={() => requestNav({ tab })}>
      {label} →
    </button>
  );
  return (
    <div className="card">
      <h3>Getting started — from zero to a bot you can trust</h3>
      <p className="hint">
        You're connected. Everything below starts <strong>off</strong> — nothing trades until you deliberately turn it
        on, and even then it's simulated money first.
      </p>
      <ol className="steps">
        <li>
          <strong>Pick a universe.</strong> Add a few names to the watchlist (stocks and crypto in one search), or
          browse the curated sector/theme baskets. {go("watchlist", "Watchlist")} {go("baskets", "Baskets")}
        </li>
        <li>
          <strong>Create your first strategy from a preset.</strong> Every field explains itself with a ? bubble, and
          the strategy is created <strong>disabled</strong>. {go("strategies", "Strategies")}
        </li>
        <li>
          <strong>Backtest it.</strong> The same rules replay over past prices on the strategy's own universe — equity
          curve vs buy-and-hold vs SPY, every trade with its reason, and what was held each day.{" "}
          {go("backtest", "Backtest")}
        </li>
        <li>
          <strong>Optionally, let the optimizer search better settings.</strong> It judges configs only on history the
          search never saw, and tells you how many combinations it tried. {go("optimizer", "Optimizer")}
        </li>
        <li>
          <strong>Turn it on gently.</strong> Enable the strategy, then start the engine below in{" "}
          <strong>shadow</strong> mode — it journals every trade it <em>would</em> make, placing no orders. Graduate to{" "}
          <strong>paper</strong> when you're comfortable, and judge it by the scoreboard's "vs holding SPY" line.
        </li>
      </ol>
      <p className="hint">
        If a search box comes up empty, the symbol directory hasn't synced yet — Settings → Symbol directory → Sync
        now. Slack alerts are optional under Settings too.
      </p>
    </div>
  );
}

function StrategyContributionsCard() {
  const [totals, setTotals] = useState<StrategyPnl | null>(null);
  const [daily, setDaily] = useState<StrategyPnlDaily | null>(null);
  const [windowDays, setWindowDays] = useState(30); // chart lookback; 0 = all time
  const [account, setAccount] = useState<string>(""); // "" = current account (default)

  // Totals re-fetch when the account filter changes.
  useEffect(() => {
    getStrategyPnl(account || undefined).then(setTotals).catch(() => setTotals(null));
  }, [account]);

  // The daily chart re-fetches whenever the window or account changes.
  useEffect(() => {
    getStrategyPnlDaily(windowDays, account || undefined).then(setDaily).catch(() => setDaily(null));
  }, [windowDays, account]);

  if (!totals) return null;

  // One stable colour per strategy, shared by the table swatch and the chart.
  // Keyed off the all-time totals order so colours don't shuffle when the chart
  // window changes (the daily set is a subset of the strategies in totals).
  const orderIds = totals.strategies.map((s) => s.strategy_id);
  const colorFor = (id: number) => PALETTE[Math.max(0, orderIds.indexOf(id)) % PALETTE.length];
  const series: PnlSeries[] = (daily?.strategies ?? []).map((s) => ({
    name: s.name,
    color: colorFor(s.strategy_id),
    values: s.values,
  }));

  return (
    <div className="card">
      <div className="card-head">
        <h3>
          Strategy contributions <span className="hint">(realized · {totals.mode} mode)</span>
        </h3>
        <AccountSelect value={account} onChange={setAccount} />
      </div>
      <p className="hint">
        Each strategy's realized (locked-in) profit or loss — the exact split behind the single scoreboard line, and it
        sums to the account's realized total. Open positions are shown as a count, not yet marked to market.
      </p>
      {totals.strategies.length === 0 ? (
        <p className="hint">
          No {totals.mode}-mode trades yet — each strategy's contribution appears here once it closes a trade.
        </p>
      ) : (
        <>
          <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Realized P&amp;L</th>
                <th>Trades</th>
                <th>Win rate</th>
                <th>Open</th>
              </tr>
            </thead>
            <tbody>
              {totals.strategies.map((s) => (
                <tr key={s.strategy_id}>
                  <td className="sym">
                    <span className="swatch" style={{ background: colorFor(s.strategy_id) }} /> {s.name}
                  </td>
                  <td className={s.realized_pnl >= 0 ? "up" : "down"}>{signed(s.realized_pnl)}</td>
                  <td>{s.trades}</td>
                  <td>{s.win_rate == null ? "—" : `${Math.round(s.win_rate * 100)}%`}</td>
                  <td>{s.open_positions || "—"}</td>
                </tr>
              ))}
              <tr>
                <td className="sym" style={{ borderTop: "2px solid var(--border)" }}>
                  Total
                </td>
                <td
                  className={totals.realized_total >= 0 ? "up" : "down"}
                  style={{ borderTop: "2px solid var(--border)", fontWeight: 700 }}
                >
                  {signed(totals.realized_total)}
                </td>
                <td colSpan={3} style={{ borderTop: "2px solid var(--border)" }}></td>
              </tr>
            </tbody>
          </table>
          </div>

          <p className="hint" style={{ marginTop: "1rem" }}>
            Daily realized contribution ({windowDays > 0 ? `last ${windowDays} days` : "all time"}, days with trades)
            — each bar is a day, stacked by strategy; gains rise above the line, losses drop below.
          </p>
          <div className="range-buttons">
            {PNL_RANGES.map((r) => (
              <button
                key={r.label}
                className={`small ${windowDays === r.days ? "mode-active" : ""}`}
                onClick={() => setWindowDays(r.days)}
              >
                {r.label}
              </button>
            ))}
          </div>
          {daily && daily.days.length > 0 ? (
            <>
              <StackedPnlBars days={daily.days} series={series} />
              <div className="legend">
                {series.map((s) => (
                  <span key={s.name}>
                    <span className="swatch" style={{ background: s.color }} /> {s.name}
                  </span>
                ))}
              </div>
            </>
          ) : (
            <p className="hint">No closed trades in this window.</p>
          )}
        </>
      )}
    </div>
  );
}

// Every open position across ALL strategies, each with its owner named. The
// "position already open for this symbol" rail is account-wide, so the holder
// is often a different strategy than the one that got blocked — this card is
// where you find out which. Polls like the engine card.
function OpenPositionsCard() {
  const [data, setData] = useState<OpenPositionsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const refresh = useCallback(() => {
    getOpenPositions()
      .then((d) => {
        setData(d);
        setErr(null);
      })
      .catch((e: Error) => setErr(e.message));
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, [refresh]);

  if (err) return <div className="card error">Open positions: {err}</div>;
  if (!data) return null;
  const pnl = data.total_unrealized_pnl;
  return (
    // Folded by default: the summary carries the numbers that matter at a
    // glance (how many open, total unrealized); open it for the per-position
    // detail. Same fold pattern as the Settings cards.
    <details className="card fold">
      <summary>
        <h3>
          Open positions — all strategies{" "}
          <span className="hint">
            ({data.positions.length} open
            {data.positions.length > 0 && (
              <>
                {" · "}
                <span className={pnl >= 0 ? "up" : "down"}>
                  {pnl >= 0 ? "+" : "−"}${Math.abs(pnl).toFixed(2)} unrealized
                </span>
              </>
            )}
            )
          </span>
        </h3>
      </summary>
      {data.positions.length === 0 ? (
        <p className="hint">
          Nothing is held right now. When a strategy buys, the position appears here with its owner — including the one
          behind any "position already open for this symbol" rail block.
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Symbol</th>
                <th>Mode</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Now</th>
                <th>Unrealized</th>
                <th>Held since</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((p, i) => (
                <tr key={i}>
                  <td className="sym">{p.strategy_name}</td>
                  <td className="sym">{p.symbol}</td>
                  <td>{p.mode}</td>
                  <td>{p.qty}</td>
                  <td>${p.entry_price.toFixed(4)}</td>
                  <td>{p.current_price != null ? `$${p.current_price.toFixed(4)}` : "—"}</td>
                  <td className={p.unrealized_pnl == null ? "" : p.unrealized_pnl >= 0 ? "up" : "down"}>
                    {p.unrealized_pnl != null
                      ? `${p.unrealized_pnl >= 0 ? "+" : "−"}$${Math.abs(p.unrealized_pnl).toFixed(2)} (${p.unrealized_pct}%)`
                      : "—"}
                  </td>
                  <td className="hint">{p.entry_at ? new Date(p.entry_at).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}

function EngineCard({ heartbeat }: { heartbeat: { stale: boolean; label: string } }) {
  const [engine, setEngine] = useState<EngineState | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getEngine().then(setEngine).catch((e: Error) => setNote(e.message));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, [refresh]);

  async function switchMode(mode: string) {
    setNote(null);
    try {
      if (mode === "paper") {
        const sure = window.confirm(
          "Paper mode places SIMULATED orders on your Alpaca paper account (no real money). Continue?",
        );
        if (!sure) return;
        await setEngineMode("paper", true);
      } else {
        await setEngineMode(mode);
      }
      refresh();
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  if (!engine) return <div className="card">Engine: loading…</div>;
  return (
    <div className="card">
      <h3>
        Engine <InfoTip k="shadow_mode" />
      </h3>
      <div className="mode-switch">
        {engine.modes.map((m) => (
          <button
            key={m}
            className={`small ${engine.mode === m ? "mode-active" : ""}`}
            onClick={() => engine.mode !== m && switchMode(m)}
          >
            {m === "off" ? "Off" : m === "shadow" ? "Shadow" : "Paper"}
          </button>
        ))}
      </div>
      <dl>
        <dt>Heartbeat</dt>
        <dd>
          <span className={`pill ${heartbeat.stale ? "warn" : "ok"}`}>{heartbeat.label}</span>
        </dd>
        <dt>Regime</dt>
        <dd>
          {engine.regime ? (
            <>
              <span className={`pill ${engine.regime.ok ? "ok" : "warn"}`}>
                {engine.regime.ok ? "BULL — trading allowed" : "CAUTION — stock entries blocked"}
              </span>{" "}
              <span className="hint">{engine.regime.detail}</span>
            </>
          ) : (
            "—"
          )}
        </dd>
        <dt>Today</dt>
        <dd>
          {engine.today.entries} entries · {engine.today.open_positions} open ·{" "}
          <span className={engine.today.realized_pnl >= 0 ? "up" : "down"}>
            ${engine.today.realized_pnl.toFixed(2)} realized
          </span>
        </dd>
        <dt>Leverage</dt>
        <dd>
          <span className={`pill ${engine.leverage.enabled ? "warn" : "ok"}`}>
            {engine.leverage.enabled ? (
              <>
                ENABLED <IconWarn className="icon-inline" />
              </>
            ) : (
              "locked off"
            )}
          </span>
        </dd>
      </dl>
      {note && <div className="error">{note}</div>}
    </div>
  );
}

function ScoreboardCard() {
  const [board, setBoard] = useState<Scoreboard | null>(null);
  const [daily, setDaily] = useState<StrategyPnlDaily | null>(null);

  useEffect(() => {
    getScoreboard().then(setBoard);
    // Per-strategy realized P&L per day — the attribution behind each move.
    getStrategyPnlDaily(0).then(setDaily).catch(() => setDaily(null));
  }, []);

  // {day -> per-strategy contribution}, converted from DOLLARS into the same
  // percentage points the chart plots (÷ the baseline equity), so "who moved the
  // line" is directly comparable to the line itself. Realized only: a position
  // that merely moved in value isn't attributed until it's closed, so the parts
  // won't always sum to the day's step — said plainly under the chart.
  const attribution = useMemo<Record<string, DayHolding[]> | undefined>(() => {
    if (!daily || !board?.base_equity) return undefined;
    const base = board.base_equity;
    const out: Record<string, DayHolding[]> = {};
    daily.days.forEach((day, i) => {
      const rows = daily.strategies
        .map((s) => ({ symbol: s.name, qty: 1, day_pnl_pct: ((s.values[i] ?? 0) / base) * 100 }))
        .filter((r) => r.day_pnl_pct !== 0)
        .sort((a, b) => Math.abs(b.day_pnl_pct) - Math.abs(a.day_pnl_pct));
      if (rows.length) out[day] = rows;
    });
    return out;
  }, [daily, board]);

  return (
    <div className="card scoreboard">
      <h3>Scoreboard — bot vs. doing nothing</h3>
      <p className="hint">
        The honesty meter: the bot's account value against simply having bought and held SPY or Bitcoin on day one.
        If the bot can't beat these lines in paper trading, it doesn't deserve real money.
      </p>
      {board && board.verdict && <p className="verdict">{board.verdict}</p>}
      {board && (
        <LineChart
          labels={board.days}
          series={[
            { label: "QT bot", color: "var(--accent)", values: board.bot },
            { label: "Buy & hold SPY", color: "var(--ok)", values: board.spy },
            { label: "Buy & hold BTC", color: "var(--warn)", values: board.btc },
          ]}
          holdings={attribution}
          holdingsLabel="strategies"
        />
      )}
      {board && (
        <p className="hint">
          Measured from <strong>{board.days[0] ?? "—"}</strong> on account{" "}
          <strong>{board.account ?? "—"}</strong> — equity is only comparable within one broker account, so switching
          accounts starts a fresh line rather than reading the balance change as a loss.
          {attribution && (
            <>
              {" "}
              Hover a day to see which strategies moved it. Attribution is <strong>realized</strong> P&amp;L (closed
              trades), so it won't always add up to the day's full step — open positions move the line too.
            </>
          )}
        </p>
      )}
    </div>
  );
}
