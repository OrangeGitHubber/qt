import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import {
  IconCrypto,
  IconDelete,
  IconEdit,
  IconOptimize,
  IconPause,
  IconPlay,
  IconStock,
  IconWarn,
} from "../components/icons";
import { fmtClock, fmtDateTime, zoneAbbr } from "../lib/datetime";
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
  allow_concurrent_symbol: false,
  notes: "",
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

// How often the open panels and the list re-read the server. 30s is a
// compromise: the engine ticks about once a minute, so anything faster mostly
// re-fetches an unchanged answer, and anything slower means watching a strategy
// trade with a page that lags behind the trade.
const POLL_MS = 30_000;

/** Lazily-loaded panel data that stays live for as long as its disclosure is
 * open, and reports when it was last read.
 *
 * Shared by both panels on purpose. They were written separately and drifted:
 * one refetched on every expand, the other cached its first answer forever, and
 * nothing on screen distinguished them — so the honest-looking one and the stale
 * one sat next to each other looking identical. One implementation cannot drift.
 */
function useLivePanel<T>(fetcher: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [at, setAt] = useState<Date | null>(null);
  const [open, setOpen] = useState(false);
  // A ref, not the loading flag: `load` must keep a stable identity for the
  // interval below, and reading state inside it would capture a stale value.
  const busy = useRef(false);

  const load = useCallback(() => {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    setErr(null);
    fetcher()
      .then((d) => {
        setData(d);
        setAt(new Date());
      })
      .catch((e: Error) => setErr(e.message))
      .finally(() => {
        busy.current = false;
        setLoading(false);
      });
  }, [fetcher]);

  useEffect(() => {
    if (!open) return;
    const id = setInterval(() => {
      // Nothing to see on a hidden tab, and a background tab quietly polling
      // the broker for hours is a cost with no reader.
      if (!document.hidden) load();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [open, load]);

  function onToggle(e: React.SyntheticEvent<HTMLDetailsElement>) {
    const isOpen = (e.target as HTMLDetailsElement).open;
    setOpen(isOpen);
    if (isOpen) load();
  }

  return { data, loading, err, at, onToggle, load };
}

/** When the panel beside it last read the server. Staleness you can see beats
 * staleness you have to guess at — especially while a strategy is trading. */
function AsOf({ at, loading }: { at: Date | null; loading: boolean }) {
  if (loading) return <span className="hint as-of">refreshing…</span>;
  if (!at) return null;
  return (
    <span className="hint as-of">
      {/* `at` is a local clock reading, but it is read next to server times in
          the panel beside it, so it has to be on the same clock as those. */}
      as of {fmtClock(at)} {zoneAbbr(at)}
    </span>
  );
}

// Expandable per-strategy holdings: the open positions this strategy owns right
// now, with best-effort live unrealized P&L. Lazy-loads on first expand.
function HoldingsView({ strategyId, count }: { strategyId: number; count: number }) {
  const fetcher = useCallback(() => getStrategyHoldings(strategyId), [strategyId]);
  const { data, loading, err, at, onToggle } = useLivePanel<StrategyHoldings>(fetcher);

  const money = (n: number) => `${n < 0 ? "−" : ""}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

  return (
    <details className="holdings" onToggle={onToggle}>
      <summary>
        Holdings ({count}) <AsOf at={at} loading={loading} />
      </summary>
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
  const fetcher = useCallback(() => getStrategyLastRun(strategyId), [strategyId]);
  const { data, loading, err, at, onToggle } = useLivePanel<StrategyLastRun>(fetcher);

  const badge = (d: string) =>
    d === "bought" ? "up" : d === "blocked" ? "down" : "";

  return (
    <details className="lastrun" onToggle={onToggle}>
      <summary>
        Last run — why it did / didn't buy <AsOf at={at} loading={loading} />
      </summary>
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
            Ran <strong>{fmtDateTime(data.ran_at)}</strong>
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
                    {/* Where each name placed in the ranking. The engine buys
                        strictly best-first, so a buy far down the list means
                        everything above it was already held or failed a rule. */}
                    <th>Rank</th>
                    <th>Day</th>
                    <th>Decision</th>
                    <th className="why">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {data.candidates.map((c, i) => (
                    <tr key={i}>
                      <td>{c.symbol}</td>
                      <td className="hint">
                        {c.rank == null ? "—" : `#${c.rank}${c.rank_of ? ` / ${c.rank_of}` : ""}`}
                      </td>
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

  // What's actually changed since this editor opened. Drives both the button
  // label and the line above it: a button that promises "creates new config
  // version" while you've only typed a note is telling you something untrue.
  //
  // Keys sorted before comparing — a spread can reorder them, and a reorder is
  // not an edit.
  const changed = useMemo(() => {
    const stable = (v: unknown): unknown => {
      if (Array.isArray(v)) return v.map(stable);
      if (v && typeof v === "object") {
        return Object.fromEntries(
          Object.keys(v as object).sort().map((k) => [k, stable((v as Record<string, unknown>)[k])]),
        );
      }
      return v;
    };
    // `enabled` is toggled from the list, not here; version/open_trades are the
    // server's. None of them are edits to the config.
    const config = (x: Partial<StrategyRow>) => {
      const { notes: _n, version: _v, open_trades: _o, enabled: _e, ...rest } = x;
      return JSON.stringify(stable(rest));
    };
    return {
      config: config(s) !== config(initial),
      notes: (s.notes ?? "") !== (initial.notes ?? ""),
    };
  }, [s, initial]);

  const p = s.params!;
  // Market orders have no limit price, so the marketable-limit slippage buffers
  // (entry + exit) don't apply — grey them out when market mode is on.
  const marketMode = !!p.execution?.market_orders;
  // The ATR stop REPLACES the fixed stop-loss (see evaluate_exit): the hard stop
  // becomes stop_mult x the symbol's own daily ATR%. Easy to miss, because the
  // ATR knob lives in a collapsed Advanced section while the stop-loss sits in
  // plain sight — so the field you're reading isn't the rule that's running.
  const atrStopOn = Number(p.atr?.stop_mult || 0) > 0;
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
          <Param
            label={atrStopOn ? "Stop-loss (%) — replaced by the ATR stop" : "Stop-loss (%) — required"}
            tip="stop_loss"
          >
            <NumberField step="0.1" min="0.1" value={p.exit.stop_loss_pct}
              onChange={(n) => setExit("stop_loss_pct", n)} disabled={atrStopOn} />
          </Param>
          <Param label="Take-profit (%, 0 = off)" tip="take_profit">
            <NumberField step="0.1" min="0" value={p.exit.take_profit_pct}
              onChange={(n) => setExit("take_profit_pct", n)} />
          </Param>
        </div>
        {atrStopOn && (
          <p className="field-help">
            <strong>Your hard stop is {Number(p.atr?.stop_mult)}x ATR, not {p.exit.stop_loss_pct}%.</strong> With the
            ATR stop switched on (Advanced &rarr; volatility stops &amp; sizing), the stop level is{" "}
            {Number(p.atr?.stop_mult)} x each symbol's own <strong>daily ATR</strong>, recalculated every tick — so it
            sits wider on a volatile name and tighter on a calm one. A symbol moving 1% a day would stop out around{" "}
            {(Number(p.atr?.stop_mult) * 1).toFixed(1)}%; one moving 4% a day, around{" "}
            {(Number(p.atr?.stop_mult) * 4).toFixed(1)}%.
            <br />
            The fixed {p.exit.stop_loss_pct}% above is kept as the <strong>fallback</strong>, used only when ATR can't
            be worked out (not enough history, or a data blip) — a position is never left without a stop. To go back to
            a plain percentage, set the ATR stop to 0.
            <br />
            Your <strong>trailing stop stays active either way</strong> — the two are independent, and whichever
            triggers first sells.
          </p>
        )}

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
            {/* Crypto is ALWAYS fractional — you buy 0.0016 BTC whether this is
                on or off — so promising "allow fractional" there describes
                nothing. What the toggle still changes for crypto is the ORDER
                TYPE, and that's what the label should say. */}
            {s.asset_class === "crypto" ? (
              <>Buy &amp; sell at market price <InfoTip k="market_fractional" /></>
            ) : (
              <>Buy &amp; sell at market price (allow fractional shares) <InfoTip k="market_fractional" /></>
            )}
          </label>
        </div>
        {s.asset_class === "crypto" && (
          <p className="hint">
            Crypto is always bought in fractions, on or off — this only chooses the <strong>order type</strong>:
            a market order fills immediately at whatever price is there, versus a price-protected limit that can sit
            unfilled on a fast move.
          </p>
        )}

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
          <div className="check-row">
            <label className="check">
              <input type="checkbox" checked={!!s.allow_concurrent_symbol}
                onChange={(e) => setS({ ...s, allow_concurrent_symbol: e.target.checked })} />
              Let this strategy hold a symbol another strategy already holds{" "}
              <InfoTip k="concurrent_symbol" />
            </label>
          </div>
          {s.allow_concurrent_symbol && (
            <p className="hint">
              You'll then own the name in two places at once, so your real exposure to it is the sum of both
              positions. The wash-sale guard and the cooldown after a loss stay <strong>account-wide</strong> either
              way — they protect the whole portfolio, not one strategy.
            </p>
          )}
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
      {/* Your own notes. Last field before saving, because it's the one you write
          AFTER deciding everything above it. Editing it alone doesn't create a
          config version — a note changes no trading behaviour. */}
      <label className="notes-field">
        <span className="field-cap">Notes — for you, not the engine</span>
        <textarea
          rows={5}
          maxLength={10000}
          value={s.notes ?? ""}
          placeholder="What you're testing and why, what a backtest showed, what to try next — e.g. 1.9x ATR beat SPY over 500d but lost to buy & hold; try a wider trailing stop."
          onChange={(e) => setS({ ...s, notes: e.target.value })}
        />
        <span className="hint">
          Never read by the engine, and saving a note on its own won't create a new config version.
        </span>
      </label>
      {error !== null && <div className="error">{error}</div>}
      {/* The consequence lives here, not in the button. A button should say what
          it DOES; "Save (creates new config version)" was a sentence wearing a
          button, and its length is what made the row jump when it became
          "Saving…". This line updates as you type, so you know before clicking. */}
      {s.id != null && (
        <p className="hint save-note">
          {changed.config
            ? `Saving creates a new config version. Trades already made keep pointing at v${s.version ?? "?"}, so past results stay honest.`
            : changed.notes
              ? "Only your notes changed — no new config version."
              : "No changes yet."}
        </p>
      )}
      <div className="toolbar">
        <button disabled={busy}>{busy ? "Saving…" : s.id ? "Save" : "Create strategy"}</button>
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

// ---- Optimizer lineage ---------------------------------------------------
// The list's worst problem was never width. A parameter search leaves its output
// BESIDE its input, so "… v2 with ATR", "… v2 with ATR (search draft)" and
// "… (search draft) (search draft)" are one idea wearing six names — six full
// rows for one thing you own. The optimizer already stamps optimized_from_id on
// everything it creates (see Optimizer.tsx), so the page can put descendants
// back under the strategy they were searched from instead of listing them as
// strangers that happen to share a prefix.
interface Family {
  root: StrategyRow;
  members: StrategyRow[]; // root first, then descendants by generation, then id
}

/** Group every strategy under its oldest surviving ancestor, and record how many
 *  searches deep each one sits. One upward walk per row, stopping on a missing
 *  parent (deleted mid-chain — the orphan becomes its own root rather than
 *  vanishing off the page) and on a repeated id (a hand-edited a→b→a cycle would
 *  otherwise spin forever, the same guard the Optimizer's walk uses). */
function buildFamilies(rows: StrategyRow[]): { families: Family[]; depth: Map<number, number> } {
  const byId = new Map(rows.map((r) => [r.id, r]));
  const depth = new Map<number, number>();
  const kin = new Map<number, StrategyRow[]>();
  for (const r of rows) {
    let cur = r;
    let d = 0;
    const seen = new Set<number>([r.id]);
    while (cur.optimized_from_id != null) {
      const parent = byId.get(cur.optimized_from_id);
      if (!parent || seen.has(parent.id)) break;
      seen.add(parent.id);
      cur = parent;
      d += 1;
    }
    depth.set(r.id, d);
    const list = kin.get(cur.id);
    if (list) list.push(r);
    else kin.set(cur.id, [r]);
  }
  const families = [...kin.entries()].map(([rootId, members]) => ({
    root: byId.get(rootId)!,
    members: members.sort((a, b) => depth.get(a.id)! - depth.get(b.id)! || a.id - b.id),
  }));
  return { families, depth };
}

type StatusFilter = "all" | "enabled" | "paused";
type AssetFilter = "all" | "stock" | "crypto";
type SortKey = "state" | "name" | "open" | "new";

const SORT_LABELS: Record<SortKey, string> = {
  state: "Enabled first",
  name: "Name (A–Z)",
  open: "Open positions",
  new: "Newest first",
};

export default function Strategies() {
  const [rows, setRows] = useState<StrategyRow[] | null>(null);
  const [presets, setPresets] = useState<Record<string, Preset>>({});
  const [baskets, setBaskets] = useState<Basket[]>([]);
  const [editing, setEditing] = useState<Partial<StrategyRow> | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [equity, setEquity] = useState<number | null>(null);
  const editorRef = useRef<HTMLDivElement>(null);

  // List controls. Filtering beats sectioning here: the old Enabled / Disabled
  // split is the very thing that made lineage impossible to show, because a
  // family is normally one enabled parent plus a pile of disabled drafts and
  // the split tore it in half. State is now a FILTER over one list, and
  // "enabled first" is the default sort instead.
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [asset, setAsset] = useState<AssetFilter>("all");
  const [sort, setSort] = useState<SortKey>("state");
  const [grouped, setGrouped] = useState(true);
  // Which families have their search variants unfolded, keyed by root id.
  const [openFamilies, setOpenFamilies] = useState<Set<number>>(() => new Set());

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

  // The list carries each strategy's open-position count and its enabled pill,
  // and both move without you touching anything — a strategy opens a position
  // while you are reading the page. Until now the list was only re-read on mount
  // and after your own edits, so those numbers silently aged.
  //
  // Re-sorting under the cursor is the risk this trades against, and it is
  // small: only the "Open positions" order can reshuffle, and only when a
  // position actually opens or closes, which is the event you came to see.
  // Expanded rows keep their state — React reuses the DOM node for a given id.
  useEffect(() => {
    const id = setInterval(() => {
      if (!document.hidden) refresh();
    }, POLL_MS);
    return () => clearInterval(id);
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

  // One pass over the list: group into optimizer families, apply the filters to
  // the MEMBERS (a family survives while anything in it matches), then order.
  const view = useMemo(() => {
    const all = rows ?? [];
    const q = query.trim().toLowerCase();
    const keep = (r: StrategyRow) => {
      if (status !== "all" && r.enabled !== (status === "enabled")) return false;
      if (asset !== "all" && r.asset_class !== asset) return false;
      if (!q) return true;
      // Symbols too: "why is NVDA being traded" is a list question, and the
      // strategy holding it rarely has NVDA in its name.
      return r.name.toLowerCase().includes(q) || r.symbols.some((s) => s.toLowerCase().includes(q));
    };

    const { families, depth } = buildFamilies(all);
    const groups = (grouped ? families : all.map((r) => ({ root: r, members: [r] })))
      .map((f) => ({ root: f.root, members: f.members.filter(keep) }))
      .filter((f) => f.members.length > 0);

    // A family sorts by its strongest member, not its root: a family whose
    // 4th-generation draft is the enabled one is still a live family.
    const best = (f: Family, pick: (m: StrategyRow) => number) => Math.max(...f.members.map(pick));
    groups.sort((a, b) => {
      const byName = a.root.name.localeCompare(b.root.name);
      if (sort === "name") return byName;
      if (sort === "open") return best(b, (m) => m.open_trades ?? 0) - best(a, (m) => m.open_trades ?? 0) || byName;
      if (sort === "new") return best(b, (m) => m.id) - best(a, (m) => m.id);
      return best(b, (m) => (m.enabled ? 1 : 0)) - best(a, (m) => (m.enabled ? 1 : 0)) || byName;
    });

    return {
      groups,
      depth,
      byId: new Map(all.map((r) => [r.id, r])),
      shown: groups.reduce((n, f) => n + f.members.length, 0),
      total: all.length,
      liveCount: all.filter((r) => r.enabled).length,
    };
  }, [rows, query, status, asset, sort, grouped]);

  const filtered = query.trim() !== "" || status !== "all" || asset !== "all";
  function clearFilters() {
    setQuery("");
    setStatus("all");
    setAsset("all");
  }

  function toggleFamily(rootId: number) {
    setOpenFamilies((cur) => {
      const next = new Set(cur);
      if (!next.delete(rootId)) next.add(rootId);
      return next;
    });
  }

  function strategyRow(r: StrategyRow, variant = false) {
    // Folded: ONE full-width line whose fields land in fixed columns, so 23 of
    // them read as a table you can scan down (every sleeve, every open count
    // aligned) instead of 23 paragraphs. Expanded: the whole page width, which
    // is what the "why it did / didn't buy" trace needed all along — in a 270px
    // card its Why column wrapped to one word per line.
    const universeShort =
      r.universe === "basket"
        ? `basket "${basketName(r.basket_id)}" top ${r.top_n}`
        : r.universe === "custom"
          ? `${r.symbols.length} symbol${r.symbols.length === 1 ? "" : "s"}`
          : r.universe;
    const gen = (view.depth.get(r.id) ?? 0) + 1;
    const parent = r.optimized_from_id != null ? view.byId.get(r.optimized_from_id) : undefined;
    const AssetIcon = r.asset_class === "crypto" ? IconCrypto : IconStock;
    const open = r.open_trades ?? 0;
    return (
      <details className={`strat-row${r.enabled ? " card-live" : ""}${variant ? " strat-variant" : ""}`} key={r.id}>
        <summary>
          <span className="sr-name">
            {variant && <span className="sr-branch" aria-hidden>↳</span>}
            <span className="sr-title">{r.name}</span>
            <span className={`pill ${r.enabled ? "ok pill-live" : "muted"}`}>
              {r.enabled ? "● ENABLED" : "disabled"}
            </span>
            {gen > 1 && (
              <span
                className="pill muted sr-gen"
                title={`Generation ${gen} — produced by a parameter search${
                  parent ? ` from "${parent.name}"` : ""
                }${r.optimized_days ? ` over ${r.optimized_days} days` : ""}`}
              >
                gen {gen}
              </span>
            )}
          </span>
          <span className="sr-meta">
            <AssetIcon className="icon-inline" /> {r.asset_class} · {universeShort} ·{" "}
            {r.swing_mode ? "swing" : "intraday"}
          </span>
          <span className="sr-num">
            ${r.sleeve_usd.toLocaleString()}
            <span className="sr-cap"> sleeve</span>
          </span>
          {/* An open position is the one number worth interrupting a scan for —
              it means real money is in the market under this config. */}
          <span className={`sr-num${open > 0 ? " sr-open" : ""}`}>
            {open > 0 ? (
              <>
                {open}
                <span className="sr-cap"> open</span>
              </>
            ) : (
              <span className="sr-cap">—</span>
            )}
          </span>
          <span className="sr-num sr-cap">v{r.version}</span>
        </summary>
        <div className="strat-body">
          {/* Each dt/dd pair is wrapped so the whole set can flow into columns —
              the label-above-value shape survives a 4-column layout, where the
              old auto/1fr grid stretched one value across the full page. */}
          <dl className="strat-dl">
            <div>
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
            </div>
            <div>
              <dt>Entry</dt>
              <dd>
                +{r.params.entry.min_day_gain_pct}% day{r.params.entry.require_above_vwap ? ", above VWAP" : ""}
              </dd>
            </div>
            <div>
              <dt>Exit</dt>
              <dd>
                trail {r.params.exit.trailing_stop_pct}% · stop{" "}
                {Number(r.params.atr?.stop_mult || 0) > 0
                  ? `${Number(r.params.atr!.stop_mult)}×ATR`
                  : `${r.params.exit.stop_loss_pct}%`}
                {r.params.exit.take_profit_pct ? ` · target ${r.params.exit.take_profit_pct}%` : ""}
              </dd>
            </div>
            <div>
              <dt>Sizing</dt>
              <dd>
                ${r.sizing_usd} / trade, ${r.sleeve_usd} sleeve, max {r.max_positions}
              </dd>
            </div>
            <div>
              <dt>Config</dt>
              <dd>
                v{r.version} · {r.open_trades ?? 0} open trade(s)
              </dd>
            </div>
            {/* Where this config came from. The row only has room for "gen 3";
                the answer you actually want — searched from WHAT, over how long —
                is the thing that tells you whether the number is trustworthy. */}
            {gen > 1 && (
              <div>
                <dt>Lineage</dt>
                <dd>
                  generation {gen}
                  {parent ? ` · searched from "${parent.name}"` : " · parent deleted"}
                  {r.optimized_days ? ` over ${r.optimized_days} days` : ""}
                </dd>
              </div>
            )}
          </dl>
          {/* Readable without opening the editor — notes you have to dig for are
              notes you stop writing. pre-wrap keeps the line breaks you typed. */}
          {r.notes && (
            <>
              <span className="field-cap">Your notes</span>
              <p className="hint strat-notes">{r.notes}</p>
            </>
          )}
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
            {/* Same unsaved-changes guard as Optimize: leaving a half-edited form
                behind for a page that reloads the strategy from the server would
                drop the edits silently. */}
            <button
              className="small btn-ghost"
              onClick={() => leaveFor("backtest", r.id)}
              title="Test this strategy against history (opens Backtest)"
            >
              Backtest
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
        </div>
      </details>
    );
  }

  // A family renders as its LIVE members plus a fold holding the rest. Anything
  // enabled stays a top-level row on purpose: a strategy that trades real money
  // must never be two clicks away because it happens to be someone's descendant.
  // With nothing enabled the root stands in, so a family is never headless.
  function familyBlock(f: Family) {
    if (f.members.length === 1) return strategyRow(f.members[0]);
    const enabled = f.members.filter((m) => m.enabled);
    const shown = enabled.length > 0 ? enabled : [f.members[0]];
    const tucked = f.members.filter((m) => !shown.includes(m));
    const open = openFamilies.has(f.root.id);
    return (
      <div className="strat-family" key={`fam-${f.root.id}`}>
        {shown.map((m) => strategyRow(m))}
        {tucked.length > 0 && (
          <>
            <button
              type="button"
              className="fam-toggle"
              aria-expanded={open}
              onClick={() => toggleFamily(f.root.id)}
            >
              {open ? "▾" : "▸"} {tucked.length} more from parameter searches on this
            </button>
            {open && <div className="fam-kids">{tucked.map((m) => strategyRow(m, true))}</div>}
          </>
        )}
      </div>
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
          <div className="strat-controls">
            <input
              className="strat-search"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search name or symbol…"
              aria-label="Search strategies by name or symbol"
            />
            <div className="seg" role="group" aria-label="Filter by state">
              {(["all", "enabled", "paused"] as StatusFilter[]).map((s) => (
                <button
                  key={s}
                  className={status === s ? "active" : ""}
                  aria-pressed={status === s}
                  onClick={() => setStatus(s)}
                >
                  {s === "all" ? "All" : s === "enabled" ? "Enabled" : "Paused"}
                </button>
              ))}
            </div>
            <div className="seg" role="group" aria-label="Filter by asset class">
              {(["all", "stock", "crypto"] as AssetFilter[]).map((a) => (
                <button
                  key={a}
                  className={asset === a ? "active" : ""}
                  aria-pressed={asset === a}
                  onClick={() => setAsset(a)}
                >
                  {a === "all" ? "All" : a === "stock" ? "Stocks" : "Crypto"}
                </button>
              ))}
            </div>
            <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)} aria-label="Sort strategies">
              {(Object.keys(SORT_LABELS) as SortKey[]).map((k) => (
                <option key={k} value={k}>
                  {SORT_LABELS[k]}
                </option>
              ))}
            </select>
            {/* An escape hatch, not a preference to fiddle with: grouping is a
                claim about how these rows relate, and you should be able to see
                the raw list when you doubt it. */}
            <label className="check" title="Fold each strategy's optimizer descendants under it">
              <input type="checkbox" checked={grouped} onChange={(e) => setGrouped(e.target.checked)} />
              Group search variants
            </label>
          </div>
          <p className="hint strat-summary">
            {filtered ? `${view.shown} of ${view.total}` : `${view.total}`} strategies
            {grouped && view.groups.length !== view.shown ? ` in ${view.groups.length} groups` : ""} ·{" "}
            <strong>{view.liveCount} enabled</strong> — armed, they trade once the engine is on.
          </p>
          {view.groups.length === 0 ? (
            <div className="card">
              <p className="hint">
                No strategy matches these filters.{" "}
                <button className="small btn-ghost" onClick={clearFilters}>
                  Clear filters
                </button>
              </p>
            </div>
          ) : (
            <div className="strat-list">{view.groups.map(familyBlock)}</div>
          )}
        </>
      )}
    </>
  );
}
