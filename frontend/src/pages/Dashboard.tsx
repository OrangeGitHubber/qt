import { useCallback, useEffect, useState } from "react";
import { EngineState, getEngine, getScoreboard, Scoreboard, setEngineMode, StatusResponse } from "../api";
import InfoTip from "../components/InfoTip";
import LineChart from "../components/LineChart";

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
    </>
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
