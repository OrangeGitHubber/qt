import { FormEvent, useEffect, useState } from "react";
import { BacktestResult, getStrategies, runBacktest, StrategyRow } from "../api";
import LineChart from "../components/LineChart";

function Stat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

export default function Backtest() {
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [symbols, setSymbols] = useState("");
  const [days, setDays] = useState(90);
  const [timeframe, setTimeframe] = useState("1Hour");
  const [cash, setCash] = useState(5000);
  const [spread, setSpread] = useState(0.1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);

  useEffect(() => {
    getStrategies().then((rows) => {
      setStrategies(rows);
      if (rows.length && strategyId === null) setStrategyId(rows[0].id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function run(e: FormEvent) {
    e.preventDefault();
    if (strategyId === null) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await runBacktest({
        strategy_id: strategyId,
        symbols: symbols.split(",").map((s) => s.trim()).filter(Boolean),
        days,
        timeframe,
        starting_cash: cash,
        spread_pct: spread,
      });
      setResult(r);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="toolbar">
        <h2>Backtest</h2>
      </div>
      <div className="card">
        <p className="hint">
          Replays a strategy's exact rules over past prices — the same code the live engine runs. Honest limits: it
          tests a <strong>fixed symbol list</strong> (not what the scanner would have picked each day), fills are
          modeled as price ± the spread cost, and the free IEX feed sees a slice of the market.{" "}
          <strong>Past results predict nothing</strong> — a backtest can only kill bad ideas cheaply, not promise good
          ones.
        </p>
        <form onSubmit={run}>
          <div className="filter-grid">
            <label>
              Strategy
              <select value={strategyId ?? ""} onChange={(e) => setStrategyId(Number(e.target.value))} required>
                {strategies.length === 0 && <option value="">— create a strategy first —</option>}
                {strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.asset_class})
                  </option>
                ))}
              </select>
            </label>
            <label>
              Symbols (comma-separated; blank = your watchlist)
              <input value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="NVDA, AMD, TSLA" />
            </label>
            <label>
              History (days)
              <input type="number" min={7} max={730} value={days} onChange={(e) => setDays(Number(e.target.value))} />
            </label>
            <label>
              Bar size
              <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                <option value="15Min">15 minutes (slow, precise)</option>
                <option value="1Hour">1 hour (recommended)</option>
                <option value="1Day">1 day (fast, coarse)</option>
              </select>
            </label>
            <label>
              Starting cash ($)
              <input type="number" min={100} step="any" value={cash} onChange={(e) => setCash(Number(e.target.value))} />
            </label>
            <label>
              Spread cost per side (%)
              <input type="number" min={0} max={2} step={0.05} value={spread} onChange={(e) => setSpread(Number(e.target.value))} />
            </label>
          </div>
          {error && <div className="error">{error}</div>}
          <button disabled={busy || strategyId === null}>{busy ? "Replaying history…" : "Run backtest"}</button>
        </form>
      </div>

      {result && (
        <>
          <div className="card">
            <h3>
              {result.strategy_name} · {result.symbols.join(", ")} · last {result.days} days ({result.timeframe})
            </h3>
            {result.trades === 0 && result.diagnosis?.summary && (
              <div className="card note" style={{ cursor: "default" }}>
                <strong>Why zero trades?</strong> {result.diagnosis.summary}
                <p className="hint">
                  {result.diagnosis.bars_evaluated.toLocaleString()} bars evaluated · biggest day-gain seen:{" "}
                  {result.diagnosis.max_day_gain_pct ?? "—"}% · days reaching your gain threshold:{" "}
                  {result.diagnosis.days_reaching_min_gain} · rejected by gain/VWAP/time-window:{" "}
                  {result.diagnosis.rejected_day_gain}/{result.diagnosis.rejected_vwap}/
                  {result.diagnosis.rejected_entry_window} · blocked by rails: {result.diagnosis.entry_ok_but_rail_blocked}
                </p>
              </div>
            )}
            <div className="stats">
              <Stat
                label="Net P&L"
                value={`$${result.net_pnl.toLocaleString()} (${result.net_pnl_pct >= 0 ? "+" : ""}${result.net_pnl_pct}%)`}
                tone={result.net_pnl >= 0 ? "up" : "down"}
              />
              <Stat label="Trades" value={String(result.trades)} />
              <Stat label="Win rate" value={result.win_rate != null ? `${result.win_rate}%` : "—"} />
              <Stat label="Avg win / loss" value={`${result.avg_win ?? "—"} / ${result.avg_loss ?? "—"}`} />
              <Stat label="Profit factor" value={result.profit_factor != null ? String(result.profit_factor) : "—"} />
              <Stat label="Max drawdown" value={`${result.max_drawdown_pct}%`} tone={result.max_drawdown_pct > 10 ? "down" : undefined} />
            </div>
            <LineChart
              labels={result.equity_days}
              series={[
                { label: "Strategy", color: "var(--accent)", values: result.equity },
                ...(result.benchmark
                  ? [{ label: `Hold ${result.benchmark_symbol}`, color: "var(--ok)", values: result.benchmark }]
                  : []),
              ]}
            />
            {result.benchmark && result.benchmark.length > 0 && (
              <p className="verdict">
                {(() => {
                  const bot = result.equity[result.equity.length - 1] ?? 0;
                  const bench = result.benchmark[result.benchmark.length - 1];
                  if (bench == null) return null;
                  return bot > bench
                    ? `Strategy beat buy-and-hold ${result.benchmark_symbol} by ${(bot - bench).toFixed(2)} points over this window.`
                    : `Buy-and-hold ${result.benchmark_symbol} beat the strategy by ${(bench - bot).toFixed(2)} points — the bar to clear.`;
                })()}
              </p>
            )}
          </div>
          <div className="card">
            <h3>Simulated trades ({result.trade_list.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>P&L</th>
                  <th>Exit reason</th>
                </tr>
              </thead>
              <tbody>
                {result.trade_list.map((t, i) => (
                  <tr key={i}>
                    <td className="sym">{t.symbol}</td>
                    <td>
                      ${t.entry_price} <span className="hint">{new Date(t.entry_at).toLocaleDateString()}</span>
                    </td>
                    <td>${t.exit_price}</td>
                    <td className={(t.pnl ?? 0) >= 0 ? "up" : "down"}>${t.pnl?.toFixed(2)}</td>
                    <td className="hint">{t.exit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
