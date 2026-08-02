import { FormEvent, useEffect, useRef, useState } from "react";
import {
  Basket,
  createStrategy,
  getBasketSweepStatus,
  getBaskets,
  getOptimizerStatus,
  getStrategies,
  OptimizerNeighbourPoint,
  OptimizerResult,
  OptimizerStatus,
  startBasketSweep,
  startOptimizer,
  StrategyRow,
  SweepRow,
  SweepStatus,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";
import { IconWarn } from "../components/icons";
import RunStatus from "../components/RunStatus";
import UniverseChips, { resolveUniverse } from "../components/UniverseChips";
import { consumeNav } from "../lib/nav";

// Human labels for the four searched knobs (keys match the backend PARAM_SPACE).
const KNOB_LABELS: Record<string, string> = {
  min_day_gain_pct: "Min gain today (%)",
  trailing_stop_pct: "Trailing stop (%)",
  stop_loss_pct: "Stop-loss (%)",
  take_profit_pct: "Take-profit (%)",
  rsi_max: "Max RSI (entry)",
  rsi_min: "Min RSI (entry)",
  exit_rsi_above: "Sell if RSI above",
  macd_slow: "MACD speed (slow EMA — lower = faster)",
  atr_stop_mult: "ATR stop (× ATR)",
};
// RSI/MACD knobs are searched only when the strategy uses that signal; the
// plateau grid filters by presence in the neighbourhood, so they simply don't
// appear otherwise.
const KNOB_ORDER = [
  "min_day_gain_pct",
  "trailing_stop_pct",
  "stop_loss_pct",
  "take_profit_pct",
  "rsi_max",
  "rsi_min",
  "exit_rsi_above",
  "atr_stop_mult",
  "macd_slow",
];

function pct(v: number | null | undefined): string {
  return v != null ? `${v}%` : "—";
}

// --- Which bar size this strategy's rules demand (mirrors the backend guards in
// qt/api/backtest.py: _uses_daily_only_signals / _has_price_triggered_exit /
// _mixed_resolution, and the same helpers on the Backtest page). ---

// VWAP and an entry-time window can only be evaluated on intraday bars.
function wantsIntradayRules(s: StrategyRow | undefined): boolean {
  const e = s?.params?.entry;
  return !!(e?.require_above_vwap || (e?.entry_window_start && e?.entry_window_end));
}

// MACD/RSI/ATR are DAILY signals — the live engine reads them off completed daily closes.
function wantsDailySignals(s: StrategyRow | undefined): boolean {
  if (!s || wantsIntradayRules(s)) return false;
  const e = s.params?.entry;
  const x = s.params?.exit;
  return !!(
    e?.require_macd_bullish ||
    x?.exit_on_macd_bearish ||
    (e?.rsi_min ?? 0) > 0 ||
    (e?.rsi_max ?? 0) > 0 ||
    (x?.exit_rsi_above ?? 0) > 0 ||
    // ATR belongs with them: computed from DAILY bars live, and meaningless on
    // an intraday series (14 periods of 15-minute bars is 3.5 hours of range).
    (s.params?.atr?.stop_mult ?? 0) > 0 ||
    (s.params?.atr?.risk_usd ?? 0) > 0
  );
}

// A price-triggered exit — stop-loss, trailing stop or take-profit. Three of the
// four knobs the search tunes ARE these, and a once-a-day replay checks them only
// at the close, so on daily bars a tight stop looks nearly free.
function hasPriceExit(s: StrategyRow | undefined): boolean {
  const x = s?.params?.exit;
  return (
    (x?.stop_loss_pct ?? 0) > 0 ||
    (x?.trailing_stop_pct ?? 0) > 0 ||
    (x?.take_profit_pct ?? 0) > 0 ||
    // The ATR stop counts — it replaces the fixed percentage rather than adding
    // to it, so the fixed one can legitimately be zero.
    (s?.params?.atr?.stop_mult ?? 0) > 0
  );
}

// MIXED RESOLUTION: daily signals AND a price-triggered exit. One bar stream
// can't serve both, so the search replays 15-minute bars while MACD/RSI still
// come from completed daily closes.
function isMixedResolution(s: StrategyRow | undefined): boolean {
  return wantsDailySignals(s) && hasPriceExit(s);
}

// The bar size a strategy is actually searched on, so the form never offers
// something the backend will override.
function deriveSearchTimeframe(s: StrategyRow | undefined): "1Day" | "15Min" {
  if (isMixedResolution(s)) return "15Min";
  if (wantsDailySignals(s)) return "1Day";
  return wantsIntradayRules(s) ? "15Min" : "1Day";
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

// The label already carries the unit ("Min gain today (%)"), so the value is
// shown bare — except where 0 means the rule is switched off entirely, which
// "0" reads as a setting rather than an absence.
const OFF_AT_ZERO = new Set(["take_profit_pct", "rsi_max", "rsi_min", "exit_rsi_above"]);

function fmtKnob(knob: string, v: number | null | undefined): string {
  if (v == null) return "not set";
  return v === 0 && OFF_AT_ZERO.has(knob) ? "off" : String(v);
}

// One knob, before -> after. Unchanged knobs are shown too rather than hidden:
// "the search agreed with what you already had" is a real result, and dropping
// the row would make it look like the knob wasn't searched at all.
function KnobDiff({
  knob,
  from,
  to,
  inGrid,
}: {
  knob: string;
  from: number | null | undefined;
  to: number;
  inGrid: boolean;
}) {
  const label = KNOB_LABELS[knob] ?? knob;
  const changed = from == null || Math.abs(from - to) > 1e-9;
  return (
    <div className={`knob-diff${changed ? "" : " knob-same"}`}>
      <div className="knob-diff-label">{label}</div>
      <div
        className="knob-diff-values"
        aria-label={changed ? `${label}: ${fmtKnob(knob, from)} changes to ${fmtKnob(knob, to)}` : `${label}: unchanged at ${fmtKnob(knob, to)}`}
      >
        {changed ? (
          <>
            <span className="knob-from">{fmtKnob(knob, from)}</span>
            <span className="knob-arrow" aria-hidden="true">
              →
            </span>
            <span className="knob-to knob-changed">{fmtKnob(knob, to)}</span>
          </>
        ) : (
          <>
            <span className="knob-to">{fmtKnob(knob, to)}</span>
            <span className="knob-unchanged">unchanged</span>
          </>
        )}
      </div>
      {from != null && !inGrid && (
        <div className="knob-diff-note hint">
          Your {fmtKnob(knob, from)} isn't one of the values this search tries, so it was never tested directly.
        </div>
      )}
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
  const [days, setDays] = useState(180);
  const [timeframe, setTimeframe] = useState("1Day");
  const [iterations, setIterations] = useState(40);
  const [cash, setCash] = useState(5000);
  const [spread, setSpread] = useState(0.1);
  const [relStep, setRelStep] = useState(0.15);

  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<OptimizerStatus | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Basket sweep: the same search across EVERY basket, ranked by out-of-sample
  // margin over SPY. Own status/poll so it can't tangle with the single search.
  const [sweepStatus, setSweepStatus] = useState<SweepStatus | null>(null);
  const [sweepError, setSweepError] = useState<string | null>(null);
  const [sweepDays, setSweepDays] = useState(365);
  const [sweepIterations, setSweepIterations] = useState(60);
  const [sweepSaved, setSweepSaved] = useState<Record<number, string>>({}); // basket_id → draft name
  const sweepPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const strategy = strategies.find((s) => s.id === strategyId);
  const stratNeedsIntraday = wantsIntradayRules(strategy);
  // MACD/RSI are daily signals — an intraday search computes them off intraday
  // closes, which whipsaws and won't match live. But a strategy that ALSO has a
  // stop runs MIXED: 15-minute replay, signals still from daily closes. Only a
  // daily-signal strategy with nothing price-triggered stays locked to 1 Day.
  const stratMixed = isMixedResolution(strategy);
  const stratLockedDaily = wantsDailySignals(strategy) && !stratMixed;
  const running = status?.running ?? false;
  const result: OptimizerResult | null = status && !status.running ? status.result : null;
  const sweepRunning = sweepStatus?.running ?? false;
  const sweepResult = sweepStatus && !sweepStatus.running ? sweepStatus.result : null;
  // RSI/MACD knobs are only searched when the strategy uses that signal — show
  // those result columns only when they were actually part of the search.
  const showRsiMax = !!result && result.results.some((r) => (r.params.rsi_max ?? 0) > 0);
  const showRsiMin = !!result && result.results.some((r) => (r.params.rsi_min ?? 0) > 0);
  const showRsiExit = !!result && result.results.some((r) => (r.params.exit_rsi_above ?? 0) > 0);
  const showMacd = !!result && result.results.some((r) => (r.params.macd_slow ?? 0) > 0);
  // The stop columns are mutually exclusive by construction: when the strategy
  // uses an ATR stop, the fixed stop-loss is inert and isn't searched at all, so
  // showing an always-empty "Stop" column would read as "the search found
  // nothing" rather than "this knob doesn't apply here".
  const showAtrStop = !!result && result.results.some((r) => (r.params.atr_stop_mult ?? 0) > 0);
  const showStopLoss = !!result && result.results.some((r) => (r.params.stop_loss_pct ?? 0) > 0);

  useEffect(() => {
    // A strategy row's "Optimize" button jumps here with that strategy in tow.
    const pre = consumeNav()?.strategyId ?? null;
    getStrategies()
      .then((rows) => {
        setStrategies(rows);
        if (rows.length && strategyId === null)
          setStrategyId(pre !== null && rows.some((r) => r.id === pre) ? pre : rows[0].id);
      })
      // Silent here meant a permanently dead Start button — say so instead.
      .catch((e: Error) => setError(`Couldn't load your strategies: ${e.message}`));
    getBaskets().then(setBaskets).catch(() => setBaskets([]));
    // Pick up a search already in flight (e.g. after a page switch).
    getOptimizerStatus()
      .then((s) => {
        setStatus(s);
        if (s.running) startPolling();
      })
      .catch(() => {});
    getBasketSweepStatus()
      .then((s) => {
        setSweepStatus(s);
        if (s.running) startSweepPolling();
      })
      .catch(() => {});
    return () => {
      stopPolling();
      stopSweepPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Default the cash to the selected strategy's sleeve, like the backtest page.
  useEffect(() => {
    const strat = strategies.find((s) => s.id === strategyId);
    if (strat) {
      setCash(Math.max(strat.sleeve_usd || 5000, 100));
      // No universe state to seed: it is derived from the strategy on every
      // render (see `uni`) and resolved again server-side, so there is nothing
      // here that can fall out of step with the strategy you picked.
      // The bar size follows the strategy's own rules: VWAP / an entry-time window
      // are INTRADAY (on daily bars every entry is rejected and the search returns
      // 0 trades); MACD/RSI with a stop is MIXED (15-minute replay, daily signals);
      // otherwise daily (fast) is the sensible default.
      setTimeframe(deriveSearchTimeframe(strat));
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

  function stopSweepPolling() {
    if (sweepPollRef.current) {
      clearInterval(sweepPollRef.current);
      sweepPollRef.current = null;
    }
  }

  function startSweepPolling() {
    stopSweepPolling();
    const tick = async () => {
      try {
        const s = await getBasketSweepStatus();
        setSweepStatus(s);
        if (!s.running) stopSweepPolling();
      } catch (err) {
        setSweepError((err as Error).message);
        stopSweepPolling();
      }
    };
    tick();
    sweepPollRef.current = setInterval(tick, 1500);
  }

  async function runSweep() {
    setSweepError(null);
    setSweepSaved({});
    setSweepStatus(null);
    try {
      await startBasketSweep({ days: sweepDays, iterations: sweepIterations });
      startSweepPolling();
    } catch (err) {
      setSweepError((err as Error).message);
    }
  }

  async function saveSweepDraft(row: SweepRow) {
    const sizing = sweepStatus?.result?.template_sizing;
    try {
      // Mirror what was actually tested: the whole basket eligible (top_n = its
      // size, so ranking doesn't gate) with the sweep's template sizing. Created
      // DISABLED — a hypothesis to review and walk up the shadow → paper ladder.
      const draft = await createStrategy({
        name: `${row.basket_name} (sweep draft)`,
        asset_class: "stock",
        universe: "basket",
        basket_id: row.basket_id,
        symbols: [],
        rank_by: "momentum_today",
        top_n: Math.min(row.symbols.length, 50),
        preset: "custom",
        params: row.best_draft_params,
        sizing_usd: sizing?.sizing_usd ?? 1000,
        sleeve_usd: sizing?.sleeve_usd ?? 5000,
        max_positions: sizing?.max_positions ?? 5,
        swing_mode: true,
        ignore_regime: false,
      });
      setSweepSaved((m) => ({ ...m, [row.basket_id]: draft.name }));
    } catch (err) {
      setSweepError((err as Error).message);
    }
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    if (strategyId === null) {
      setError("No strategy is selected — pick one above, or create one on the Strategies tab.");
      return;
    }
    setError(null);
    setSaveMsg(null);
    setStatus(null);
    try {
      await startOptimizer({
        strategy_id: strategyId,
        symbols: [],  // the strategy's universe is resolved on the server
        // Both derived server-side from the strategy now; sent only so an older
        // backend still behaves. The server ignores them.
        scanner_replay: uni.scannerReplay,
        replay_top_n: strategy?.top_n ?? 10,
        days,
        timeframe,
        iterations,
        relative_step: relStep,
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

  // Read-only, from the strategy itself — never from a control on this page.
  const uni = resolveUniverse(strategy, baskets);

  const progressPct =
    status && status.combos_total > 0 ? Math.min(100, Math.round((status.combos_done / status.combos_total) * 100)) : 0;

  // One phrase describing where each job is, shared by the status strip beside
  // the button and the fuller line in the card below it — so the two can't
  // disagree about what's happening.
  const searchPhase = !status
    ? null
    : status.phase === "downloading bars"
      ? "Downloading historical bars…"
      : `Searching ${status.combos_done} of ${status.combos_total} combinations…`;
  const sweepPct =
    sweepStatus && sweepStatus.baskets_total > 0
      ? Math.round((sweepStatus.baskets_done / sweepStatus.baskets_total) * 100)
      : 0;
  const sweepPhase = !sweepStatus
    ? null
    : sweepStatus.phase === "downloading bars"
      ? "Downloading daily bars for every basket…"
      : `Basket ${Math.min(sweepStatus.baskets_done + 1, sweepStatus.baskets_total)} of ${sweepStatus.baskets_total}` +
        (sweepStatus.current_basket ? ` — ${sweepStatus.current_basket}` : "");

  const hb = result?.hold_benchmark_comparison;

  return (
    <>
      <div className="toolbar">
        <h2>
          Strategy optimizer <InfoTip k="parameter_search" />
        </h2>
      </div>

      <div className="card card-form">
        <p className="hint">
          A <strong>parameter search</strong> — not "AI". It runs the same backtester across many settings so you find
          configs that actually held up, instead of guessing numbers. Every setting is tried{" "}
          <strong>relative to what it is now</strong> — a few steps up and down from your own value, each step the same
          percentage — so the search refines the strategy you have rather than sweeping numbers it invented. Only
          settings you've switched on are tuned; it won't turn a rule on for you. Every guard here exists to fight{" "}
          <strong>overfitting</strong> <InfoTip k="overfitting" />:
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
          </div>
          {/* The universe is the STRATEGY'S, shown read-only — same rule the
              backtest follows. Optimizing against a hand-picked symbol list
              tunes settings for those names, then hands them to a strategy that
              trades a different pool; the numbers would describe an experiment
              you can never actually run. */}
          <UniverseChips uni={uni} />
          {uni.scannerReplay ? (
            <p className="hint">
              A scanner strategy is searched against its <strong>real, day-varying universe</strong> — each past day,
              only that day's top {strategy?.top_n ?? 10} risers were eligible to enter, replayed offline from the bar
              cache. Needs a completed sweep (Settings → Historical bar cache).
            </p>
          ) : uni.symbols.length === 1 ? (
            <p className="hint warn">
              <IconWarn className="icon-inline" /> This strategy trades a <strong>single symbol</strong>, so the search
              can only fit that one name's history. Settings found this way rarely survive contact with another.
            </p>
          ) : (
            <p className="hint">
              Searched across the strategy's whole universe, never one ticker — a setting that fits a single name's
              history rarely survives contact with another.
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
                disabled={uni.scannerReplay || stratMixed}
                title={
                  uni.scannerReplay
                    ? "Ignored in scanner replay — the cache decides (15-min if swept, else daily)"
                    : stratMixed
                      ? "Fixed by the strategy: daily signals + a stop means a 15-minute replay"
                      : undefined
                }
              >
                {/* No 1-hour option, same as the backtest: 15-min is strictly the
                    more faithful intraday simulation (the live engine ticks ~60s). */}
                <option value="1Day" disabled={stratMixed}>1 day (fast — recommended for a search)</option>
                <option value="15Min" disabled={stratLockedDaily}>15 minutes (slower, precise)</option>
              </select>
              {stratLockedDaily && !uni.scannerReplay && (
                <span className="field-help warn">
                  <IconWarn className="icon-inline" /> This strategy uses MACD/RSI (daily signals) and has no stop /
                  trailing stop / take-profit, so the search is locked to 1 Day — on intraday bars the signals whipsaw
                  and won't match the live engine, and there's no price-triggered exit an intraday replay could test.
                </span>
              )}
              {stratMixed && !uni.scannerReplay && (
                <span className="field-help">
                  This strategy has <strong>daily signals (MACD/RSI) and a price-triggered stop</strong>, so the search
                  runs <strong>mixed resolution</strong>: entries and exits replay on 15-minute bars while MACD/RSI keep
                  coming from <em>completed daily closes</em>, exactly as the live engine reads them. That matters here —
                  three of the four searched knobs (trailing stop, stop-loss, take-profit) only trigger intraday, so on
                  daily bars a tight stop looks almost free and the search would drift toward stops that whipsaw for
                  real. Expect it to take <strong>much longer</strong>: ~26× as many bars per day to replay.
                </span>
              )}
              {stratNeedsIntraday && !uni.scannerReplay && (
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
              <span className="field-cap">Step size</span>
              <select value={relStep} onChange={(e) => setRelStep(Number(e.target.value))}>
                <option value={0.1}>±10% per step (fine)</option>
                <option value={0.15}>±15% per step (default)</option>
                <option value={0.25}>±25% per step (wide)</option>
              </select>
              <span className="field-help">
                How far apart the values tried for each setting are. Every setting is searched relative to what it is
                now — a 2% trailing stop is tried at 1.74, 2.00, 2.30 and so on, four steps either way.
              </span>
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
          {error !== null && <div className="error">{error}</div>}
          {/* Not disabled when nothing is selected — a dead button looks broken.
              start() explains instead. */}
          <button disabled={running}>{running ? "Searching…" : "Run parameter search"}</button>
          <RunStatus
            running={running}
            phase={searchPhase}
            pct={status?.phase === "downloading bars" ? null : progressPct}
            startedAt={status?.started_at}
          />
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
              <strong>{result.tested_combinations.toLocaleString()} combinations</strong>
              {result.relative_step ? <> at ±{Math.round(result.relative_step * 100)}% per step</> : null}
              {result.search_space_size ? (
                <> of ~{result.search_space_size.toLocaleString()} possible</>
              ) : null}{" "}
              · {result.symbols.length} symbol{result.symbols.length === 1 ? "" : "s"} · last {result.days} days (
              {result.timeframe})
            </h3>
            {result.scanner_replay ? (
              <>
                <p className="hint">
                  Tested by <strong>scanner replay</strong>: each day's top {result.replay_top_n} risers over{" "}
                  {result.days_replayed ?? "—"} days — <strong>{result.universe_size ?? result.symbols.length}</strong>{" "}
                  distinct names searched, using {result.replay_intraday ? "15-minute (intraday)" : "daily"} bars from
                  the cache.
                </p>
                {(result.universe_dropped?.length ?? 0) > 0 && (
                  <p className="hint warn">
                    <IconWarn className="icon-inline" />{" "}
                    <strong>
                      {result.universe_dropped!.length} mover
                      {result.universe_dropped!.length === 1 ? "" : "s"} couldn't be searched
                    </strong>{" "}
                    — no cached bars for {result.universe_dropped!.slice(0, 8).join(", ")}
                    {result.universe_dropped!.length > 8 ? ", …" : ""}. They made a top-N list but weren't tested, so
                    these results cover a smaller universe than the scanner actually saw. Re-run the sweep (Settings →
                    Historical bar cache) to fill them in.
                  </p>
                )}
              </>
            ) : (
              <p className="hint">
                Tested on: <strong>{result.symbols.join(", ")}</strong>
              </p>
            )}
            {result.mixed_resolution && (
              <p className="hint">
                <strong>Stops tested on 15-minute bars, signals from daily closes.</strong> MACD/RSI came from{" "}
                <em>completed</em> daily closes (no peek at the day in progress), while every searched stop-loss,
                trailing stop and take-profit was checked against real intraday prices — so a tight stop was scored on
                whether it would actually have been hit, not on where the day happened to close.
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
              <Stat
                label="Out-of-sample entries"
                value={String(result.best.out_of_sample?.entries ?? result.best.out_of_sample?.trades ?? "—")}
              />
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
              {(() => {
                const best = result.best!.params as unknown as Record<string, number>;
                const base = result.baseline_params ?? {};
                const knobs = KNOB_ORDER.filter((k) => best[k] !== undefined);
                const changed = knobs.filter((k) => {
                  const from = base[k]?.value;
                  return from == null || Math.abs(from - best[k]) > 1e-9;
                });
                return (
                  <>
                    <p className="hint">
                      What the search would change, next to what "{result.strategy_name ?? strategy?.name}" is set to
                      now —{" "}
                      <strong>
                        {changed.length} of {knobs.length} setting{knobs.length === 1 ? "" : "s"}
                      </strong>{" "}
                      {changed.length === 1 ? "differs" : "differ"}.
                    </p>
                    <div className="knob-diffs">
                      {knobs.map((k) => (
                        <KnobDiff
                          key={k}
                          knob={k}
                          from={base[k]?.value}
                          to={best[k]}
                          inGrid={base[k]?.in_grid ?? true}
                        />
                      ))}
                    </div>
                  </>
                );
              })()}
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
              For each knob, the bar in the middle-ish is the winning value; the bars around it are the values one step
              either side. When neighbours score <strong>similarly</strong>, the setting is a dependable plateau. When
              the winner <strong>towers alone</strong> over bad neighbours, it's likely a fluke that won't repeat — and
              a bigger step size makes that easier to tell apart, because neighbours are further away.
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
                    {showStopLoss && <th>Stop</th>}
                    {showAtrStop && <th>ATR stop</th>}
                    <th>Take-profit</th>
                    {showMacd && <th>MACD slow</th>}
                    {showRsiMin && <th>Min RSI</th>}
                    {showRsiMax && <th>Max RSI</th>}
                    {showRsiExit && <th>Sell RSI&gt;</th>}
                    <th>In-sample</th>
                    <th>Out-of-sample (real)</th>
                    <th>OOS entries</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.map((r) => (
                    <tr key={r.rank} className={r.is_best ? "cmp-win" : ""}>
                      <td>{r.rank}</td>
                      <td>{r.params.min_day_gain_pct}</td>
                      <td>{r.params.trailing_stop_pct}</td>
                      {showStopLoss && <td>{r.params.stop_loss_pct}</td>}
                      {showAtrStop && <td>{r.params.atr_stop_mult}×</td>}
                      <td>{r.params.take_profit_pct}</td>
                      {showMacd && <td>{r.params.macd_slow ?? "—"}</td>}
                      {showRsiMin && <td>{r.params.rsi_min || "off"}</td>}
                      {showRsiMax && <td>{r.params.rsi_max || "off"}</td>}
                      {showRsiExit && <td>{r.params.exit_rsi_above || "off"}</td>}
                      <td>{pct(r.in_sample?.net_pnl_pct)}</td>
                      <td className={(r.out_of_sample?.net_pnl_pct ?? 0) >= 0 ? "up" : "down"}>
                        {pct(r.out_of_sample?.net_pnl_pct)}
                      </td>
                      <td>{r.out_of_sample?.entries ?? r.out_of_sample?.trades ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      <div className="card">
        <h3>
          Basket sweep — which theme would have beaten SPY? <InfoTip k="parameter_search" />
        </h3>
        <p className="hint">
          Runs the <strong>same parameter search across every basket</strong> with one identical momentum template
          {sweepResult
            ? ` ($${sweepResult.template_sizing.sizing_usd.toLocaleString()}/trade, $${sweepResult.template_sizing.sleeve_usd.toLocaleString()} sleeve, max ${sweepResult.template_sizing.max_positions} positions, daily bars)`
            : " ($1,000/trade, $5,000 sleeve, max 5 positions, daily bars)"}
          , then ranks the winners by their <strong>out-of-sample margin over SPY</strong> — measured only on the slice
          of history each search never saw. A winner with no out-of-sample trades ranks last as untested. Every row is a{" "}
          <strong>hypothesis to shadow- and paper-trade</strong>, never a verdict; the numbers come from the backtester,
          not from anyone's opinion.
        </p>
        <p className="hint warn">
          <IconWarn className="icon-inline" /> <strong>The sweep ranks on daily bars, so its stop values are
          indicative.</strong>{" "}
          A daily replay checks a stop once a day at the close, so a position that dipped through its stop and recovered
          is scored a winner — which makes a tight stop look almost free. The <em>ranking</em> survives that (every
          basket is scored the same way), but before trusting a row's stop-loss or trailing stop, re-run that basket
          through the single-strategy search above, where a strategy with a price-triggered exit is replayed on
          15-minute bars. Sweeping every basket intraday would mean an enormous download, which is why it stays daily.
        </p>
        <div className="filter-grid">
          <label>
            <span className="field-cap">
              History (days) <InfoTip k="history_days" />
            </span>
            <NumberField min={90} max={730} step={1} value={sweepDays} onChange={setSweepDays} />
          </label>
          <label>
            <span className="field-cap">Combinations per basket</span>
            <NumberField min={5} max={200} step={1} value={sweepIterations} onChange={setSweepIterations} />
          </label>
        </div>
        {sweepError && <div className="error">{sweepError}</div>}
        {sweepStatus?.error && !sweepRunning && <div className="error">Sweep failed: {sweepStatus.error}</div>}
        <button type="button" disabled={sweepRunning || running} onClick={runSweep}>
          {sweepRunning ? "Sweeping…" : "Sweep all baskets"}
        </button>
        {/* started_at comes from the SERVER, so the clock survives a page reload
            mid-sweep instead of restarting at zero on a job that's been running
            for ten minutes. */}
        <RunStatus
          running={sweepRunning}
          phase={sweepPhase}
          pct={sweepStatus?.phase === "downloading bars" ? null : sweepPct}
          startedAt={sweepStatus?.started_at}
        />
        {sweepRunning && sweepStatus && (
          <>
            <p className="hint">{sweepPhase}</p>
            <div className="progress-track" aria-label="sweep progress">
              <div
                className="progress-fill"
                style={{
                  width: `${
                    sweepStatus.phase === "downloading bars" || !sweepStatus.baskets_total
                      ? 5
                      : Math.round((sweepStatus.baskets_done / sweepStatus.baskets_total) * 100)
                  }%`,
                }}
              />
            </div>
          </>
        )}
        {sweepResult && (
          <>
            <p className="hint">
              Swept <strong>{sweepResult.rows.length} baskets</strong> × ~{sweepResult.iterations} combinations each
              over the last {sweepResult.days} days.
              {!sweepResult.spy_available && " SPY data was unavailable, so margins are missing."}
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Basket</th>
                    <th>Out-of-sample (real)</th>
                    <th>SPY (same window)</th>
                    <th>Margin</th>
                    <th>vs holding the basket</th>
                    <th>OOS entries</th>
                    <th>Winning settings</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {sweepResult.rows.map((r) => (
                    <tr key={r.basket_id} className={r.rank === 1 && !r.untested ? "cmp-win" : ""}>
                      <td>{r.rank}</td>
                      <td className="sym">
                        {r.basket_name} <span className="hint">({r.symbols.length} symbols)</span>
                      </td>
                      <td className={(r.oos_pct ?? 0) >= 0 ? "up" : "down"}>{pct(r.oos_pct)}</td>
                      <td>{pct(r.spy_oos_pct)}</td>
                      <td className={r.margin_vs_spy == null ? "" : r.margin_vs_spy >= 0 ? "up" : "down"}>
                        {r.margin_vs_spy == null
                          ? "—"
                          : `${r.margin_vs_spy >= 0 ? "+" : ""}${r.margin_vs_spy} pts`}
                      </td>
                      <td>{r.beat_hold == null ? "—" : r.beat_hold ? "beat it" : "lost to it"}</td>
                      <td>
                        {r.oos_entries ?? r.oos_trades ?? "—"}
                        {r.untested && <span className="hint"> (untested)</span>}
                      </td>
                      <td className="hint">
                        gain≥{r.best_params.min_day_gain_pct}% · trail {r.best_params.trailing_stop_pct}% · stop{" "}
                        {r.best_params.stop_loss_pct}% · TP {r.best_params.take_profit_pct || "off"}
                      </td>
                      <td>
                        {sweepSaved[r.basket_id] ? (
                          <span className="hint">saved ✓</span>
                        ) : (
                          <button type="button" className="small" onClick={() => saveSweepDraft(r)}>
                            Save draft
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {sweepResult.rows[0]?.warnings.map((w, i) => (
              <p key={i} className="hint warn">
                <IconWarn className="icon-inline" /> Leader: {w}
              </p>
            ))}
            {sweepResult.skipped.length > 0 && (
              <p className="hint">
                Skipped: {sweepResult.skipped.map((s) => `${s.basket_name} (${s.reason})`).join("; ")}.
              </p>
            )}
            <p className="hint">
              "Save draft" creates a <strong>disabled</strong> strategy on that basket with the winning settings —
              review it, run it in shadow, and let the paper scoreboard be the judge.
            </p>
          </>
        )}
      </div>
    </>
  );
}
