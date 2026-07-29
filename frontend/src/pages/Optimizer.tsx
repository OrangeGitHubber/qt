import { FormEvent, useEffect, useRef, useState } from "react";
import {
  Basket,
  createStrategy,
  getBaskets,
  getOptimizerStatus,
  getStrategies,
  OptimizerNeighbourPoint,
  OptimizerResult,
  OptimizerStatus,
  startOptimizer,
  StrategyRow,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";
import SymbolPicker from "../components/SymbolPicker";
import { IconWarn } from "../components/icons";

// Human labels for the four searched knobs (keys match the backend PARAM_SPACE).
const KNOB_LABELS: Record<string, string> = {
  min_day_gain_pct: "Min gain today (%)",
  trailing_stop_pct: "Trailing stop (%)",
  stop_loss_pct: "Stop-loss (%)",
  take_profit_pct: "Take-profit (%)",
};
const KNOB_ORDER = ["min_day_gain_pct", "trailing_stop_pct", "stop_loss_pct", "take_profit_pct"];

function pct(v: number | null | undefined): string {
  return v != null ? `${v}%` : "—";
}

// Spell out EXACTLY which symbols the search will run on, mirroring the backend's
// _resolve_symbols rules, so there's no mystery about the universe. `warn` = the
// surprising / fallback cases the user should notice.
function universeExplain(
  strategy: StrategyRow | undefined,
  symbols: string[],
  baskets: Basket[],
  scannerReplay: boolean,
  replayTopN: number,
): { warn: boolean; text: string } | null {
  if (!strategy) return null;
  if (scannerReplay) {
    return {
      warn: false,
      text: `the historical scanner risers — each past day, only that day's top ${replayTopN} movers are eligible to enter, read offline from the bar cache. This tunes the strategy against its REAL, day-varying universe (needs a completed sweep in Settings → Historical bar cache).`,
    };
  }
  if (symbols.length > 0) {
    const shown = symbols.slice(0, 12).join(", ");
    return { warn: false, text: `your ${symbols.length} picked symbol${symbols.length === 1 ? "" : "s"}: ${shown}${symbols.length > 12 ? " …" : ""}` };
  }
  if (strategy.universe === "basket") {
    const b = baskets.find((x) => x.id === strategy.basket_id);
    return b
      ? { warn: false, text: `the members of basket “${b.name}” (${b.count} symbol${b.count === 1 ? "" : "s"})` }
      : { warn: true, text: "this basket strategy has no basket/members — pick symbols above first" };
  }
  if (strategy.universe === "custom") {
    const n = strategy.symbols?.length ?? 0;
    return n > 0
      ? { warn: false, text: `this strategy's own symbol list (${n})` }
      : { warn: true, text: "this strategy's symbol list is empty — pick symbols above" };
  }
  // scanner / watchlist / both
  return {
    warn: true,
    text: `your ${strategy.asset_class} watchlist — a scanner strategy can't replay the historical daily risers, so the search validates on your watchlist. Pick specific symbols above to test something else.`,
  };
}

function Stat({ label, value, tone, sub }: { label: string; value: string; tone?: "up" | "down"; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
      {sub && <div className="stat-label">{sub}</div>}
    </div>
  );
}

// One knob's plateau strip: the winner plus its immediate neighbours. If the
// neighbours score similarly, the winner sits on a plateau (trustworthy); if the
// winner spikes alone, it's probably noise.
function Plateau({ knob, points }: { knob: string; points: OptimizerNeighbourPoint[] }) {
  const scores = points.map((p) => p.score).filter((s): s is number => s != null);
  const max = scores.length ? Math.max(...scores) : 0;
  const min = scores.length ? Math.min(...scores) : 0;
  const span = max - min || 1;
  return (
    <div className="plateau">
      <div className="field-cap">{KNOB_LABELS[knob] ?? knob}</div>
      <div className="plateau-bars">
        {points.map((p, i) => {
          const h = p.score == null ? 4 : 6 + ((p.score - min) / span) * 46;
          return (
            <div key={i} className={`plateau-bar ${p.is_best ? "best" : ""}`}>
              <div className="plateau-fill" style={{ height: `${h}px` }} title={p.score == null ? "too few trades" : `score ${p.score}`} />
              <div className="plateau-val">{p.value}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function Optimizer() {
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [baskets, setBaskets] = useState<Basket[]>([]);
  const [symbols, setSymbols] = useState<string[]>([]);
  const [scannerReplay, setScannerReplay] = useState(false);
  const [replayTopN, setReplayTopN] = useState(10);
  const [days, setDays] = useState(180);
  const [timeframe, setTimeframe] = useState("1Day");
  const [iterations, setIterations] = useState(40);
  const [cash, setCash] = useState(5000);
  const [spread, setSpread] = useState(0.1);

  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<OptimizerStatus | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const strategy = strategies.find((s) => s.id === strategyId);
  const stratNeedsIntraday = !!(
    strategy?.params?.entry?.require_above_vwap ||
    (strategy?.params?.entry?.entry_window_start && strategy?.params?.entry?.entry_window_end)
  );
  const running = status?.running ?? false;
  const result: OptimizerResult | null = status && !status.running ? status.result : null;

  useEffect(() => {
    getStrategies().then((rows) => {
      setStrategies(rows);
      if (rows.length && strategyId === null) setStrategyId(rows[0].id);
    });
    getBaskets().then(setBaskets).catch(() => setBaskets([]));
    // Pick up a search already in flight (e.g. after a page switch).
    getOptimizerStatus()
      .then((s) => {
        setStatus(s);
        if (s.running) startPolling();
      })
      .catch(() => {});
    return () => stopPolling();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Default the cash to the selected strategy's sleeve, like the backtest page.
  useEffect(() => {
    const strat = strategies.find((s) => s.id === strategyId);
    if (strat) {
      setCash(Math.max(strat.sleeve_usd || 5000, 100));
      setSymbols(strat.universe === "custom" ? (strat.symbols ?? []).slice(0, 50) : []);
      // A scanner strategy defaults to replaying its REAL universe (each day's
      // top-N risers) rather than a stand-in watchlist — the whole point of the
      // scanner-replay optimizer. Any other universe keeps its fixed symbol set.
      const isScanner = strat.universe === "scanner";
      setScannerReplay(isScanner);
      if (isScanner) setReplayTopN(strat.top_n || 10);
      // VWAP and an entry-time window are INTRADAY rules — they can't be evaluated
      // on daily bars (every entry gets rejected, so the whole search returns 0
      // trades). Default such strategies to 15-minute bars so the search is valid;
      // otherwise daily (fast) is the sensible default.
      const e = strat.params?.entry;
      const needsIntraday = !!(e?.require_above_vwap || (e?.entry_window_start && e?.entry_window_end));
      setTimeframe(needsIntraday ? "15Min" : "1Day");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId, strategies]);

  function stopPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  function startPolling() {
    stopPolling();
    const tick = async () => {
      try {
        const s = await getOptimizerStatus();
        setStatus(s);
        if (!s.running) stopPolling();
      } catch (err) {
        setError((err as Error).message);
        stopPolling();
      }
    };
    tick();
    pollRef.current = setInterval(tick, 1500);
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    if (strategyId === null) return;
    setError(null);
    setSaveMsg(null);
    setStatus(null);
    try {
      await startOptimizer({
        strategy_id: strategyId,
        symbols,
        scanner_replay: scannerReplay,
        replay_top_n: replayTopN,
        days,
        timeframe,
        iterations,
        starting_cash: cash,
        spread_pct: spread,
      });
      startPolling();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function saveDraft() {
    if (!result || !strategy) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      // Mirror the tested strategy's config, swapping in the searched params. The
      // create endpoint always makes it DISABLED — a hypothesis to review, tweak,
      // and walk up the shadow -> paper ladder. Nothing is enabled automatically.
      const draft = await createStrategy({
        name: `${strategy.name} (search draft)`,
        asset_class: strategy.asset_class,
        universe: strategy.universe,
        basket_id: strategy.basket_id,
        symbols: strategy.symbols,
        rank_by: strategy.rank_by,
        top_n: strategy.top_n,
        rank_enabled: strategy.rank_enabled,
        preset: "custom",
        params: result.best_draft_params,
        sizing_usd: strategy.sizing_usd,
        sleeve_usd: strategy.sleeve_usd,
        max_positions: strategy.max_positions,
        swing_mode: strategy.swing_mode,
        ignore_regime: strategy.ignore_regime,
      });
      setSaveMsg(
        `Saved "${draft.name}" as a DISABLED draft. Open the Strategies tab to review it, then run it in shadow mode before anything else.`,
      );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const progressPct =
    status && status.combos_total > 0 ? Math.min(100, Math.round((status.combos_done / status.combos_total) * 100)) : 0;

  const hb = result?.hold_benchmark_comparison;

  return (
    <>
      <div className="toolbar">
        <h2>
          Strategy optimizer <InfoTip k="parameter_search" />
        </h2>
      </div>

      <div className="card">
        <p className="hint">
          A <strong>parameter search</strong> — not "AI". It runs the same backtester across many settings for a
          momentum strategy (min gain, trailing stop, stop-loss, take-profit) so you find configs that actually held up,
          instead of guessing numbers. Every guard here exists to fight <strong>overfitting</strong>{" "}
          <InfoTip k="overfitting" />:
        </p>
        <ul className="hint">
          <li>
            The search only sees the <strong>first ~70%</strong> of the history (in-sample). Every winner is then re-run
            on the <strong>final ~30% it never saw</strong> (out-of-sample) — and only that number is treated as real.
          </li>
          <li>
            It always tells you <strong>how many combinations were tested</strong> — a winner out of 12 tries means far
            less than a winner out of 2,000.
          </li>
          <li>
            It shows the <strong>neighbourhood</strong> around the winner: a good setting sits on a plateau, a lone spike
            is noise.
          </li>
          <li>
            It compares the winner to simply <strong>buying and holding</strong> the same symbols.
          </li>
        </ul>
        <p className="hint">
          The result is a <strong>hypothesis</strong>: an editable draft strategy, born <strong>disabled</strong>, that
          still has to earn its way up shadow → paper. Past results predict nothing.
        </p>

        <form className="backtest-form" onSubmit={start}>
          <div className="filter-grid">
            <label>
              <span className="field-cap">Strategy to tune</span>
              <select value={strategyId ?? ""} onChange={(e) => setStrategyId(Number(e.target.value))} required>
                {strategies.length === 0 && <option value="">— create a strategy first —</option>}
                {strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.asset_class})
                  </option>
                ))}
              </select>
            </label>
            {/* The universe comes from the STRATEGY, not the optimizer. A scanner
                strategy is optimized by replaying its real universe (the day's
                top-N risers), so we show the risers knob; every other universe is
                a fixed pool you can optionally narrow to a validation subset. */}
            {scannerReplay ? (
              <label>
                <span className="field-cap">
                  Risers per day (top N) <InfoTip k="replay_top_n" />
                </span>
                <NumberField min={1} max={100} step={1} value={replayTopN} onChange={setReplayTopN} />
              </label>
            ) : (
              <div className="field">
                <span className="field-cap">
                  Symbols to validate across (optional — defaults to the strategy's own universe){" "}
                </span>
                <SymbolPicker assetClass={strategy?.asset_class} value={symbols} onChange={setSymbols} multi />
              </div>
            )}
          </div>
          {(() => {
            const u = universeExplain(strategy, symbols, baskets, scannerReplay, replayTopN);
            if (!u) return null;
            return (
              <p className={`hint ${u.warn ? "warn" : ""}`}>
                This search will test on: <strong>{u.text}</strong>
              </p>
            );
          })()}
          {!scannerReplay && (
            <p className="hint">
              Validate across <strong>several symbols or the basket</strong>, never one ticker — a setting that fits a
              single name's history rarely survives contact with another.
            </p>
          )}
          <div className="filter-grid">
            <label>
              <span className="field-cap">
                History (days) <InfoTip k="history_days" />
              </span>
              <NumberField min={30} max={730} step={1} value={days} onChange={setDays} />
            </label>
            <label>
              <span className="field-cap">
                Bar size <InfoTip k="bar" />
              </span>
              <select
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                disabled={scannerReplay}
                title={scannerReplay ? "Ignored in scanner replay — the cache decides (15-min if swept, else daily)" : undefined}
              >
                <option value="1Day">1 day (fast — recommended for a search)</option>
                <option value="1Hour">1 hour (slower)</option>
                <option value="15Min">15 minutes (slowest, precise)</option>
              </select>
              {stratNeedsIntraday && !scannerReplay && (
                <span className="field-help">
                  This strategy uses VWAP / an entry window (intraday rules), so daily bars would reject every entry —
                  defaulted to intraday. Switch to 1 day only if you also turn those rules off.
                </span>
              )}
            </label>
            <label>
              <span className="field-cap">Combinations to try</span>
              <NumberField min={5} max={200} step={1} value={iterations} onChange={setIterations} />
            </label>
            <label>
              <span className="field-cap">
                Spread cost per side (%) <InfoTip k="spread_cost" />
              </span>
              <NumberField min={0} max={2} step={0.05} value={spread} onChange={setSpread} />
            </label>
          </div>
          {strategy && (
            <p className="hint">
              Account size is this strategy's <strong>${strategy.sleeve_usd.toLocaleString()}</strong> sleeve (with $
              {strategy.sizing_usd.toLocaleString()} per trade) — a single-strategy search can't deploy more than its
              sleeve, so there's no separate "starting cash" to set.
            </p>
          )}
          <p className="hint">
            More combinations searches harder but takes longer (each is a full backtest) — and, counter-intuitively,
            trying more settings makes a good in-sample score <em>easier to hit by luck</em>, which is exactly why the
            out-of-sample check matters.
          </p>
          {error && <div className="error">{error}</div>}
          <button disabled={running || strategyId === null}>
            {running ? "Searching…" : "Run parameter search"}
          </button>
        </form>
      </div>

      {running && status && (
        <div className="card">
          <h3>Searching…</h3>
          <p className="hint">
            {status.phase === "downloading bars"
              ? "Downloading historical bars…"
              : `Tested ${status.combos_done} of ~${status.combos_total} combinations on the in-sample history.`}
          </p>
          <div className="progress-track" aria-label="search progress">
            <div className="progress-fill" style={{ width: `${status.phase === "downloading bars" ? 5 : progressPct}%` }} />
          </div>
        </div>
      )}

      {status?.error && !running && <div className="card error">Search failed: {status.error}</div>}

      {result && result.best && (
        <>
          <div className="card">
            <h3>
              {result.strategy_name} · tested{" "}
              <strong>{result.tested_combinations.toLocaleString()} combinations</strong> · {result.symbols.length}{" "}
              symbol{result.symbols.length === 1 ? "" : "s"} · last {result.days} days ({result.timeframe})
            </h3>
            {result.scanner_replay ? (
              <p className="hint">
                Tested by <strong>scanner replay</strong>: each day's top {result.replay_top_n} risers over{" "}
                {result.days_replayed ?? "—"} days — a universe of <strong>{result.universe_size ?? result.symbols.length}</strong>{" "}
                distinct names, using {result.replay_intraday ? "15-minute (intraday)" : "daily"} bars from the cache.
              </p>
            ) : (
              <p className="hint">
                Tested on: <strong>{result.symbols.join(", ")}</strong>
              </p>
            )}
            {result.no_trade_reason && (
              <p className="hint warn" style={{ marginTop: "0.5rem" }}>
                <IconWarn className="icon-inline" /> <strong>No configuration traded — so this isn't a verdict on the strategy, it's a setup issue.</strong>{" "}
                {result.no_trade_reason} Fix that and re-run; until something trades, there's nothing to optimize.
              </p>
            )}
            <p className="hint">
              <strong>Out-of-sample is the real result.</strong> The search optimized on{" "}
              {result.in_sample_window.days} days ({result.in_sample_window.start.slice(0, 10)} →{" "}
              {result.in_sample_window.end.slice(0, 10)}) and this headline is measured on the{" "}
              {result.out_of_sample_window.days} days <em>after</em> that, which the search never saw (
              {result.out_of_sample_window.start.slice(0, 10)} → {result.out_of_sample_window.end.slice(0, 10)}). A
              backtest can only kill bad ideas cheaply, never prove good ones.
            </p>

            <div className="stats">
              <Stat
                label="Out-of-sample return (real)"
                value={pct(result.best.out_of_sample?.net_pnl_pct)}
                tone={(result.best.out_of_sample?.net_pnl_pct ?? 0) >= 0 ? "up" : "down"}
                sub="on data the search never saw"
              />
              <Stat
                label="In-sample return"
                value={pct(result.best.in_sample?.net_pnl_pct)}
                sub="search only — NOT proof"
              />
              <Stat label="Out-of-sample trades" value={String(result.best.out_of_sample?.trades ?? "—")} />
              <Stat label="Out-of-sample win rate" value={pct(result.best.out_of_sample?.win_rate)} />
              <Stat
                label="Out-of-sample drawdown"
                value={pct(result.best.out_of_sample?.max_drawdown_pct)}
                tone={(result.best.out_of_sample?.max_drawdown_pct ?? 0) > 10 ? "down" : undefined}
              />
            </div>

            {(result.best.in_sample?.net_pnl_pct ?? 0) > (result.best.out_of_sample?.net_pnl_pct ?? 0) + 2 && (
              <p className="hint warn">
                <IconWarn className="icon-inline" /> The in-sample return ({pct(result.best.in_sample?.net_pnl_pct)}) is noticeably higher than the
                out-of-sample return ({pct(result.best.out_of_sample?.net_pnl_pct)}). That gap is the fingerprint of
                overfitting — the settings fit the searched history better than reality. Trust the lower number.
              </p>
            )}

            <div className="deployment">
              <h4>Winning settings (the draft)</h4>
              <div className="stats">
                {KNOB_ORDER.map((k) => (
                  <Stat
                    key={k}
                    label={KNOB_LABELS[k]}
                    value={String((result.best!.params as unknown as Record<string, number>)[k])}
                  />
                ))}
              </div>
            </div>

            {hb && (
              <div className="verdicts">
                <p className="verdict">
                  {hb.beat_hold == null
                    ? "Not enough out-of-sample data to compare against buy-and-hold."
                    : hb.beat_hold
                      ? `Beat simply buying & holding ${hb.hold_label ?? "the symbols"} out-of-sample (${pct(
                          hb.strategy_out_of_sample_pct,
                        )} vs ${pct(hb.hold_out_of_sample_pct)}) — the trading added value here.`
                      : `Simply buying & holding ${hb.hold_label ?? "the symbols"} beat the search's best config out-of-sample (${pct(
                          hb.hold_out_of_sample_pct,
                        )} vs ${pct(hb.strategy_out_of_sample_pct)}) — the trading destroyed value. You'd be better off just holding.`}
                </p>
              </div>
            )}

            {result.warnings.map((w, i) => (
              <p key={i} className="hint warn">
                <IconWarn className="icon-inline" /> {w}
              </p>
            ))}

            <div style={{ marginTop: 12 }}>
              <button type="button" onClick={saveDraft} disabled={saving}>
                {saving ? "Saving…" : "Save as draft strategy"}
              </button>
              <p className="hint">
                Creates a new <strong>disabled</strong> strategy from these settings, mirroring "{strategy?.name}". It
                never trades until you review it and deliberately move it up the autonomy ladder.
              </p>
              {saveMsg && <div className="note-ok">{saveMsg}</div>}
            </div>
          </div>

          <div className="card">
            <h3>
              Plateau check — is the winner on solid ground?{" "}
              <span className="hint">(scores of the values either side of the winner, in-sample)</span>
            </h3>
            <p className="hint">
              For each knob, the bar in the middle-ish is the winning value; the bars around it are its neighbours. When
              neighbours score <strong>similarly</strong>, the setting is a dependable plateau. When the winner{" "}
              <strong>towers alone</strong> over bad neighbours, it's likely a fluke that won't repeat.
            </p>
            <div className="plateau-grid">
              {KNOB_ORDER.filter((k) => result.neighbourhood[k]).map((k) => (
                <Plateau key={k} knob={k} points={result.neighbourhood[k]} />
              ))}
            </div>
          </div>

          <div className="card">
            <h3>
              Top configurations{" "}
              <span className="hint">(in-sample rank — watch how out-of-sample often disagrees)</span>
            </h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Min gain</th>
                    <th>Trail</th>
                    <th>Stop</th>
                    <th>Take-profit</th>
                    <th>In-sample</th>
                    <th>Out-of-sample (real)</th>
                    <th>OOS trades</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.map((r) => (
                    <tr key={r.rank} className={r.is_best ? "cmp-win" : ""}>
                      <td>{r.rank}</td>
                      <td>{r.params.min_day_gain_pct}</td>
                      <td>{r.params.trailing_stop_pct}</td>
                      <td>{r.params.stop_loss_pct}</td>
                      <td>{r.params.take_profit_pct}</td>
                      <td>{pct(r.in_sample?.net_pnl_pct)}</td>
                      <td className={(r.out_of_sample?.net_pnl_pct ?? 0) >= 0 ? "up" : "down"}>
                        {pct(r.out_of_sample?.net_pnl_pct)}
                      </td>
                      <td>{r.out_of_sample?.trades ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  );
}
