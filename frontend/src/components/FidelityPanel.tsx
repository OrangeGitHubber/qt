import { useEffect, useState } from "react";
import { FidelityReport, getStrategies, runFidelity, StrategyRow } from "../api";
import InfoTip from "../components/InfoTip";
import { IconWarn } from "../components/icons";
import { fmtHm, fmtIsoDate, zoneAbbr } from "../lib/datetime";

/** Does the backtester actually reproduce what really happened?
 *
 *  Its own tab under Backtest. It used to be a collapsed fold at the foot of the
 *  page, on the reasoning that "can I trust this?" only means something once
 *  there is a result to ask it of — but the report is long, it is run on its own
 *  rather than after a backtest, and being last-and-collapsed made a validation
 *  instrument read like a footnote.
 *
 *  The two halves age differently. The DECISION half is a validation instrument:
 *  once the replay is trusted it agrees every time and this goes quiet. The
 *  EXECUTION half never settles — what your fills cost depends on your broker,
 *  your symbols and your order sizes — and the number it produces is meant to go
 *  straight into the spread setting on the form above.
 */
/** Bar sizes as a human says them. "1Min" is a wire value, not a sentence. */
const BAR_SIZE: Record<string, string> = {
  "1Min": "1-minute",
  "15Min": "15-minute",
  "1Hour": "1-hour",
  "1Day": "daily",
};

export default function FidelityPanel() {
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [mode, setMode] = useState("paper");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<FidelityReport | null>(null);
  // An explicit stretch, set by taking up the suggestion below. Null = use the
  // suggested stable stretch. Null = the strategy's whole trading life, which
  // the server works out from the journal.
  const [window, setWindow] = useState<{ start: string; end: string } | null>(null);

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
      setReport(
        await runFidelity({
          strategy_id: strategyId,
          mode,
          ...(window ? { window_start: window.start, window_end: window.end } : {}),
        }),
      );
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
    <div className="card">
      <div className="cache-head">
        <h3>Backtest fidelity — is the replay telling the truth?</h3>
        <span className="hint">Compare real trades against a backtest of the same period</span>
      </div>

      <p className="hint">
        Point the backtester at a stretch you have <strong>already traded</strong> and see whether it agrees. The entry,
        exit and safety-rail code is shared between the live engine and the backtester — it is one implementation, not
        two — so the two cannot disagree about strategy logic. Anything that does differ is therefore either the{" "}
        <strong>data</strong> the replay saw or the <strong>price</strong> it assumed, and those need opposite fixes.
      </p>

      <p className="hint">
        The period is not a choice: every comparison runs from the moment the strategy was{" "}
        <strong>switched on</strong> up to now, worked out from the journal. Replaying further back than the
        strategy existed counts every trade the backtest takes there as one it invented, which says nothing
        about the backtester.
      </p>

      <div className="grid-2">
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
      </div>

      {window && (
        <p className="hint">
          Comparing <strong>{fmtIsoDate(window.start)}</strong> to <strong>{fmtIsoDate(window.end)}</strong> — a
          stretch with no edits in it.{" "}
          <button type="button" className="btn-ghost" onClick={() => setWindow(null)}>
            Use the whole period instead
          </button>
        </p>
      )}
      <button type="button" onClick={run} disabled={busy || strategyId === null}>
        {busy ? "Comparing…" : "Run comparison"}
      </button>
      {error && <div className="error">{error}</div>}

      {report && d && x && (
        <>
          {/* WHICH BARS GRADED THESE TRADES. The live engine decides every 60
              seconds, so the replay's resolution is not a detail: a 50% match
              rate means something quite different when a coarser bar could not
              have seen the decision in the first place. The replay picks the
              finest resolution the window can afford and drops back when the
              bars aren't there, so this is read off the result rather than
              assumed. */}
          {report.timeframe && (
            <p className="hint">
              Graded on <strong>{BAR_SIZE[report.timeframe] ?? report.timeframe}</strong> bars.{" "}
              {report.timeframe === "1Min"
                ? "That matches the 60-second cycle the live engine decides on, so a mismatch below is a real disagreement rather than a gap between two bars."
                : "The live engine decides every 60 seconds, so a trade it made between two of these bars was never something the replay could see — read a lone mismatch with that in mind."}
            </p>
          )}

          {(report.bar_gaps?.length ?? 0) > 0 && (
            <p className="hint warn">
              <IconWarn className="icon-inline" /> <strong>The replay had days with no price data.</strong> Read the
              mismatches below with that in mind: a trade "the backtest missed" on a day it had no bars for is a gap in
              the cache, not a fault in the replay. Fill the gap and re-run before drawing conclusions.
            </p>
          )}

          {(report.config?.basket_changes_during_window ?? 0) > 0 && (
            <p className={report.config!.segmented ? "hint" : "hint warn"}>
              {!report.config!.segmented && <IconWarn className="icon-inline" />}{" "}
              <strong>
                The basket was edited {report.config!.basket_changes_during_window} time
                {report.config!.basket_changes_during_window === 1 ? "" : "s"} while these trades were being made.
              </strong>{" "}
              {report.config!.segmented ? (
                <>
                  Membership is tracked to the moment, not the day, and the window was cut at each edit — so every
                  stretch was replayed against the symbols that were actually in the basket at the time.
                </>
              ) : (
                <>
                  Trades from before an edit ran on a different list of symbols than the replay uses, so some
                  mismatches below are that rather than a fault in the backtester. Membership is tracked to the moment,
                  not the day — but a single replay can only use one list, and a change that later reverted wouldn't
                  show as a difference at all, which is why this is counted separately.
                </>
              )}
            </p>
          )}

          {report.config?.segmented && (
            <p className="hint">
              <strong>
                This comparison was split into {report.config.segments?.length ?? 0} stretches.
              </strong>{" "}
              <InfoTip k="fidelity_segments" /> The configuration changed while these trades were being made, so the
              window was cut at each change and every stretch was replayed with the settings that were live during it —
              rather than measuring all of them against today's.{" "}
              <strong>Each stretch starts with a fresh account and no open positions</strong>, which real trading did
              not: cash and held positions carried across those moments in life and cannot here. A stretch that begins
              with the full budget available has room the live account did not, so the replay leans towards taking more
              trades than reality across a cut.
              {(report.config.boundary_spanning_trades ?? 0) > 0 && (
                <>
                  {" "}
                  <strong>
                    {report.config.boundary_spanning_trades} trade
                    {report.config.boundary_spanning_trades === 1 ? " was" : "s were"} opened in one stretch and closed
                    in another.
                  </strong>{" "}
                  No stretch could reproduce that — the first stops watching at the cut, the second starts holding
                  nothing — so their entries still count above and their exits are left out of every exit number,
                  the same treatment a trade you closed by hand gets.
                </>
              )}
              {(report.config.segments_with_unknown_universe ?? 0) > 0 && (
                <>
                  {" "}
                  {report.config.segments_with_unknown_universe} of those stretches had no record of which symbols were
                  in play at the time, so today's list stood in for them. Read their mismatches with that in mind.
                </>
              )}
            </p>
          )}

          {report.config?.not_segmented_reason && (
            <p className="hint warn">
              <IconWarn className="icon-inline" /> <strong>This comparison was not split.</strong>{" "}
              {report.config.not_segmented_reason} <InfoTip k="fidelity_segments" />
            </p>
          )}

          {(report.config?.drift.length ?? 0) > 0 && (
            <p className="hint warn">
              <IconWarn className="icon-inline" />{" "}
              <strong>This strategy was edited after these trades were made.</strong> Every trade records the config
              version that produced it.{" "}
              {report.config!.segmented ? (
                <>
                  Each stretch above was replayed with its own settings, so the comparison is no longer measuring
                  today's against yesterday's — this is here to show you what moved.
                </>
              ) : (
                <>
                  The replay uses the strategy as it stands <em>today</em> — so this is measuring whether today's
                  settings reproduce yesterday's trades, which is a different and much less useful question.
                </>
              )}{" "}
              What changed:{" "}
              {report.config!.drift
                .slice(0, 6)
                .map((c) => `${c.field} ${JSON.stringify(c.then)} → ${JSON.stringify(c.now)}`)
                .join("; ")}
              {report.config!.drift.length > 6 ? `; …and ${report.config!.drift.length - 6} more` : ""}.
              {report.config!.versions_used > 1 && !report.config!.segmented && (
                <>
                  {" "}
                  These trades span <strong>{report.config!.versions_used} different config versions</strong>, so no
                  single replay can be faithful to all of them — not even one using an older config.
                </>
              )}
            </p>
          )}

          {d.backtest_trades === 0 && (
            <p className="hint warn">
              <IconWarn className="icon-inline" /> <strong>The backtest took no trades at all</strong> over this
              window, so nothing below is a comparison — every real trade is listed as "missed" because there was
              nothing to match it against.{" "}
              {report.backtest_no_trade_reason ? <>{report.backtest_no_trade_reason}</> : null}{" "}
              {d.missed_outside_universe > 0 && (
                <>
                  <strong>
                    {d.missed_outside_universe} of the {d.missed_by_backtest} traded symbols aren't in the universe the
                    replay was pointed at
                  </strong>{" "}
                  ({report.replayed_symbols.slice(0, 8).join(", ")}
                  {report.replayed_symbols.length > 8 ? ", …" : ""}). That normally means the strategy's universe was
                  changed after those trades were made, so today's settings are being compared against a different
                  strategy's history. Fix the comparison before reading anything into it.
                </>
              )}
            </p>
          )}

          {report.config?.stable_window && (
            <p className="hint">
              <strong>
                The longest stretch you didn't edit runs {fmtIsoDate(report.config.stable_window.start)} to{" "}
                {fmtIsoDate(report.config.stable_window.end)} ({report.config.stable_window.days} days,{" "}
                {report.config.stable_window.live_trades} trades).
              </strong>{" "}
              Comparing that measures the backtester; comparing a window you edited repeatedly mostly measures the
              edits.{" "}
              <button
                type="button"
                className="btn-ghost"
                onClick={() =>
                  setWindow({
                    start: report.config!.stable_window!.start,
                    end: report.config!.stable_window!.end,
                  })
                }
              >
                Use that window
              </button>
            </p>
          )}

          {(report.config?.changes?.length ?? 0) > 0 && (
            <details className="fold">
              <summary>
                <span className="hint">
                  {report.config!.changes!.length} configuration change
                  {report.config!.changes!.length === 1 ? "" : "s"} during this window — what moved, and when
                </span>
              </summary>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>When ({zoneAbbr()})</th>
                      <th>What changed</th>
                      <th>Trades after it</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.config!.changes!.map((c) => (
                      <tr key={c.at}>
                        <td>{fmtIsoDate(c.at)}</td>
                        <td className="hint">
                          {c.changed.length
                            ? c.changed
                                .map((d) => `${d.field} ${JSON.stringify(d.then)} → ${JSON.stringify(d.now)}`)
                                .join("; ")
                            : "basket membership"}
                        </td>
                        <td>{c.live_trades_after}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}

          {report.window?.clamped_to_first_trade && (
            <p className="hint">
              <strong>
                Compared {fmtIsoDate(report.window.start)} to {fmtIsoDate(report.window.end)} ({report.window.days}{" "}
                days).
              </strong>{" "}
              That is this strategy's whole {report.mode} life — its first activity was{" "}
              {fmtIsoDate(report.window.first_trade_at)}. Anything earlier would be replaying a period it didn't
              exist in.
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

          {report.quiet && (
            <p className="hint">
              <strong>Nothing traded on either side.</strong> The strategy has been live
              {report.activated_at ? ` since ${fmtIsoDate(report.activated_at)} ${fmtHm(report.activated_at)}` : ""},
              and neither it nor the replay of it took a trade in that time. That is an agreement, not a
              failure — the two are behaving identically. Give it longer, or loosen the entry rules if you
              expected activity by now.
            </p>
          )}

          {report.timeline.length > 0 && (
            <>
              <h4>What happened, in order</h4>
              <p className="hint">
                Every buy, every sell, and every edit in between — at the moment each happened, not grouped by day.
                This is the part that names a bug: "the replay sold three hours later" is something to go and fix,
                where a percentage is not. An edit in the middle explains the rows after it. Times are shown in{" "}
                <strong>{zoneAbbr()}</strong> — the whole point of the column is comparing when things happened, so
                which clock it is has to be on the page. Change it under Settings → Display.
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>When ({zoneAbbr()})</th>
                      <th>Symbol</th>
                      <th>What</th>
                      <th>Verdict</th>
                      <th>Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.timeline.map((r, i) => (
                      <tr
                        key={`${r.at ?? r.day}-${r.symbol}-${i}`}
                        className={r.kind === "edit" || r.kind === "activated" ? "cmp-win" : ""}
                      >
                        {/* `at` is an instant and moves with the display zone;
                            `day` is already a bucketed calendar day and must
                            not be converted, so it is printed as it came. */}
                        <td className="tabular">
                          {r.at ? fmtIsoDate(r.at) : r.day}
                          {r.at && r.at.includes("T") ? <span className="hint"> {fmtHm(r.at)}</span> : null}
                        </td>
                        <td>{r.symbol || "—"}</td>
                        <td>{r.action}</td>
                        <td
                          className={
                            r.verdict === "match" ? "up" : r.kind === "edit" || r.kind === "activated" ? "" :
                            r.verdict.includes("not compared") || r.verdict.includes("nothing") ? "" : "down"
                          }
                        >
                          {r.verdict}
                        </td>
                        <td className="hint">{r.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
