import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  Basket,
  createStrategy,
  deleteStrategy,
  getBaskets,
  getPresets,
  getStatus,
  getStrategies,
  getStrategyHoldings,
  getStrategyLastRun,
  getStrategyRanking,
  Preset,
  RankBy,
  StrategyHoldings,
  StrategyLastRun,
  StrategyRanking,
  StrategyRow,
  toggleStrategy,
  updateStrategy,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";
import UniverseSymbols from "../components/UniverseSymbols";
import { IconDelete, IconEdit, IconOptimize, IconPause, IconPlay, IconWarn } from "../components/icons";
import { consumeNav, requestNav } from "../lib/nav";

const RANK_LABELS: Record<RankBy, string> = {
  momentum_today: "Today's % move (momentum)",
  return_30d: "30-day return",
  relative_strength: "Relative strength (vs 200-day average)",
  rs_vs_spy: "Relative strength vs S&P 500",
  rsi: "RSI (14-day) — highest first",
};

const EMPTY: Partial<StrategyRow> = {
  name: "",
  asset_class: "stock",
  universe: "scanner",
  basket_id: null,
  symbols: [],
  rank_by: "momentum_today",
  top_n: 10,
  rank_enabled: false,
  preset: "custom",
  swing_mode: true,
  ignore_regime: false,
  sizing_usd: 200,
  sleeve_usd: 1000,
  max_positions: 3,
  params: {
    entry: {
      min_day_gain_pct: 3,
      max_day_gain_pct: 0,
      min_price: 0,
      max_price: 0,
      require_above_vwap: true,
      rsi_min: 0,
      rsi_max: 0,
      entry_window_start: "09:30",
      entry_window_end: "15:30",
      entry_slippage_pct: 0.5,
    },
    exit: {
      trailing_stop_pct: 5,
      stop_loss_pct: 4,
      take_profit_pct: 12,
      max_holding_hours: 120,
      flatten_before_close: false,
      exit_below_vwap: false,
      exit_rsi_above: 0,
      exit_on_regime_bear: false,
      exit_slippage_pct: 1,
      exit_slippage_max_pct: 1,
    },
    execution: { market_orders: false },
  },
};

// One compact numeric/labelled control: a caption (+ optional ? tooltip) above a
// value-sized input. Grouped in a .param-grid so a strategy's numbers read as a
// related set rather than a stack of full-width text boxes.
function Param({
  label,
  tip,
  children,
}: {
  label: ReactNode;
  tip?: Parameters<typeof InfoTip>[0]["k"];
  children: ReactNode;
}) {
  return (
    <div className="param">
      <span className="param-cap">
        {label}
        {tip && <InfoTip k={tip} />}
      </span>
      {children}
    </div>
  );
}

// A segmented pill group — two options as a "slider" (Asset class, Trading
// style) or a wrapping multi-option set with sub-labels (Universe).
function Segmented<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  wrap = false,
}: {
  value: T;
  onChange: (v: T) => void;
  // `sub` renders a smaller second line under the label — for options whose
  // name alone doesn't say what they do (e.g. "Scanner" / today's risers).
  options: { value: T; label: string; sub?: string }[];
  ariaLabel: string;
  wrap?: boolean; // let a 5-option group wrap instead of overflowing the card
}): ReactNode {
  return (
    <div className={`segmented${wrap ? " segmented-wrap" : ""}`} role="group" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={value === o.value ? "seg-on" : ""}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
          {o.sub && <span className="seg-sub">{o.sub}</span>}
        </button>
      ))}
    </div>
  );
}

// Expandable per-strategy holdings: the open positions this strategy owns right
// now, with best-effort live unrealized P&L. Lazy-loads on first expand.
function HoldingsView({ strategyId, count }: { strategyId: number; count: number }) {
  const [data, setData] = useState<StrategyHoldings | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function load() {
    if (data || loading) return;
    setLoading(true);
    setErr(null);
    getStrategyHoldings(strategyId)
      .then(setData)
      .catch((e: Error) => setErr(e.message))
      .finally(() => setLoading(false));
  }

  const money = (n: number) => `${n < 0 ? "−" : ""}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

  return (
    <details
      className="holdings"
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) load();
      }}
    >
      <summary>Holdings ({count})</summary>
      {loading && <p className="hint">Loading positions…</p>}
      {err && <div className="error">{err}</div>}
      {data && data.holdings.length === 0 && <p className="hint">No open positions right now.</p>}
      {data && data.holdings.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="holdings-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Qty</th>
                  <th>Entry</th>
                  <th>Now</th>
                  <th>Unreal. P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {data.holdings.map((h, i) => (
                  <tr key={i}>
                    <td>{h.symbol}</td>
                    <td>{h.qty.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td>
                    <td>{h.entry_price != null ? money(h.entry_price) : "—"}</td>
                    <td>{h.current_price != null ? money(h.current_price) : "—"}</td>
                    <td className={(h.unrealized_pnl ?? 0) >= 0 ? "up" : "down"}>
                      {h.unrealized_pnl != null ? `${money(h.unrealized_pnl)} (${h.unrealized_pct}%)` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.total_value > 0 && (
            <p className="hint">
              Value {money(data.total_value)} · unrealized{" "}
              <span className={data.total_unrealized_pnl >= 0 ? "up" : "down"}>{money(data.total_unrealized_pnl)}</span>
              {" "}(prices may lag a little)
            </p>
          )}
        </>
      )}
    </details>
  );
}

// "Last run" debug view: the engine's most recent decision trace for a strategy
// — what it looked at each cycle and why it did (or didn't) buy. Reloads on each
// expand to show the latest.
function LastRunView({ strategyId }: { strategyId: number }) {
  const [data, setData] = useState<StrategyLastRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function load() {
    if (loading) return;
    setLoading(true);
    setErr(null);
    getStrategyLastRun(strategyId)
      .then(setData)
      .catch((e: Error) => setErr(e.message))
      .finally(() => setLoading(false));
  }

  const badge = (d: string) =>
    d === "bought" ? "up" : d === "blocked" ? "down" : "";

  return (
    <details
      className="lastrun"
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) load();
      }}
    >
      <summary>Last run — why it did / didn't buy</summary>
      {loading && <p className="hint">Loading…</p>}
      {err && <div className="error">{err}</div>}
      {data && !data.ran && (
        <p className="hint">
          Hasn't run yet since the app started. Enable it and turn the engine on, then check back after a cycle
          (~1 min).
        </p>
      )}
      {data && data.ran && (
        <>
          <p className="hint">
            Ran <strong>{data.ran_at ? new Date(data.ran_at).toLocaleString() : "—"}</strong>
            {data.universe ? <> · looks in: {data.universe}</> : null}
          </p>
          <p className="hint">
            <strong>{data.outcome}</strong>
          </p>
          {data.candidates && data.candidates.length > 0 && (
            <div className="table-scroll">
              <table className="holdings-table lastrun-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Day</th>
                    <th>Decision</th>
                    <th className="why">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {data.candidates.map((c, i) => (
                    <tr key={i}>
                      <td>{c.symbol}</td>
                      <td className={c.change_pct >= 0 ? "up" : "down"}>
                        {c.change_pct >= 0 ? "+" : ""}
                        {c.change_pct}%
                      </td>
                      <td className={badge(c.decision)}>{c.decision}</td>
                      <td className="why">{c.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </details>
  );
}

// "Current ranking" view: for a ranked strategy, the live ranking of its whole
// pool — which names make the top-N cut and which (e.g. one you expected) don't.
function RankingView({ strategyId }: { strategyId: number }) {
  const [data, setData] = useState<StrategyRanking | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function load() {
    if (loading) return;
    setLoading(true);
    setErr(null);
    getStrategyRanking(strategyId)
      .then(setData)
      .catch((e: Error) => setErr(e.message))
      .finally(() => setLoading(false));
  }

  return (
    <details
      className="lastrun"
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) load();
      }}
    >
      <summary>Current ranking — who's eligible right now</summary>
      {loading && <p className="hint">Ranking the pool…</p>}
      {err && <div className="error">{err}</div>}
      {data && !data.ranked && <p className="hint">{data.reason}</p>}
      {data && data.ranked && data.error && <p className="hint">{data.error}</p>}
      {data && data.ranked && !data.error && (
        <>
          <p className="hint">
            Ranked by <strong>{data.rank_label}</strong>. The top <strong>{data.top_n}</strong> (✓) are the ones this
            strategy will consider; the rest are ranked out.
          </p>
          <div className="table-scroll">
            <table className="holdings-table ranking-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Symbol</th>
                  <th>{data.rank_label}</th>
                  <th>Day</th>
                  <th>
                    MACD <InfoTip k="macd" />
                  </th>
                </tr>
              </thead>
              <tbody>
                {(data.rows ?? []).map((r, i) => (
                  <tr key={i} className={r.in_top_n ? "in-topn" : ""}>
                    <td>{r.rank ?? "—"}</td>
                    <td>
                      {r.symbol} {r.in_top_n ? "✓" : ""}
                    </td>
                    <td>{r.value != null ? `${r.value}%` : "—"}</td>
                    <td className={(r.change_pct ?? 0) >= 0 ? "up" : "down"}>
                      {r.change_pct != null ? `${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%` : "—"}
                    </td>
                    <td className={r.macd_bullish == null ? "" : r.macd_bullish ? "up" : "down"}>
                      {r.macd_bullish == null ? "—" : r.macd_bullish ? "Bullish" : "Bearish"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </details>
  );
}

function Editor({
  initial,
  presets,
  baskets,
  allStrategies,
  equity,
  onSaved,
  onCancel,
  onBasketsChanged,
}: {
  initial: Partial<StrategyRow>;
  presets: Record<string, Preset>;
  baskets: Basket[];
  allStrategies: StrategyRow[];
  equity: number | null;
  onSaved: () => void;
  onCancel: () => void;
  onBasketsChanged: () => void;
}) {
  const [s, setS] = useState<Partial<StrategyRow>>(JSON.parse(JSON.stringify(initial)));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // guards against a double-submit mid-save

  // What this strategy already holds (cost basis) — the same number the sleeve
  // rail measures against. Only meaningful when editing an existing strategy;
  // used to warn if the new sleeve is set below current holdings.
  const [heldExposure, setHeldExposure] = useState<number | null>(null);
  useEffect(() => {
    if (typeof initial.id !== "number") return;
    let alive = true;
    getStrategyHoldings(initial.id)
      .then((h) => alive && setHeldExposure(h.total_cost))
      .catch(() => alive && setHeldExposure(null));
    return () => {
      alive = false;
    };
  }, [initial.id]);

  // Live sleeve-allocation readout. Sleeves are allowed to overlap (sum > equity)
  // on purpose — whichever strategy trades first draws the shared cash, and the
  // no-leverage rail caps total spending at real equity. So this only informs,
  // never blocks.
  const otherSleeves = allStrategies
    .filter((r) => r.id !== s.id)
    .reduce((sum, r) => sum + (r.sleeve_usd || 0), 0);
  const totalSleeves = otherSleeves + (s.sleeve_usd || 0);
  const overAllocated = equity != null && totalSleeves > equity;
  const money = (n: number) => `$${Math.round(n).toLocaleString()}`;

  // Config traps that silently ruin a backtest / live run — warn, never block.
  const sizing = s.sizing_usd || 0;
  const sleeve = s.sleeve_usd || 0;
  // When the per-trade size is AS LARGE AS the whole sleeve, the strategy is
  // "all-in": at most one position, and — because the no-leverage rail caps total
  // spend at real equity — a single losing trade drops equity below one full
  // position and blocks EVERY entry after it (the strategy silently stalls, e.g.
  // a $1k/trade, $1k account that loses $30 can never fund another $1k buy).
  const allInSizing = sizing > 0 && sleeve > 0 && sizing >= sleeve;
  // Milder: sizing under the sleeve but over half of it → only ONE position fits.
  const oneShotSleeve = sizing > 0 && sleeve > 0 && !allInSizing && sleeve < sizing * 2;
  const maxConcurrent = sizing > 0 ? Math.floor(sleeve / sizing) : 0;
  const stopPct = s.params?.exit.stop_loss_pct ?? 0;
  // A tight stop held overnight is hit by normal daily noise — a swing killer.
  const tightSwingStop = !!s.swing_mode && stopPct > 0 && stopPct < 3;
  // Setting the sleeve below what the strategy already holds sells nothing — it
  // just freezes NEW buys until exits free up room. Reassure, don't alarm.
  const sleeveBelowHoldings = heldExposure != null && sleeve > 0 && sleeve < heldExposure;
  // VWAP is an INTRADAY measure (it resets each session), so requiring it forces
  // intraday-bar backtests and doesn't belong in a daily/swing rotation whose
  // signals (RSI, MACD, relative strength) are all daily. Warn on swing + VWAP.
  const vwapOnSwing = !!s.swing_mode && !!s.params?.entry.require_above_vwap;

  // --- How many positions can actually be open at once? Three separate caps the
  // engine applies (capital, the Max-positions knob, the universe size), shown
  // together so the sizing numbers don't read as independent when they're coupled.
  const atrSizingOn =
    Number(s.params?.atr?.risk_usd || 0) > 0 && Number(s.params?.atr?.stop_mult || 0) > 0;
  const maxPos = s.max_positions || 0;
  const customCount = s.symbols?.length || 0;
  // Known universe size only for FIXED universes; scanner/watchlist/both are
  // dynamic (top movers / live list), so there's no fixed count to show.
  const universeCap =
    s.universe === "custom" && customCount > 0
      ? s.rank_enabled && s.top_n
        ? Math.min(s.top_n, customCount)
        : customCount
      : s.universe === "basket" && s.top_n
        ? s.top_n
        : null;
  // Capital-implied count is a clean integer only with FIXED sizing; ATR sizing
  // makes $/trade vary, so it isn't a fixed cap then.
  const capitalCap = !atrSizingOn && sizing > 0 ? maxConcurrent : null;
  const posCaps: { n: number; why: string }[] = [];
  if (capitalCap != null && capitalCap > 0)
    posCaps.push({ n: capitalCap, why: `capital (${money(sleeve)} ÷ ${money(sizing)})` });
  if (maxPos > 0) posCaps.push({ n: maxPos, why: `“Max positions” (${maxPos})` });
  if (universeCap != null)
    posCaps.push({
      n: universeCap,
      why:
        s.universe === "custom"
          ? `your universe (${universeCap} symbol${universeCap === 1 ? "" : "s"})`
          : `the basket top-${universeCap}`,
    });
  const effectiveCap = posCaps.length ? Math.min(...posCaps.map((c) => c.n)) : 0;
  const posBinders = posCaps.filter((c) => c.n === effectiveCap).map((c) => c.why);
  const bindersText =
    posBinders.length <= 1
      ? posBinders[0] ?? ""
      : `${posBinders.slice(0, -1).join(", ")} and ${posBinders[posBinders.length - 1]}`;
  // "Max positions" set higher than the real limit → it isn't the active cap.
  const maxPosNoOp = maxPos > 0 && effectiveCap > 0 && maxPos > effectiveCap;
  // "Max positions" set below what capital allows → deliberately parks cash.
  const maxPosHoldsBack =
    maxPos > 0 && capitalCap != null && maxPos < capitalCap && (universeCap == null || maxPos < universeCap);
  const parkedCash = maxPosHoldsBack ? Math.max(0, sleeve - maxPos * sizing) : 0;

  function applyPreset(key: string) {
    if (key === "custom") {
      setS({ ...s, preset: "custom" });
      return;
    }
    const p = presets[key];
    setS({
      ...s,
      preset: key,
      name: s.name || p.label,
      asset_class: p.asset_class,
      universe: p.universe as StrategyRow["universe"],
      swing_mode: p.swing_mode,
      // Basket presets (e.g. sector rotation) carry their own ranking + count.
      rank_by: (p.rank_by ?? s.rank_by ?? "momentum_today") as StrategyRow["rank_by"],
      top_n: p.top_n ?? s.top_n ?? 10,
      // Custom-universe presets (e.g. the DCA sleeve) can seed a starter symbol
      // list; the deep-copied params carries params.dca along for the ride.
      symbols: p.symbols ?? s.symbols ?? [],
      params: JSON.parse(JSON.stringify(p.params)),
    });
  }

  function setEntry(key: string, value: unknown) {
    setS((cur) => ({ ...cur, params: { ...cur.params!, entry: { ...cur.params!.entry, [key]: value } } }));
  }
  function setExit(key: string, value: unknown) {
    setS((cur) => ({ ...cur, params: { ...cur.params!, exit: { ...cur.params!.exit, [key]: value } } }));
  }
  // MACD periods are optional; seed the 12/26/9 defaults the first time one is
  // edited so all three are stored together.
  function setMacd(key: "fast" | "slow" | "signal", value: number) {
    setS((cur) => ({
      ...cur,
      params: {
        ...cur.params!,
        macd: { fast: 12, slow: 26, signal: 9, ...(cur.params!.macd ?? {}), [key]: value },
      },
    }));
  }
  // ATR block is optional; seed 14 / 0 / 0 (period / stop off / sizing off) the
  // first time any field is edited so all three are stored together.
  function setAtr(key: "period" | "stop_mult" | "risk_usd", value: number) {
    setS((cur) => ({
      ...cur,
      params: {
        ...cur.params!,
        atr: { period: 14, stop_mult: 0, risk_usd: 0, ...(cur.params!.atr ?? {}), [key]: value },
      },
    }));
  }

  // Swing vs intraday are opposites (hold overnight vs flatten before the close),
  // so they're one choice, not two independent checkboxes. Intraday implies
  // flatten-before-close for stocks; crypto has no close, so flatten stays off.
  const tradingStyle: "swing" | "intraday" = s.swing_mode ? "swing" : "intraday";
  function setStyle(style: "swing" | "intraday") {
    setS((cur) => ({
      ...cur,
      swing_mode: style === "swing",
      params: {
        ...cur.params!,
        exit: {
          ...cur.params!.exit,
          flatten_before_close: style === "intraday" && cur.asset_class === "stock",
        },
      },
    }));
  }

  /** Write the form to the server. Returns the strategy id (a NEW strategy only
   *  gets one here), or null if it failed — in which case the error is already
   *  on screen and the caller must not navigate away. */
  async function persist(): Promise<number | null> {
    setError(null);
    try {
      if (s.id) {
        await updateStrategy(s.id, s);
        return s.id;
      }
      return (await createStrategy(s)).id;
    } catch (err) {
      setError((err as Error).message);
      return null;
    }
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const id = await persist();
    setBusy(false);
    if (id !== null) onSaved();
  }

  /** Save, THEN jump to the backtest/optimizer. Both of those read the saved
   *  strategy from the server, so navigating first silently tested the previous
   *  version — you'd tweak a stop, hit Backtest, and grade the old config. */
  async function saveAndGo(tab: string) {
    setBusy(true);
    const id = await persist();
    setBusy(false);
    if (id === null) return; // stay on the form; the error is shown above
    onSaved();
    requestNav({ tab, strategyId: id });
  }

  const p = s.params!;
  // Market orders have no limit price, so the marketable-limit slippage buffers
  // (entry + exit) don't apply — grey them out when market mode is on.
  const marketMode = !!p.execution?.market_orders;
  // The MACD period fields appear only when at least one MACD toggle is on.
  const macdOn = !!(p.entry.require_macd_bullish || p.exit.exit_on_macd_bearish);
  const macd = p.macd ?? { fast: 12, slow: 26, signal: 9 };
  const atr = p.atr ?? { period: 14, stop_mult: 0, risk_usd: 0 };
  const windowOn = !!(p.entry.entry_window_start && p.entry.entry_window_end);
  // Ranking: a basket is always ranked; watchlist/custom can opt in. Scanner/both
  // are already ranked by the scanner, so the controls don't apply there.
  const rankable = s.universe === "watchlist" || s.universe === "custom";
  const ranking = s.universe === "basket" || (rankable && !!s.rank_enabled);
  return (
    <form className="card editor" onSubmit={save}>
      <h3>{s.id ? `Edit: ${s.name}` : "New strategy"}</h3>

      {/* 1 — BASICS: what am I building, and how does it hold? */}
      <section className="builder-sec">
        <h4 className="builder-head">Start here</h4>
        <div className="filter-grid">
          <label>
            Start from preset
            <select value={s.preset} onChange={(e) => applyPreset(e.target.value)}>
              <option value="custom">Custom</option>
              {Object.entries(presets).map(([k, v]) => (
                <option key={k} value={k}>
                  {v.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Name
            <input value={s.name ?? ""} onChange={(e) => setS({ ...s, name: e.target.value })} required />
          </label>
        </div>
        <div className="slider-row">
          <div className="field">
            <span className="field-cap">Asset class</span>
            <Segmented
              value={s.asset_class as "stock" | "crypto"}
              ariaLabel="Asset class"
              onChange={(v) => setS({ ...s, asset_class: v })}
              options={[
                { value: "stock", label: "Stocks" },
                { value: "crypto", label: "Crypto" },
              ]}
            />
          </div>
          {/* "Swing vs intraday" is a SESSION concept. Crypto trades 24/7, so
              there is no day to be inside of and the toggle would be a label for
              nothing (the engine ignores the deferral for crypto). Show the real
              control instead: the hold-time ceiling. */}
          {s.asset_class === "crypto" ? (
            <div className="field">
              <span className="field-cap">
                Max holding time <InfoTip k="max_holding" />
              </span>
              <div className="affix">
                <NumberField step="1" min="0" value={p.exit.max_holding_hours}
                  onChange={(n) => setExit("max_holding_hours", n)} />
                <span className="affix-unit">hrs</span>
              </div>
              <span className="field-help">
                Crypto trades 24/7 — there's no market close, so "swing" and "intraday" don't apply. Your exit rules are
                live from the moment you're filled. Set a limit here to keep trades short (0 = no limit); stop-loss and
                trailing stop always apply.
              </span>
            </div>
          ) : (
            <div className="field">
              <span className="field-cap">
                Trading style <InfoTip k="swing_mode" />
              </span>
              <Segmented
                value={tradingStyle}
                ariaLabel="Trading style"
                onChange={setStyle}
                options={[
                  { value: "swing", label: "Swing" },
                  { value: "intraday", label: "Intraday" },
                ]}
              />
            </div>
          )}
        </div>
        {s.preset !== "custom" && presets[s.preset!] && <p className="sec-sub">{presets[s.preset!].description}</p>}
      </section>

      {/* 2 — UNIVERSE: where candidates come from */}
      <section className="builder-sec">
        <h4 className="builder-head">
          Universe <InfoTip k="universe" />
        </h4>
        <p className="sec-sub">Where this strategy looks for things to buy.</p>
        <div className="field">
          <span className="field-cap">Look in</span>
          <Segmented
            value={s.universe as NonNullable<StrategyRow["universe"]>}
            ariaLabel="Universe"
            wrap
            onChange={(v) => setS({ ...s, universe: v })}
            options={[
              { value: "scanner", label: "Scanner", sub: "today's risers" },
              { value: "watchlist", label: "Watchlist", sub: "your list only" },
              { value: "both", label: "Scanner + watchlist", sub: "both sources" },
              { value: "basket", label: "Basket", sub: "sector or theme" },
              { value: "custom", label: "Specific symbols", sub: "pick your own" },
            ]}
          />
        </div>
        <div className="filter-grid">
          {s.universe === "basket" && (
            <label>
              Basket
              <select
                value={s.basket_id ?? ""}
                onChange={(e) => setS({ ...s, basket_id: e.target.value ? Number(e.target.value) : null })}
                required
              >
                <option value="">— pick a basket —</option>
                {baskets.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name} ({b.count})
                  </option>
                ))}
              </select>
            </label>
          )}
          {p.dca && (
            <label>
              Buy every N days <InfoTip k="dca" />
              <NumberField
                step="1"
                min="1"
                value={p.dca.interval_days}
                onChange={(n) => setS((cur) => ({ ...cur, params: { ...cur.params!, dca: { interval_days: n } } }))}
              />
            </label>
          )}
        </div>

        {/* Ranking: a basket is always ranked; a watchlist/custom list opts in
            with this checkbox to trade only the strongest few. */}
        {rankable && (
          <label className="check" style={{ marginTop: "0.6rem" }}>
            <input
              type="checkbox"
              checked={!!s.rank_enabled}
              onChange={(e) => setS({ ...s, rank_enabled: e.target.checked })}
            />
            Rank &amp; trade only the top N of this list <InfoTip k="rank_enabled" />
          </label>
        )}
        {ranking && (
          <div className="filter-grid" style={{ marginTop: "0.6rem" }}>
            <label>
              Rank by {s.rank_by === "rs_vs_spy" && <InfoTip k="rs_vs_spy" />}
              <select value={s.rank_by} onChange={(e) => setS({ ...s, rank_by: e.target.value as RankBy })}>
                {(Object.keys(RANK_LABELS) as RankBy[])
                  // rs_vs_spy is benchmark-relative to SPY — a stock-only ranking.
                  .filter((k) => k !== "rs_vs_spy" || s.asset_class === "stock")
                  .map((k) => (
                    <option key={k} value={k}>
                      {RANK_LABELS[k]}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Take top N <InfoTip k="rank_by" />
              <NumberField step="1" min="1" max="50" value={s.top_n!} onChange={(n) => setS({ ...s, top_n: n })} />
            </label>
          </div>
        )}
        <div style={{ marginTop: "0.6rem" }}>
          <UniverseSymbols
            universe={s.universe!}
            assetClass={s.asset_class as "stock" | "crypto"}
            basketId={s.basket_id ?? null}
            baskets={baskets}
            onBasketsChanged={onBasketsChanged}
            customSymbols={s.symbols ?? []}
            onCustomChange={(syms) => setS({ ...s, symbols: syms })}
          />
        </div>
      </section>

      {/* 3 — ENTRY: when to buy */}
      <section className="builder-sec">
        <h4 className="builder-head">Entry criteria</h4>
        <p className="sec-sub">When the strategy is allowed to buy.</p>
        <div className="param-grid">
          <Param label="Min gain today (%)" tip="min_day_gain">
            <NumberField step="0.1" min="0" value={p.entry.min_day_gain_pct}
              onChange={(n) => setEntry("min_day_gain_pct", n)} />
          </Param>
        </div>
        <div className="check-row">
          <label className="check">
            <input type="checkbox" checked={p.entry.require_above_vwap}
              onChange={(e) => setEntry("require_above_vwap", e.target.checked)} />
            Require price above VWAP <InfoTip k="vwap" />
          </label>
          <label className="check">
            <input type="checkbox" checked={!!p.entry.require_macd_bullish}
              onChange={(e) => setEntry("require_macd_bullish", e.target.checked)} />
            Require bullish MACD <InfoTip k="macd" />
          </label>
        </div>

        <details className="adv">
          <summary>Advanced entry options</summary>
          <div className="param-grid">
            <Param label="Max gain today (%, 0 = off)" tip="max_day_gain">
              <NumberField step="0.1" min="0" value={p.entry.max_day_gain_pct ?? 0}
                onChange={(n) => setEntry("max_day_gain_pct", n)} />
            </Param>
            <Param label="Min share price ($, 0 = any)" tip="share_price_band">
              <NumberField step="any" min="0" value={p.entry.min_price ?? 0}
                onChange={(n) => setEntry("min_price", n)} />
            </Param>
            <Param label="Max share price ($, 0 = none)" tip="share_price_band">
              <NumberField step="any" min="0" value={p.entry.max_price ?? 0}
                onChange={(n) => setEntry("max_price", n)} />
            </Param>
            <Param label="Min RSI (0 = off)" tip="rsi_entry">
              <NumberField step="1" min="0" max="100" value={p.entry.rsi_min ?? 0}
                onChange={(n) => setEntry("rsi_min", n)} />
            </Param>
            <Param label="Max RSI (0 = off)" tip="rsi_entry">
              <NumberField step="1" min="0" max="100" value={p.entry.rsi_max ?? 0}
                onChange={(n) => setEntry("rsi_max", n)} />
            </Param>
            <Param label={marketMode ? "Entry slippage — n/a at market" : "Entry slippage (%)"} tip="entry_slippage">
              <NumberField step="0.1" min="0" max="5" value={p.entry.entry_slippage_pct ?? 0.5}
                onChange={(n) => setEntry("entry_slippage_pct", n)} disabled={marketMode} />
            </Param>
          </div>
          <div className="check-row">
            <label className="check">
              <input
                type="checkbox"
                checked={windowOn}
                onChange={(e) => {
                  if (e.target.checked) {
                    setEntry("entry_window_start", "09:30");
                    setEntry("entry_window_end", "15:30");
                  } else {
                    setEntry("entry_window_start", null);
                    setEntry("entry_window_end", null);
                  }
                }}
              />
              Limit entries to a time window (ET) <InfoTip k="entry_window" />
            </label>
          </div>
          {windowOn && (
            <div className="param-grid">
              <Param label="Window start (ET)">
                <input type="time" value={p.entry.entry_window_start!}
                  onChange={(e) => setEntry("entry_window_start", e.target.value || null)} />
              </Param>
              <Param label="Window end (ET)">
                <input type="time" value={p.entry.entry_window_end!}
                  onChange={(e) => setEntry("entry_window_end", e.target.value || null)} />
              </Param>
            </div>
          )}
        </details>
      </section>

      {/* 4 — EXIT: when to sell */}
      <section className="builder-sec">
        <h4 className="builder-head">Exit criteria</h4>
        <p className="sec-sub">When to sell — "the configurable downturn".</p>
        <div className="param-grid">
          <Param label="Trailing stop (%)" tip="trailing_stop">
            <NumberField step="0.1" min="0.5" value={p.exit.trailing_stop_pct}
              onChange={(n) => setExit("trailing_stop_pct", n)} />
          </Param>
          <Param label="Stop-loss (%) — required" tip="stop_loss">
            <NumberField step="0.1" min="0.1" value={p.exit.stop_loss_pct}
              onChange={(n) => setExit("stop_loss_pct", n)} />
          </Param>
          <Param label="Take-profit (%, 0 = off)" tip="take_profit">
            <NumberField step="0.1" min="0" value={p.exit.take_profit_pct}
              onChange={(n) => setExit("take_profit_pct", n)} />
          </Param>
        </div>

        <details className="adv">
          <summary>Advanced exit options</summary>
          <div className="param-grid">
            {/* Crypto promotes this to the main section (it replaces the
                inapplicable swing/intraday toggle) — don't show it twice. */}
            {s.asset_class !== "crypto" && (
              <Param label="Max holding time (hrs, 0 = off)" tip="max_holding">
                <NumberField step="1" min="0" value={p.exit.max_holding_hours}
                  onChange={(n) => setExit("max_holding_hours", n)} />
              </Param>
            )}
            <Param label={marketMode ? "Exit slippage — n/a at market" : "Exit slippage (%)"} tip="exit_slippage">
              <NumberField step="0.1" min="0" max="10" value={p.exit.exit_slippage_pct ?? 1}
                onChange={(n) => setExit("exit_slippage_pct", n)} disabled={marketMode} />
            </Param>
            <Param label={marketMode ? "Max exit slippage — n/a" : "Max exit slippage (%)"} tip="exit_slippage">
              <NumberField step="0.1" min="0" max="20" value={p.exit.exit_slippage_max_pct ?? 1}
                onChange={(n) => setExit("exit_slippage_max_pct", n)} disabled={marketMode} />
            </Param>
            <Param label="Sell if RSI above (0 = off)" tip="rsi_exit">
              <NumberField step="1" min="0" max="100" value={p.exit.exit_rsi_above ?? 0}
                onChange={(n) => setExit("exit_rsi_above", n)} />
            </Param>
          </div>
          <div className="check-row">
            <label className="check">
              <input type="checkbox" checked={p.exit.exit_below_vwap}
                onChange={(e) => setExit("exit_below_vwap", e.target.checked)} />
              Exit if price falls below VWAP <InfoTip k="vwap" />
            </label>
            <label className="check">
              <input type="checkbox" checked={!!p.exit.exit_on_macd_bearish}
                onChange={(e) => setExit("exit_on_macd_bearish", e.target.checked)} />
              Exit when MACD turns bearish <InfoTip k="macd" />
            </label>
            {s.asset_class === "stock" && (
              <label className="check">
                <input type="checkbox" checked={!!p.exit.exit_on_regime_bear}
                  onChange={(e) => setExit("exit_on_regime_bear", e.target.checked)} />
                Sell to cash when the market turns down (SPY &lt; 200-day) <InfoTip k="regime_exit" />
              </label>
            )}
            {ranking && (
              <label className="check">
                <input type="checkbox" checked={!!p.exit.rotate_on_rank_dropout}
                  onChange={(e) => setExit("rotate_on_rank_dropout", e.target.checked)} />
                Rotate out when it leaves the top {s.top_n} <InfoTip k="rotate_on_rank_dropout" />
              </label>
            )}
          </div>
        </details>
      </section>

      {/* MACD periods — ONE indicator shared by the entry filter AND the exit
          signal, so it lives in its own block (not owned by either) and appears
          whenever either MACD toggle above is on. */}
      {macdOn && (
        <section className="builder-sec">
          <h4 className="builder-head">MACD periods</h4>
          <p className="sec-sub">
            The shared MACD used by "Require bullish MACD" (entry) and "Exit when MACD turns bearish" (exit) above.
            Lower = a faster, less-laggy MACD.
          </p>
          <div className="param-grid">
            <Param label="MACD fast" tip="macd_fast">
              <NumberField step="1" min="1" value={macd.fast} onChange={(n) => setMacd("fast", n)} />
            </Param>
            <Param label="MACD slow" tip="macd_slow">
              <NumberField step="1" min="2" value={macd.slow} onChange={(n) => setMacd("slow", n)} />
            </Param>
            <Param label="MACD signal" tip="macd_signal">
              <NumberField step="1" min="1" value={macd.signal} onChange={(n) => setMacd("signal", n)} />
            </Param>
          </div>
        </section>
      )}

      {/* 5 — SIZING & RISK: how much, and the safety knobs */}
      <section className="builder-sec">
        <h4 className="builder-head">Sizing &amp; risk</h4>
        <p className="sec-sub">How much to commit, and the safety rails.</p>
        <div className="param-grid">
          <Param label="$ per trade" tip="sizing">
            <NumberField step="any" min="10" value={s.sizing_usd!} onChange={(n) => setS({ ...s, sizing_usd: n })} />
          </Param>
          <Param label="Sleeve budget ($)" tip="sleeve">
            <NumberField step="any" min="10" value={s.sleeve_usd!} onChange={(n) => setS({ ...s, sleeve_usd: n })} />
          </Param>
          <Param label="Max positions" tip="max_positions">
            <NumberField step="1" min="1" max="25" value={s.max_positions!}
              onChange={(n) => setS({ ...s, max_positions: n })} />
          </Param>
        </div>

        <div className="check-row">
          <label className="check">
            <input
              type="checkbox"
              checked={!!p.execution?.market_orders}
              onChange={(e) =>
                setS((cur) => ({
                  ...cur,
                  params: { ...cur.params!, execution: { market_orders: e.target.checked } },
                }))
              }
            />
            Buy &amp; sell at market price (allow fractional shares) <InfoTip k="market_fractional" />
          </label>
        </div>

        <details className="adv">
          <summary>Advanced — volatility stops &amp; sizing (ATR)</summary>
          <div className="param-grid">
            <Param label="ATR stop (× ATR, 0 = off)" tip="atr_stop">
              <NumberField step="0.1" min="0" max="20" value={atr.stop_mult}
                onChange={(n) => setAtr("stop_mult", n)} />
            </Param>
            <Param label="Risk $ per trade (0 = off)" tip="atr_risk">
              <NumberField step="any" min="0" value={atr.risk_usd} onChange={(n) => setAtr("risk_usd", n)} />
            </Param>
            <Param label="ATR period (days)" tip="atr_period">
              <NumberField step="1" min="2" max="100" value={atr.period} onChange={(n) => setAtr("period", n)} />
            </Param>
          </div>
          {s.asset_class === "stock" && (
            <div className="check-row">
              <label className="check">
                <input type="checkbox" checked={s.ignore_regime}
                  onChange={(e) => setS({ ...s, ignore_regime: e.target.checked })} />
                Ignore regime filter (not recommended) <InfoTip k="regime_filter" />
              </label>
            </div>
          )}
        </details>
      </section>

      <p className={`sleeve-readout${overAllocated ? " over" : ""}`}>
        All strategy sleeves total <strong>{money(totalSleeves)}</strong>
        {equity != null ? (
          <>
            {" "}
            of your <strong>{money(equity)}</strong> Alpaca equity.
            {overAllocated && (
              <>
                {" "}
                That's more than your balance — which is fine: sleeves may overlap on purpose, so
                whichever strategy trades first draws the shared cash, and the no-leverage rail caps
                total spending at your real equity. Nothing borrows.
              </>
            )}
          </>
        ) : (
          <> across all strategies. Connect Alpaca to compare against your live balance.</>
        )}
      </p>
      {allInSizing && (
        <p className="hint warn">
          <IconWarn className="icon-inline" /> <strong>All-in sizing — this strategy will stall after one loss.</strong> Your{" "}
          {money(sizing)} per trade is as large as the whole {money(sleeve)} sleeve, so only one position can ever open.
          And because the no-leverage rail caps spending at your real equity, a single losing trade drops you below one
          full position — every entry after that is blocked ("not enough funds") and the strategy silently stops trading.
          Set <strong>$ per trade</strong> to a fraction of the sleeve (e.g. {money(sleeve / 5)} for ~5 positions).
        </p>
      )}
      {oneShotSleeve && (
        <p className="hint warn">
          <IconWarn className="icon-inline" /> <strong>Only one position can be open at a time.</strong> Your sleeve budget ({money(sleeve)}) is less than
          twice your {money(sizing)} per trade, so a second buy would exceed the sleeve and be blocked — "Max positions"
          can't take effect. Backtests also stop trading once a losing streak leaves less cash than one full trade. Set
          the sleeve to a few times the per-trade size (e.g. {money(sizing * 5)} for ~5 concurrent positions).
        </p>
      )}
      {!allInSizing && !oneShotSleeve && effectiveCap > 0 && (
        <p className="hint">
          {atrSizingOn && <>ATR sizing varies $/trade with volatility, so this is approximate. </>}
          You'll hold at most <strong>{effectiveCap}</strong> position{effectiveCap === 1 ? "" : "s"} at once
          {bindersText ? <> — limited by {bindersText}.</> : "."}
        </p>
      )}
      {!allInSizing && !oneShotSleeve && maxPosNoOp && (
        <p className="hint">
          Your "Max positions" ({maxPos}) is higher than that, so it isn't the active limit here.
        </p>
      )}
      {!allInSizing && !oneShotSleeve && maxPosHoldsBack && (
        <p className="hint">
          "Max positions" ({maxPos}) holds you below what your capital allows ({capitalCap}), so about{" "}
          <strong>{money(parkedCash)}</strong> of the sleeve stays in cash by design. Raise it to {capitalCap} to use the
          full sleeve.
        </p>
      )}
      {tightSwingStop && (
        <p className="hint warn">
          <IconWarn className="icon-inline" /> <strong>Tight stop-loss ({stopPct}%) with swing mode on.</strong> Holding overnight but bailing on a{" "}
          {stopPct}% move means normal daily noise (most stocks swing more than that intraday) will stop you out almost
          immediately — usually at a small loss. Swing stops should be wider than the symbol's typical daily move (ATR),
          often 5–8%. For a stop this tight, set <strong>Trading style</strong> to <em>Intraday</em> instead.
        </p>
      )}
      {sleeveBelowHoldings && (
        <p className="hint">
          This is below the <strong>{money(heldExposure!)}</strong> this strategy currently holds. Nothing will be sold —
          your open positions keep running under their exit rules — but the strategy won't open any new positions until
          exits bring it back under {money(sleeve)}.
        </p>
      )}
      {vwapOnSwing && (
        <p className="hint warn">
          <IconWarn className="icon-inline" /> <strong>"Require price above VWAP" with swing mode on.</strong> VWAP is an{" "}
          <em>intraday</em> measure — it resets every session — so this rule forces the backtest onto intraday (1-hour /
          15-min) bars and doesn't fit a daily / rotation strategy whose signals (RSI, MACD, relative strength) are all
          daily. For a swing or rank-and-rotate strategy, turn it off; keep it only for intraday fast-mover strategies.
        </p>
      )}
      {error && <div className="error">{error}</div>}
      <div className="toolbar">
        <button disabled={busy}>
          {busy ? "Saving…" : s.id ? "Save (creates new config version)" : "Create strategy"}
        </button>
        {/* Full-size like Save (same height/baseline), ghost so Save stays the
            one primary action in the row. */}
        <button type="button" className="btn-ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        {/* These SAVE FIRST, then jump. Both destinations load the strategy from
            the server, so navigating without saving quietly tested the previous
            version — you'd tweak a stop, hit Backtest, and grade the old config.
            Works for a new strategy too: saving is what creates it. */}
        <button
          type="button"
          className="btn-ghost"
          disabled={busy}
          onClick={() => saveAndGo("backtest")}
          title="Save this strategy, then backtest exactly what you just saved"
        >
          Save &amp; backtest →
        </button>
        <button
          type="button"
          className="btn-ghost"
          disabled={busy}
          onClick={() => saveAndGo("optimizer")}
          title="Save this strategy, then optimize exactly what you just saved"
        >
          Save &amp; optimize →
        </button>
      </div>
    </form>
  );
}

export default function Strategies() {
  const [rows, setRows] = useState<StrategyRow[] | null>(null);
  const [presets, setPresets] = useState<Record<string, Preset>>({});
  const [baskets, setBaskets] = useState<Basket[]>([]);
  const [editing, setEditing] = useState<Partial<StrategyRow> | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [equity, setEquity] = useState<number | null>(null);
  const editorRef = useRef<HTMLDivElement>(null);

  // The editor opens at the TOP of the page — scroll it into view so clicking
  // Edit far down the list doesn't look like nothing happened.
  useEffect(() => {
    if (editing) editorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [editing]);

  // Switching to edit a different strategy (or starting a new one) mid-edit
  // discards the open form — say so instead of silently eating the changes.
  // Leaving the page from a row while the editor is open would drop whatever is
  // in the form — same silent loss the "Save & backtest" buttons now prevent, one
  // click away. Ask first.
  function leaveFor(tab: string, strategyId: number) {
    if (editing) {
      const name = editing.name || "the open strategy";
      if (!window.confirm(`You're still editing ${name} — leave anyway? Unsaved changes will be lost.`)) return;
    }
    requestNav({ tab, strategyId });
  }

  function startEdit(target: Partial<StrategyRow>) {
    if (editing && editing.id !== target.id) {
      const name = editing.name || "the current strategy";
      if (!window.confirm(`You're still editing ${name} — switch anyway? Unsaved changes will be lost.`)) return;
    }
    setEditing(target);
  }

  const refresh = useCallback(() => {
    getStrategies().then(setRows).catch((e: Error) => setNote(e.message));
  }, []);

  const refreshBaskets = useCallback(() => {
    getBaskets().then(setBaskets).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    getPresets().then(setPresets);
    getBaskets().then(setBaskets).catch(() => setBaskets([]));
    getStatus()
      .then((st) => setEquity(st.broker ? Number(st.broker.equity) : null))
      .catch(() => setEquity(null));
    // Another page (e.g. the backtest's "Edit this strategy") jumped here with a
    // target in tow — open it in the editor straight away (auto-scrolls).
    const pre = consumeNav()?.strategyId;
    if (pre != null) {
      getStrategies()
        .then((all) => {
          const target = all.find((r) => r.id === pre);
          if (target) setEditing(target);
        })
        .catch(() => {});
    }
  }, [refresh]);

  const basketName = (id: number | null) => baskets.find((b) => b.id === id)?.name ?? `#${id}`;

  async function toggle(row: StrategyRow) {
    await toggleStrategy(row.id);
    refresh();
  }

  async function remove(row: StrategyRow) {
    try {
      await deleteStrategy(row.id);
      refresh();
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  const live = (rows ?? []).filter((r) => r.enabled);
  const paused = (rows ?? []).filter((r) => !r.enabled);

  function strategyCard(r: StrategyRow) {
    // Compact by default: one folded row per strategy (name, state, a one-line
    // summary); the full detail — holdings, ranking, last run, actions — shows
    // only when the row is expanded. Keeps a long list scannable.
    const universeShort =
      r.universe === "basket"
        ? `basket "${basketName(r.basket_id)}" top ${r.top_n}`
        : r.universe === "custom"
          ? `${r.symbols.length} symbol${r.symbols.length === 1 ? "" : "s"}`
          : r.universe;
    return (
      <details className={`card fold strat-fold${r.enabled ? " card-live" : ""}`} key={r.id}>
        <summary>
          <div className="strat-head">
            <h3>
              {r.name}{" "}
              <span className={`pill ${r.enabled ? "ok pill-live" : "muted"}`}>
                {r.enabled ? "● ENABLED" : "disabled"}
              </span>
            </h3>
            <span className="hint">
              {r.asset_class} · {universeShort} · {r.swing_mode ? "swing" : "intraday"} · $
              {r.sleeve_usd.toLocaleString()} sleeve
              {(r.open_trades ?? 0) > 0 ? ` · ${r.open_trades} open` : ""}
            </span>
          </div>
        </summary>
        <dl>
          <dt>Trades</dt>
          <dd>
            {r.asset_class} ·{" "}
            {r.universe === "basket"
              ? `basket "${basketName(r.basket_id)}" · top ${r.top_n} by ${RANK_LABELS[r.rank_by]}`
              : r.universe === "custom"
              ? `custom: ${r.symbols.join(", ") || "(none)"}`
              : r.universe}{" "}
            · {r.swing_mode ? "swing" : "intraday"}
          </dd>
          <dt>Entry</dt>
          <dd>
            +{r.params.entry.min_day_gain_pct}% day{r.params.entry.require_above_vwap ? ", above VWAP" : ""}
          </dd>
          <dt>Exit</dt>
          <dd>
            trail {r.params.exit.trailing_stop_pct}% · stop {r.params.exit.stop_loss_pct}%
            {r.params.exit.take_profit_pct ? ` · target ${r.params.exit.take_profit_pct}%` : ""}
          </dd>
          <dt>Sizing</dt>
          <dd>
            ${r.sizing_usd} / trade, ${r.sleeve_usd} sleeve, max {r.max_positions}
          </dd>
          <dt>Config</dt>
          <dd>
            v{r.version} · {r.open_trades ?? 0} open trade(s)
          </dd>
        </dl>
        {(r.open_trades ?? 0) > 0 && <HoldingsView strategyId={r.id} count={r.open_trades ?? 0} />}
        {(r.rank_enabled || r.universe === "basket") && <RankingView strategyId={r.id} />}
        <LastRunView strategyId={r.id} />
        <div className="card-actions">
          <button
            className={`small btn-icon ${r.enabled ? "btn-pause" : "btn-enable"}`}
            onClick={() => toggle(r)}
            title={r.enabled ? "Stop this strategy from opening new trades" : "Arm this strategy (it trades once the engine is on)"}
          >
            {r.enabled ? <IconPause /> : <IconPlay />}
            {r.enabled ? "Pause" : "Enable"}
          </button>
          <button className="small btn-icon btn-ghost" onClick={() => startEdit(r)} title="Edit this strategy's settings">
            <IconEdit />
            Edit
          </button>
          <button
            className="small btn-icon btn-ghost"
            onClick={() => leaveFor("optimizer", r.id)}
            title="Search better settings for this strategy (opens the Optimizer)"
          >
            <IconOptimize />
            Optimize
          </button>
          <button className="small btn-icon danger" onClick={() => remove(r)} title="Delete this strategy permanently">
            <IconDelete />
            Delete
          </button>
        </div>
      </details>
    );
  }

  return (
    <>
      <div className="toolbar">
        <h2>Strategies</h2>
        <button className="small" onClick={() => startEdit(EMPTY)}>
          + New strategy
        </button>
      </div>
      {note && (
        <div className="card note" onClick={() => setNote(null)}>
          {note}
        </div>
      )}
      {editing && (
        <div ref={editorRef}>
          {/* key forces a fresh Editor when the TARGET changes — its form state
              seeds from `initial` once, so without the remount, clicking Edit on
              a different strategy silently kept showing the old one. */}
          <Editor
            key={editing.id ?? "new"}
            initial={editing}
            presets={presets}
            baskets={baskets}
            allStrategies={rows ?? []}
            equity={equity}
            onSaved={() => {
              setEditing(null);
              refresh();
            }}
            onCancel={() => setEditing(null)}
            onBasketsChanged={refreshBaskets}
          />
        </div>
      )}
      {!rows ? (
        <div className="card">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="card">
          <p className="hint">
            No strategies yet. Create one from a preset — it starts <strong>disabled</strong> and trades nothing until
            you enable it AND turn the engine on (shadow mode first).
          </p>
        </div>
      ) : (
        <>
          {live.length > 0 && (
            <section className="strat-section">
              <h3 className="section-head">
                Enabled <span className="section-count">{live.length}</span>
                <span className="section-sub">armed — they trade once the engine is on</span>
              </h3>
              <div className="grid">{live.map(strategyCard)}</div>
            </section>
          )}
          {paused.length > 0 && (
            <section className="strat-section">
              <h3 className="section-head section-head-muted">
                Disabled / drafts <span className="section-count">{paused.length}</span>
              </h3>
              <div className="grid">{paused.map(strategyCard)}</div>
            </section>
          )}
        </>
      )}
    </>
  );
}
