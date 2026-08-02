import { useEffect, useState } from "react";
import { FidelityReport, getStrategies, runFidelity, StrategyRow } from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";
import { IconWarn } from "../components/icons";

/** Does the backtester actually reproduce what really happened?
 *
 *  Lives in Settings rather than the main navigation because the DECISION half
 *  is a validation instrument: once the replay is trusted it should agree every
 *  time, and a panel that always says "100%" does not deserve a tab. The
 *  EXECUTION half is different — what your fills really cost depends on your
 *  broker, your symbols and your order sizes, so it never becomes settled, and
 *  the number it produces is meant to go straight into the backtest's spread
 *  setting.
 */
export default function FidelityPanel() {
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [days, setDays] = useState(90);
  const [mode, setMode] = useState("paper");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<FidelityReport | null>(null);

  useEffect(() => {
    getStrategies()
      .then((rows) => {
        setStrategies(rows);
        if (rows.length && strategyId === null) setStrategyId(rows[0].id);
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function run() {
    if (strategyId === null) return;
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      setReport(await runFidelity({ strategy_id: strategyId, days, mode }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const d = report?.decision;
  const x = report?.execution;
  const pct = (v: number | null | undefined) => (v == null ? "—" : `${v}%`);

  return (
    <details className="card fold">
      <summary>
        <div className="cache-head">
          <h3>Backtest fidelity — is the replay telling the truth?</h3>
          <span className="hint">Compare real trades against a backtest of the same period</span>
        </div>
      </summary>

      <p className="hint">
        Point the backtester at a stretch you have <strong>already traded</strong> and see whether it agrees. The entry,
        exit and safety-rail code is shared between the live engine and the backtester — it is one implementation, not
        two — so the two cannot disagree about strategy logic. Anything that does differ is therefore either the{" "}
        <strong>data</strong> the replay saw or the <strong>price</strong> it assumed, and those need opposite fixes.
      </p>

      <div className="grid-3">
        <label>
          <span className="field-cap">Strategy</span>
          <select
            value={strategyId ?? ""}
            onChange={(e) => setStrategyId(e.target.value ? Number(e.target.value) : null)}
          >
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="field-cap">Compare against</span>
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="paper">Paper trades</option>
            <option value="live">Live trades</option>
            <option value="shadow">Shadow (would-be) trades</option>
          </select>
          <span className="field-help">
            Paper is the place to start: whether the replay picks the same trades is just as testable there, and it has
            the volume. What a fill <em>costs</em> is not — the broker simulates paper fills, so that half only becomes
            real with live trading.
          </span>
        </label>
        <label>
          <span className="field-cap">History (days)</span>
          <NumberField min={7} max={730} step={1} value={days} onChange={setDays} />
        </label>
      </div>

      <button type="button" onClick={run} disabled={busy || strategyId === null}>
        {busy ? "Comparing…" : "Run comparison"}
      </button>
      {error && <div className="error">{error}</div>}

      {report && d && x && (
        <>
          {(report.bar_gaps?.length ?? 0) > 0 && (
            <p className="hint warn">
              <IconWarn className="icon-inline" /> <strong>The replay had days with no price data.</strong> Read the
              mismatches below with that in mind: a trade "the backtest missed" on a day it had no bars for is a gap in
              the cache, not a fault in the replay. Fill the gap and re-run before drawing conclusions.
            </p>
          )}

          <h4>
            Did it pick the same trades? <InfoTip k="decision_fidelity" />
          </h4>
          <div className="stats">
            <div className="stat">
              <div className="stat-label">Agreed</div>
              <div className="stat-value">{pct(d.match_rate_pct)}</div>
              <div className="stat-label">{d.matched} of both sides' decisions</div>
            </div>
            <div className="stat">
              <div className="stat-label">Missed by the backtest</div>
              <div className={`stat-value ${d.missed_by_backtest ? "down" : ""}`}>{d.missed_by_backtest}</div>
              <div className="stat-label">really traded, not replayed</div>
            </div>
            <div className="stat">
              <div className="stat-label">Invented by the backtest</div>
              <div className={`stat-value ${d.invented_by_backtest ? "down" : ""}`}>{d.invented_by_backtest}</div>
              <div className="stat-label">replayed, never happened</div>
            </div>
            <div className="stat">
              <div className="stat-label">Blocked by a rail</div>
              <div className="stat-value">{report.rails_blocked.length}</div>
              <div className="stat-label">not a backtest error</div>
            </div>
          </div>
          <p className="hint">
            <InfoTip k="fidelity_buckets" /> Of the trades both sides took, {pct(d.same_exit_rule_pct)} left for the
            same reason and {pct(d.same_exit_day_pct)} left on the same day.
          </p>
          {d.manual_exits > 0 && (
            <p className="hint">
              <strong>
                {d.manual_exits} {d.manual_exits === 1 ? "trade was" : "trades were"} ended by hand
              </strong>{" "}
              — a force exit, an account reset, or reconciliation finding the broker no longer holding it. Those aren't
              strategy decisions, so the backtest had no way to make them: their entries still count above, but every
              exit number here skips them, and so does the trading cost below. Comparing your button press against a
              rule would measure the gap between two different decisions and call it slippage.
            </p>
          )}
          {!d.enough_to_judge && (
            <p className="hint warn">
              <IconWarn className="icon-inline" /> <strong>Only {d.matched} matched trades.</strong> Below about 30
              these are anecdotes rather than a measurement — one unusual trade moves everything.{" "}
              <InfoTip k="fidelity_sample" />
            </p>
          )}

          <h4>
            What did the fills really cost? <InfoTip k="execution_fidelity" />
          </h4>
          {report.execution_is_measurable ? (
            <>
              <div className="stats">
                <div className="stat">
                  <div className="stat-label">Measured cost per side</div>
                  <div className="stat-value">{pct(x.measured_cost_per_side_pct)}</div>
                  <div className="stat-label">from {x.fills_compared} real fills</div>
                </div>
                <div className="stat">
                  <div className="stat-label">Backtest assumes</div>
                  <div className="stat-value">{x.assumed_spread_pct}%</div>
                  <div className="stat-label">spread, per side</div>
                </div>
                <div className="stat">
                  <div className="stat-label">Backtest optimism</div>
                  <div className={`stat-value ${(x.backtest_pnl_optimism_usd ?? 0) > 0 ? "down" : ""}`}>
                    {x.backtest_pnl_optimism_usd == null ? "—" : `$${x.backtest_pnl_optimism_usd.toFixed(2)}`}
                  </div>
                  <div className="stat-label">over the matched trades</div>
                </div>
              </div>
              {x.suggested_spread_pct != null && (
                <p className="hint">
                  <strong>Use {x.suggested_spread_pct}% as the spread cost in future backtests.</strong> That is what
                  your fills actually cost, measured rather than guessed — and it is the one number here that stays
                  useful forever, because it depends on your broker, your symbols and your order sizes rather than on
                  whether the replay is correct.
                </p>
              )}
            </>
          ) : (
            <p className="hint">
              Not measurable against <strong>{report.mode}</strong> trades: the broker simulates those fills, so this
              would be measuring a simulation rather than a market. Switch to live trades once you have them — the
              trade-picking half above is fully meaningful either way.
            </p>
          )}

          {(report.live_only.length > 0 || report.backtest_only.length > 0) && (
            <>
              <h4>The disagreements</h4>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Day</th>
                      <th>Symbol</th>
                      <th>What happened</th>
                      <th>Why it matters</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.live_only.map((r) => (
                      <tr key={`l-${r.day}-${r.symbol}`}>
                        <td>{r.day}</td>
                        <td>{r.symbol}</td>
                        <td className="down">Traded for real, not replayed</td>
                        <td className="hint">The replay's view of that day was wrong — usually missing bars.</td>
                      </tr>
                    ))}
                    {report.backtest_only.map((r) => (
                      <tr key={`b-${r.day}-${r.symbol}`}>
                        <td>{r.day}</td>
                        <td>{r.symbol}</td>
                        <td className="down">Replayed, never happened</td>
                        <td className="hint">
                          The backtest thinks this was tradable when it wasn't — the mismatch to worry about.
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </details>
  );
}
