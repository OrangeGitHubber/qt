import { useCallback, useEffect, useState } from "react";
import {
  EngineState,
  getEngine,
  getScoreboard,
  getStrategyPnl,
  getStrategyPnlDaily,
  Scoreboard,
  setEngineMode,
  StatusResponse,
  StrategyPnl,
  StrategyPnlDaily,
} from "../api";
import InfoTip from "../components/InfoTip";
import LineChart from "../components/LineChart";
import StackedPnlBars, { PnlSeries } from "../components/StackedPnlBars";

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
              <dt>Engine heartbeat</dt>
              <dd>
                <span className={`pill ${hb.stale ? "warn" : "ok"}`}>{hb.label}</span>
              </dd>
            </dl>
          ) : (
            <p>No data.</p>
          )}
        </div>
        <EngineCard />
      </div>
      <ScoreboardCard />
      <StrategyContributionsCard />
    </>
  );
}

function StrategyContributionsCard() {
  const [totals, setTotals] = useState<StrategyPnl | null>(null);
  const [daily, setDaily] = useState<StrategyPnlDaily | null>(null);
  const [windowDays, setWindowDays] = useState(30); // chart lookback; 0 = all time

  // Totals table is all-time and fetched once.
  useEffect(() => {
    getStrategyPnl().then(setTotals).catch(() => setTotals(null));
  }, []);

  // The daily chart re-fetches whenever the selected window changes.
  useEffect(() => {
    getStrategyPnlDaily(windowDays).then(setDaily).catch(() => setDaily(null));
  }, [windowDays]);

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
      <h3>
        Strategy contributions <span className="hint">(realized · {totals.mode} mode)</span>
      </h3>
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

function EngineCard() {
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
            {engine.leverage.enabled ? "ENABLED ⚠" : "locked off"}
          </span>
        </dd>
      </dl>
      {note && <div className="error">{note}</div>}
    </div>
  );
}

function ScoreboardCard() {
  const [board, setBoard] = useState<Scoreboard | null>(null);

  useEffect(() => {
    getScoreboard().then(setBoard);
  }, []);

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
        />
      )}
    </div>
  );
}
