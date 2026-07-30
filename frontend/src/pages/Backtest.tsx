import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  Basket,
  BacktestResult,
  getBaskets,
  getStrategies,
  PortfolioBacktestResult,
  runBacktest,
  runPortfolioBacktest,
  StrategyRow,
} from "../api";
import InfoTip from "../components/InfoTip";
import LineChart, { ChartMarker, DayHolding } from "../components/LineChart";
import NumberField from "../components/NumberField";
import { IconEdit, IconWarn } from "../components/icons";
import { consumeNav, requestNav } from "../lib/nav";

type TradeEvent = {
  at: string; // ISO timestamp — drives ordering
  action: "Bought" | "Sold";
  symbol: string;
  price: number;
  qty?: number;
  pnl?: number | null;
  reason: string;
};

function Stat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

// Which side of a head-to-head metric wins? true = A, false = B, null = tie/n-a.
function cmp(a: number | null | undefined, b: number | null | undefined, higherBetter: boolean): boolean | null {
  if (a == null || b == null || a === b) return null;
  return higherBetter ? a > b : a < b;
}
function pct(v: number | null | undefined): string {
  return v != null ? `${v}%` : "—";
}

// A strategy's backtest universe, resolved READ-ONLY from its own config — the
// backtest tests the strategy's universe, it isn't picked here. Basket → its
// members; custom → its symbol list; scanner → replayed risers (no fixed list);
// watchlist/both → the asset-class watchlist.
function resolveUniverse(
  strat: StrategyRow | undefined,
  baskets: Basket[],
): { symbols: string[]; label: string; scannerReplay: boolean } {
  if (!strat) return { symbols: [], label: "", scannerReplay: false };
  if (strat.universe === "basket" && strat.basket_id != null) {
    const b = baskets.find((x) => x.id === strat.basket_id);
    const symbols = b
      ? b.symbols.filter((m) => m.asset_class === strat.asset_class).map((m) => m.symbol).slice(0, 50)
      : [];
    return { symbols, label: b ? `basket “${b.name}”` : "basket", scannerReplay: false };
  }
  if (strat.universe === "custom") {
    return { symbols: (strat.symbols ?? []).slice(0, 50), label: "specific symbols", scannerReplay: false };
  }
  if (strat.universe === "scanner") {
    return { symbols: [], label: "scanner — today’s risers (replayed)", scannerReplay: true };
  }
  return { symbols: [], label: "your watchlist", scannerReplay: false }; // watchlist | both
}

// Which bar size a strategy's signals demand: VWAP → intraday; MACD/RSI → daily.
function stratWantsIntraday(s: StrategyRow): boolean {
  return !!s.params?.entry.require_above_vwap;
}
function stratWantsDaily(s: StrategyRow): boolean {
  const e = s.params?.entry;
  const x = s.params?.exit;
  if (e?.require_above_vwap) return false;
  return (
    !!e?.require_macd_bullish ||
    !!x?.exit_on_macd_bearish ||
    (e?.rsi_min ?? 0) > 0 ||
    (e?.rsi_max ?? 0) > 0 ||
    (x?.exit_rsi_above ?? 0) > 0
  );
}

// A price-triggered exit — stop-loss, trailing stop or take-profit. These are
// the rules a once-a-day daily replay simply cannot simulate.
function stratHasPriceExit(s: StrategyRow): boolean {
  const x = s.params?.exit;
  return (x?.stop_loss_pct ?? 0) > 0 || (x?.trailing_stop_pct ?? 0) > 0 || (x?.take_profit_pct ?? 0) > 0;
}

// MIXED RESOLUTION: daily signals AND a price-triggered exit. One bar stream
// can't serve both (daily = correct signals + fake stops; intraday = correct
// stops + twitchy signals), so the backend replays 15-minute bars while taking
// MACD/RSI from completed DAILY closes. The replay is intraday — say so.
function stratMixedResolution(s: StrategyRow): boolean {
  return stratWantsDaily(s) && stratHasPriceExit(s);
}

// A strategy still HARD-LOCKED to daily bars: daily signals with nothing
// price-triggered to gain from an intraday replay.
function stratLockedDaily(s: StrategyRow): boolean {
  return stratWantsDaily(s) && !stratMixedResolution(s);
}

// The bar size is DERIVED from the strategy, never chosen: MACD/RSI → 1 Day
// (daily signals, matching live) unless a stop makes it a mixed-resolution run;
// VWAP → 15 Min (an intraday measure); a plain
// STOCK strategy follows its trading style (swing → daily, intraday → 15-min).
// 1-hour is intentionally gone: 15-min is a strictly more faithful intraday
// simulation and daily is right for daily signals — the live engine ticks every
// ~60s, so a coarse hourly bar would miss intraday stops and VWAP crosses.
//
// CRYPTO always replays on 15-min bars (unless MACD/RSI force daily). A daily
// bar spans a full 24h of continuous trading — 3.7x more hidden movement than a
// stock's 6.5h session — so a stop-loss or trailing stop simply cannot be
// simulated on it: the exit logic would run once per day at the close, and a
// position that dipped through its stop and recovered would be scored a winner.
// Resolution follows whether intraday exits EXIST (with a mandatory stop, always),
// not how long the trade is held.
function deriveTimeframe(s: StrategyRow | undefined): "1Day" | "15Min" {
  if (!s) return "1Day";
  // Daily signals + a stop → the REPLAY is 15-min (the signals stay daily).
  if (stratWantsDaily(s)) return stratMixedResolution(s) ? "15Min" : "1Day";
  if (stratWantsIntraday(s)) return "15Min";
  if (s.asset_class === "crypto") return "15Min";
  return s.swing_mode ? "1Day" : "15Min";
}

// Compare-chart line colours — strategy A (primary) and B (compared). Shared so
// the "trades in view" log can colour each strategy's name to match its line.
const SERIES_A_COLOR = "var(--accent)";
const SERIES_B_COLOR = "#a78bfa";

// Read-only display of a strategy's resolved universe, shown under its dropdown.
function UniverseChips({ uni }: { uni: { symbols: string[]; label: string } }) {
  if (!uni.label) return null;
  return (
    <div className="universe-ro">
      <span className="field-cap">Universe — {uni.label}</span>
      {uni.symbols.length > 0 ? (
        <div className="chips-ro">
          {uni.symbols.map((s) => (
            <span key={s} className="chip-ro">
              {s}
            </span>
          ))}
        </div>
      ) : (
        <span className="hint">Resolved live from the strategy — no fixed list to show.</span>
      )}
    </div>
  );
}

export default function Backtest() {
  const [mode, setMode] = useState<"single" | "compare" | "portfolio">("single");
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [baskets, setBaskets] = useState<Basket[]>([]);
  const [days, setDays] = useState(90);
  const [spread, setSpread] = useState(0.1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [compareId, setCompareId] = useState<number | null>(null); // optional 2nd strategy
  const [compareResult, setCompareResult] = useState<BacktestResult | null>(null);
  // Visible index window [start, end] reported by the chart while zoomed (null =
  // full range) — drives the "trades in view" panel.
  const [zoomRange, setZoomRange] = useState<[number, number] | null>(null);

  // Portfolio mode: N strategies sharing ONE account + the same global rails.
  const [portfolioIds, setPortfolioIds] = useState<number[]>([]);
  const [portfolioResult, setPortfolioResult] = useState<PortfolioBacktestResult | null>(null);
  const [portfolioBusy, setPortfolioBusy] = useState(false);
  const [portfolioError, setPortfolioError] = useState<string | null>(null);

  // Place each buy/sell on the equity curve's day index. Positions still open at
  // the window end aren't in trade_list (no forced sale), but their BUYS still
  // happened — mark them too, or the last entries of a run silently vanish.
  const markers = useMemo<ChartMarker[]>(() => {
    if (!result) return [];
    const dayIndex = new Map(result.equity_days.map((d, i) => [d, i]));
    const out: ChartMarker[] = [];
    for (const t of result.trade_list) {
      const entry = dayIndex.get(t.entry_day);
      if (entry !== undefined) {
        out.push({ index: entry, kind: "buy", text: `Bought ${t.qty} ${t.symbol} @ $${t.entry_price}` });
      }
      const exit = t.exit_day ? dayIndex.get(t.exit_day) : undefined;
      if (exit !== undefined) {
        out.push({
          index: exit,
          kind: "sell",
          text: `Sold ${t.symbol} @ $${t.exit_price} → ${(t.pnl ?? 0) >= 0 ? "+" : ""}$${t.pnl?.toFixed(2)} (${t.exit_reason})`,
        });
      }
    }
    for (const p of result.open_positions) {
      const entry = dayIndex.get(p.entry_day);
      if (entry !== undefined) {
        out.push({ index: entry, kind: "buy", text: `Bought ${p.qty} ${p.symbol} @ $${p.entry_price} (still open)` });
      }
    }
    return out;
  }, [result]);

  // Comparison markers: BOTH strategies' trades, each riding its own equity line
  // (A → series 0, B → series 1) and prefixed with the strategy name so the
  // hover panel says who traded. Replaces the trade log in compare mode.
  const compareMarkers = useMemo<ChartMarker[]>(() => {
    if (!result || !compareResult) return [];
    const dayIndex = new Map(result.equity_days.map((d, i) => [d, i]));
    const aName = strategies.find((s) => s.id === strategyId)?.name ?? "A";
    const bName = strategies.find((s) => s.id === compareId)?.name ?? "B";
    const out: ChartMarker[] = [];
    const add = (res: BacktestResult, si: number, name: string) => {
      for (const t of res.trade_list) {
        const e = dayIndex.get(t.entry_day);
        if (e !== undefined)
          out.push({ index: e, seriesIndex: si, kind: "buy", text: `${name}: bought ${t.qty} ${t.symbol} @ $${t.entry_price}` });
        const x = t.exit_day ? dayIndex.get(t.exit_day) : undefined;
        if (x !== undefined)
          out.push({
            index: x,
            seriesIndex: si,
            kind: "sell",
            text: `${name}: sold ${t.symbol} @ $${t.exit_price} → ${(t.pnl ?? 0) >= 0 ? "+" : ""}$${t.pnl?.toFixed(2)} (${t.exit_reason})`,
          });
      }
      for (const p of res.open_positions) {
        const e = dayIndex.get(p.entry_day);
        if (e !== undefined)
          out.push({ index: e, seriesIndex: si, kind: "buy", text: `${name}: bought ${p.qty} ${p.symbol} @ $${p.entry_price} (still open)` });
      }
    };
    add(result, 0, aName);
    add(compareResult, 1, bName);
    return out;
  }, [result, compareResult, strategies, strategyId, compareId]);

  // Trades whose entry/exit falls inside the zoomed day-window, tagged by
  // strategy (compare mode) and time-ordered — the "what happened in this span"
  // panel that appears under the chart while zoomed.
  const zoomEvents = useMemo<(TradeEvent & { strategy?: string; strategyColor?: string })[]>(() => {
    if (!result || !zoomRange) return [];
    const days = result.equity_days;
    const lo = days[Math.max(0, zoomRange[0])];
    const hi = days[Math.min(days.length - 1, zoomRange[1])];
    if (!lo || !hi) return [];
    const inWin = (d?: string | null) => !!d && d >= lo && d <= hi;
    const aName = strategies.find((s) => s.id === strategyId)?.name ?? "A";
    const bName = strategies.find((s) => s.id === compareId)?.name ?? "B";
    const rows: (TradeEvent & { strategy?: string; strategyColor?: string })[] = [];
    const push = (res: BacktestResult, name?: string, color?: string) => {
      for (const t of res.trade_list) {
        if (inWin(t.entry_day))
          rows.push({ at: t.entry_at, action: "Bought", symbol: t.symbol, price: t.entry_price, qty: t.qty, reason: t.entry_reason, strategy: name, strategyColor: color });
        if (t.exit_at && inWin(t.exit_day))
          rows.push({ at: t.exit_at, action: "Sold", symbol: t.symbol, price: t.exit_price, pnl: t.pnl, reason: t.exit_reason, strategy: name, strategyColor: color });
      }
      for (const p of res.open_positions) {
        if (inWin(p.entry_day))
          rows.push({ at: p.entry_at, action: "Bought", symbol: p.symbol, price: p.entry_price, qty: p.qty, reason: `${p.entry_reason} — still open at test end`, strategy: name, strategyColor: color });
      }
    };
    push(result, compareResult ? aName : undefined, SERIES_A_COLOR);
    if (compareResult) push(compareResult, bName, SERIES_B_COLOR);
    rows.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
    return rows;
  }, [result, compareResult, zoomRange, strategies, strategyId, compareId]);

  // Holdings attribution for the chart hover, converted from dollars into
  // PERCENTAGE POINTS of starting cash — the unit the equity line is plotted
  // in, so a day's holdings visibly sum to the line's day-over-day move.
  const holdingsPct = useMemo<Record<string, DayHolding[]> | undefined>(() => {
    if (!result?.daily_positions || !result.starting_cash) return undefined;
    const base = result.starting_cash;
    return Object.fromEntries(
      Object.entries(result.daily_positions).map(([day, rows]) => [
        day,
        rows.map((r) => ({ symbol: r.symbol, qty: r.qty, day_pnl_pct: (r.day_pnl / base) * 100 })),
      ]),
    );
  }, [result]);

  // Flatten round-trip trades into individual buy/sell actions in time order,
  // so the log reads like the chart markers: why it bought, then why it sold.
  // Still-open positions contribute their BUY (with no matching sell).
  const events = useMemo<TradeEvent[]>(() => {
    if (!result) return [];
    const out: TradeEvent[] = [];
    for (const t of result.trade_list) {
      out.push({
        at: t.entry_at,
        action: "Bought",
        symbol: t.symbol,
        price: t.entry_price,
        qty: t.qty,
        reason: t.entry_reason,
      });
      if (t.exit_at) {
        out.push({
          at: t.exit_at,
          action: "Sold",
          symbol: t.symbol,
          price: t.exit_price,
          pnl: t.pnl,
          reason: t.exit_reason,
        });
      }
    }
    for (const p of result.open_positions) {
      out.push({
        at: p.entry_at,
        action: "Bought",
        symbol: p.symbol,
        price: p.entry_price,
        qty: p.qty,
        reason: `${p.entry_reason} — still open at test end`,
      });
    }
    out.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
    return out;
  }, [result]);

  // Trade log rows: buy/sell actions PLUS "no entry" spans (collapsed runs of
  // days that traded nothing), interleaved chronologically — so a flat stretch on
  // the curve explains itself right in the log.
  const logRows = useMemo<
    (
      | { kind: "trade"; sortAt: number; ev: TradeEvent }
      | { kind: "gap"; sortAt: number; span: NonNullable<BacktestResult["no_trade_spans"]>[number] }
    )[]
  >(() => {
    if (!result) return [];
    const rows: (
      | { kind: "trade"; sortAt: number; ev: TradeEvent }
      | { kind: "gap"; sortAt: number; span: NonNullable<BacktestResult["no_trade_spans"]>[number] }
    )[] = events.map((ev) => ({ kind: "trade", sortAt: new Date(ev.at).getTime(), ev }));
    for (const span of result.no_trade_spans ?? []) {
      rows.push({ kind: "gap", sortAt: new Date(`${span.from_day}T00:00:00`).getTime(), span });
    }
    rows.sort((a, b) => a.sortAt - b.sortAt);
    return rows;
  }, [result, events]);

  useEffect(() => {
    // Another page (e.g. the strategy editor's "Backtest" button) may have
    // jumped here with a strategy in tow — preselect it.
    const pre = consumeNav()?.strategyId ?? null;
    getStrategies().then((rows) => {
      setStrategies(rows);
      if (rows.length && strategyId === null)
        setStrategyId(pre !== null && rows.some((r) => r.id === pre) ? pre : rows[0].id);
    });
    getBaskets().then(setBaskets).catch(() => setBaskets([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const strategy = strategies.find((s) => s.id === strategyId);

  // Which bar sizes are valid for this strategy. MACD/RSI are DAILY signals (the
  // live engine computes them from completed daily bars), so computing them on
  // intraday closes would be twitchy and nothing like live. VWAP is the opposite
  // — an intraday-only measure — so a VWAP strategy can't use 1 Day. (VWAP wins
  // if somehow both are set; that strategy is misconfigured and the builder
  // already warns about it.)
  const usesVwap = !!strategy && stratWantsIntraday(strategy);
  const usesDailySignals = !!strategy && stratWantsDaily(strategy);
  // Daily signals no longer mean a daily REPLAY: a strategy that also carries a
  // stop runs mixed-resolution — 15-minute bars for entries/exits, MACD/RSI still
  // read off completed daily closes. Only a daily-signal strategy with nothing
  // price-triggered stays locked to 1 Day (an intraday replay would buy it
  // nothing).
  const mixedResolution = !!strategy && stratMixedResolution(strategy);

  // Each strategy's universe is READ-ONLY here — resolved from its own config and
  // shown under its dropdown, never edited. The backtest tests the strategy's own
  // universe: strategy A on A's, the compare strategy on its own.
  const uniA = resolveUniverse(strategy, baskets);
  const compareStrat = strategies.find((s) => s.id === compareId);
  const uniB = resolveUniverse(compareStrat, baskets);

  // The universe is EXACTLY the strategy's — nothing here can deviate from it.
  // A scanner strategy always replays the historical daily risers with ITS OWN
  // top-N; there is deliberately no toggle or knob to test anything else.
  const scannerReplay = uniA.scannerReplay;
  const replayTopN = strategy?.top_n || 10;

  // Bar size + account are DERIVED, not chosen. Single mode: from the one
  // strategy. Compare mode: both share ONE timeline (the chart needs it), so the
  // bar size is the finer of the two; each strategy still runs on its own sleeve.
  const singleTimeframe = deriveTimeframe(strategy);
  const singleCash = Math.max(strategy?.sleeve_usd || 100, 100);
  const compareTf = compareStrat ? deriveTimeframe(compareStrat) : singleTimeframe;
  const compareTimeframe: "1Day" | "15Min" =
    singleTimeframe === "15Min" || compareTf === "15Min" ? "15Min" : "1Day";
  // A genuine conflict: the shared bar size violates a strategy's HARD lock
  // (MACD/RSI must be daily; VWAP must be intraday) — can't test them together.
  // A mixed-resolution strategy is NOT locked to daily: 15-min is exactly what it
  // wants, so pairing it with a VWAP strategy is fine.
  const barConflict = (s: StrategyRow | undefined) =>
    !!s &&
    ((compareTimeframe === "15Min" && stratLockedDaily(s)) ||
      (compareTimeframe === "1Day" && stratWantsIntraday(s)));
  const compareMixedBars = barConflict(strategy) || barConflict(compareStrat);
  const effectiveTimeframe = mode === "compare" ? compareTimeframe : singleTimeframe;

  async function run(e: FormEvent) {
    e.preventDefault();
    if (strategyId === null) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setCompareResult(null);
    setZoomRange(null);
    // Shared settings (period, derived bar size, spread); each strategy runs on
    // its OWN universe and its own sleeve — a head-to-head of complete configs.
    const shared = { days, timeframe: effectiveTimeframe, spread_pct: spread };
    // Only compare mode runs a second strategy.
    const doCompare = mode === "compare" && compareStrat && compareId !== strategyId;
    try {
      const [r, cr] = await Promise.all([
        runBacktest({
          strategy_id: strategyId,
          symbols: uniA.symbols,
          scanner_replay: scannerReplay,
          replay_top_n: replayTopN,
          starting_cash: singleCash,
          ...shared,
        }),
        doCompare
          ? runBacktest({
              strategy_id: compareStrat.id,
              symbols: uniB.symbols,
              scanner_replay: uniB.scannerReplay,
              replay_top_n: compareStrat.top_n || 10,
              starting_cash: Math.max(compareStrat.sleeve_usd || 5000, 100),
              ...shared,
            })
          : Promise.resolve(null),
      ]);
      setResult(r);
      setCompareResult(cr);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Portfolio trade log: every buy/sell in time order, tagged with its strategy.
  // Still-open positions contribute their BUY (with no matching sell).
  const portfolioEvents = useMemo<(TradeEvent & { strategy: string })[]>(() => {
    if (!portfolioResult) return [];
    const out: (TradeEvent & { strategy: string })[] = [];
    for (const t of portfolioResult.trade_list) {
      out.push({ at: t.entry_at, action: "Bought", symbol: t.symbol, price: t.entry_price, qty: t.qty, reason: t.entry_reason, strategy: t.strategy_name });
      if (t.exit_at) {
        out.push({ at: t.exit_at, action: "Sold", symbol: t.symbol, price: t.exit_price, pnl: t.pnl, reason: t.exit_reason, strategy: t.strategy_name });
      }
    }
    for (const p of portfolioResult.open_positions) {
      out.push({
        at: p.entry_at, action: "Bought", symbol: p.symbol, price: p.entry_price, qty: p.qty,
        reason: `${p.entry_reason} — still open at test end`, strategy: p.strategy_name ?? "",
      });
    }
    out.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
    return out;
  }, [portfolioResult]);

  function togglePortfolioId(id: number) {
    setPortfolioIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  }

  // Portfolio: the account and the bar size are DERIVED, not chosen. The account
  // is the sum of the picked strategies' sleeves (each keeps its own). The bar
  // size is the finest any strategy needs — intraday if one uses VWAP, else daily
  // (correct for MACD/RSI/rotation). A mix of intraday-only and daily-only
  // strategies can't both be faithful on one timeline, so we flag it.
  const portfolioStrats = portfolioIds
    .map((id) => strategies.find((s) => s.id === id))
    .filter((s): s is StrategyRow => !!s);
  const portfolioCash = Math.max(portfolioStrats.reduce((sum, s) => sum + (s.sleeve_usd || 0), 0), 100);
  const portfolioAnyIntraday = portfolioStrats.some(stratWantsIntraday);
  const portfolioAnyDaily = portfolioStrats.some(stratWantsDaily);
  const portfolioTimeframe = portfolioAnyIntraday ? "15Min" : "1Day";
  const portfolioMixedBars = portfolioAnyIntraday && portfolioAnyDaily;

  async function runPortfolio(e: FormEvent) {
    e.preventDefault();
    if (portfolioIds.length === 0) return;
    setPortfolioBusy(true);
    setPortfolioError(null);
    setPortfolioResult(null);
    try {
      const r = await runPortfolioBacktest({
        strategy_ids: portfolioIds,
        days,
        timeframe: portfolioTimeframe,
        starting_cash: portfolioCash,
        spread_pct: spread,
      });
      setPortfolioResult(r);
    } catch (err) {
      setPortfolioError((err as Error).message);
    } finally {
      setPortfolioBusy(false);
    }
  }

  return (
    <>
      <div className="toolbar">
        <h2>Backtest</h2>
        <div className="seg">
          <button
            type="button"
            className={mode === "single" ? "active" : ""}
            onClick={() => {
              setMode("single");
              setCompareId(null);
              setCompareResult(null);
            }}
          >
            Single strategy
          </button>
          <button
            type="button"
            className={mode === "compare" ? "active" : ""}
            onClick={() => setMode("compare")}
          >
            Compare
          </button>
          <button type="button" className={mode === "portfolio" ? "active" : ""} onClick={() => setMode("portfolio")}>
            Portfolio
          </button>
        </div>
      </div>

      {mode === "portfolio" && (
        <>
          <div className="card">
            <p className="hint">
              Runs <strong>several strategies at once over the same period, sharing ONE account</strong> and the same
              global risk rails the live engine enforces — max total positions, exposure capped at your equity (no
              leverage), the cross-strategy trade-rate limit, and the daily-loss kill switch. Each strategy still keeps
              its own sleeve, sizing and universe. Below the portfolio result you'll see a{" "}
              <strong>per-strategy contribution breakdown</strong>. Same honest limits as the single backtest — fills are
              modeled as price ± spread, the free IEX feed sees a slice of the market, and{" "}
              <strong>past results predict nothing</strong>. A scanner strategy falls back to its asset-class watchlist
              (a merged timeline can't reconstruct the historical daily risers).
            </p>
            <form className="backtest-form" onSubmit={runPortfolio}>
              <div className="field">
                <span className="field-cap">Strategies in the portfolio (pick two or more)</span>
                <div className="portfolio-picker">
                  {strategies.length === 0 && <span className="hint">Create a strategy first.</span>}
                  {strategies.map((s) => (
                    <label key={s.id} className="check">
                      <input type="checkbox" checked={portfolioIds.includes(s.id)} onChange={() => togglePortfolioId(s.id)} />
                      {s.name} <span className="hint">({s.asset_class})</span>
                    </label>
                  ))}
                </div>
              </div>
              <div className="filter-grid">
                <label>
                  <span className="field-cap">
                    History <InfoTip k="history_days" />
                  </span>
                  <div className="affix">
                    <NumberField min={7} max={730} step={1} value={days} onChange={setDays} />
                    <span className="affix-unit">days</span>
                  </div>
                </label>
                <label>
                  <span className="field-cap">
                    Spread cost per side <InfoTip k="spread_cost" />
                  </span>
                  <div className="affix">
                    <NumberField min={0} max={2} step={0.05} value={spread} onChange={setSpread} />
                    <span className="affix-unit">%</span>
                  </div>
                </label>
              </div>
              {/* Account and bar size are DERIVED, not chosen — the account is the
                  sum of the picked strategies' sleeves; the bar size is whatever
                  their signals need. */}
              {portfolioStrats.length > 0 && (
                <p className="hint">
                  <strong>Account ${portfolioCash.toLocaleString()}</strong> — the sum of the {portfolioStrats.length}{" "}
                  selected {portfolioStrats.length === 1 ? "sleeve" : "sleeves"} (each strategy keeps its own).{" "}
                  <strong>Bar size {portfolioTimeframe === "1Day" ? "1 Day" : "15 Min"}</strong>, chosen automatically
                  ({portfolioAnyIntraday
                    ? "a strategy uses VWAP, which needs intraday bars"
                    : "daily signals / rotation run on daily bars"}
                  ).
                </p>
              )}
              {portfolioMixedBars && (
                <p className="field-help warn">
                  <IconWarn className="icon-inline" /> These strategies need <strong>different</strong> bar sizes — one
                  uses VWAP (intraday), another uses MACD/RSI (daily). They can't be backtested together faithfully on a
                  single timeline; run them separately.
                </p>
              )}
              {portfolioError && <div className="error">{portfolioError}</div>}
              <button disabled={portfolioBusy || portfolioIds.length === 0 || portfolioMixedBars}>
                {portfolioBusy ? "Replaying history…" : "Run portfolio"}
              </button>
            </form>
          </div>

          {portfolioResult && (
            <>
              <div className="card">
                <h3>
                  Portfolio · {portfolioResult.strategy_count} strategies ({portfolioResult.strategy_names.join(", ")}) ·
                  last {portfolioResult.days} days ({portfolioResult.timeframe})
                </h3>
                <p className="hint">
                  <strong>Simulation, not real trading.</strong> All strategies competed for one cash balance under the
                  global rails, exactly as they would live. Fills are assumed at price ± the spread cost; a real order
                  can miss on a fast or thin move, gap overnight, or halt. Treat a good result as <em>not yet
                  disproven</em>, never proven.
                </p>
                {/* Metric | Value table (was stat tiles) — consistent with the
                    single + compare backtest views. */}
                {(() => {
                  const rows: { label: string; value: string; tone?: "up" | "down" }[] = [
                    {
                      label: "Net P&L",
                      value: `$${portfolioResult.net_pnl.toLocaleString()} (${portfolioResult.net_pnl_pct >= 0 ? "+" : ""}${portfolioResult.net_pnl_pct}%)`,
                      tone: portfolioResult.net_pnl >= 0 ? "up" : "down",
                    },
                    { label: "Trades", value: String(portfolioResult.trades) },
                    { label: "Win rate", value: portfolioResult.win_rate != null ? `${portfolioResult.win_rate}%` : "—" },
                    { label: "Avg win / loss", value: `${portfolioResult.avg_win ?? "—"} / ${portfolioResult.avg_loss ?? "—"}` },
                    { label: "Profit factor", value: portfolioResult.profit_factor != null ? String(portfolioResult.profit_factor) : "—" },
                    { label: "Max drawdown", value: `${portfolioResult.max_drawdown_pct}%`, tone: portfolioResult.max_drawdown_pct > 10 ? "down" : undefined },
                  ];
                  return (
                    <div className="compare-table">
                      <h4>Results</h4>
                      <div className="table-scroll">
                        <table>
                          <thead>
                            <tr>
                              <th>Metric</th>
                              <th>Value</th>
                            </tr>
                          </thead>
                          <tbody>
                            {rows.map((r) => (
                              <tr key={r.label}>
                                <td>{r.label}</td>
                                <td className={r.tone ?? ""}>{r.value}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  );
                })()}

                {portfolioResult.trades > 0 && (
                  <div className="deployment">
                    <h4>
                      How much of your money actually worked? <InfoTip k="capital_deployed" />
                    </h4>
                    <div className="stats">
                      <Stat
                        label="Most ever invested"
                        value={`$${portfolioResult.max_deployed_usd.toLocaleString()} (${portfolioResult.pct_capital_deployed}%)`}
                        tone={portfolioResult.pct_capital_deployed < 20 ? "down" : undefined}
                      />
                      <Stat label="Time holding anything" value={`${portfolioResult.time_in_market_pct}%`} />
                      <Stat
                        label="Return on money used"
                        value={portfolioResult.return_on_deployed_pct != null ? `${portfolioResult.return_on_deployed_pct}%` : "—"}
                        tone={(portfolioResult.return_on_deployed_pct ?? 0) >= 0 ? "up" : "down"}
                      />
                    </div>
                  </div>
                )}

                <LineChart
                  labels={portfolioResult.equity_days}
                  series={[
                    { label: "Portfolio", color: "var(--accent)", values: portfolioResult.equity },
                    ...(portfolioResult.hold_benchmark
                      ? [
                          {
                            label: `Buy & hold ${portfolioResult.hold_benchmark_label}`,
                            color: "var(--warn)",
                            values: portfolioResult.hold_benchmark,
                          },
                        ]
                      : []),
                  ]}
                />
              </div>

              <div className="card">
                <h3>
                  Per-strategy contribution{" "}
                  <span className="hint">(realized + unrealized per sleeve sums to the portfolio net P&amp;L)</span>
                </h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Strategy</th>
                        <th>Realized P&L</th>
                        <th>Unrealized</th>
                        <th>Share</th>
                        <th>Trades</th>
                        <th>Win rate</th>
                      </tr>
                    </thead>
                    <tbody>
                      {portfolioResult.contributions.map((c) => (
                        <tr key={c.strategy_id}>
                          <td className="sym">{c.strategy_name}</td>
                          <td className={c.realized_pnl >= 0 ? "up" : "down"}>
                            {c.realized_pnl >= 0 ? "+" : ""}${c.realized_pnl.toLocaleString()}
                          </td>
                          <td className={c.unrealized_pnl > 0 ? "up" : c.unrealized_pnl < 0 ? "down" : ""}>
                            {c.unrealized_pnl === 0 ? "—" : `${c.unrealized_pnl >= 0 ? "+" : ""}$${c.unrealized_pnl.toLocaleString()}`}
                          </td>
                          <td>{c.share_pct != null ? `${c.share_pct}%` : "—"}</td>
                          <td>{c.trades}</td>
                          <td>{c.win_rate != null ? `${c.win_rate}%` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {portfolioResult.open_positions.length > 0 && (
                <div className="card">
                  <h3>
                    Still open at test end{" "}
                    <span className="hint">(held, not sold — unrealized P&amp;L is already in the result)</span>
                  </h3>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Strategy</th>
                          <th>Symbol</th>
                          <th>Entry</th>
                          <th>Now</th>
                          <th>Unrealized P&L</th>
                          <th>Held since</th>
                        </tr>
                      </thead>
                      <tbody>
                        {portfolioResult.open_positions.map((p, i) => (
                          <tr key={i}>
                            <td className="sym">{p.strategy_name}</td>
                            <td className="sym">{p.symbol}</td>
                            <td>
                              ${p.entry_price.toFixed(4)}
                              <span className="hint"> ×{p.qty}</span>
                            </td>
                            <td>${p.mark_price.toFixed(4)}</td>
                            <td className={p.unrealized_pnl >= 0 ? "up" : "down"}>
                              {p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(2)}
                            </td>
                            <td>{new Date(`${p.entry_day}T00:00:00`).toLocaleDateString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              <div className="card">
                <h3>
                  Trade log — every buy and sell in order{" "}
                  <span className="hint">
                    ({portfolioEvents.length} actions across {portfolioResult.trade_list.length} trades)
                  </span>
                </h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Strategy</th>
                        <th>Action</th>
                        <th>Symbol</th>
                        <th>Price</th>
                        <th>P&L</th>
                        <th>Why</th>
                      </tr>
                    </thead>
                    <tbody>
                      {portfolioEvents.map((ev, i) => (
                        <tr key={i}>
                          <td>{new Date(ev.at).toLocaleDateString()}</td>
                          <td className="hint">{ev.strategy}</td>
                          <td className={ev.action === "Bought" ? "up" : "down"}>
                            {ev.action === "Bought" ? "▲ Bought" : "▼ Sold"}
                          </td>
                          <td className="sym">{ev.symbol}</td>
                          <td>
                            ${ev.price.toFixed(4)}
                            {ev.qty != null && <span className="hint"> ×{ev.qty}</span>}
                          </td>
                          <td className={ev.pnl == null ? "" : ev.pnl >= 0 ? "up" : "down"}>
                            {ev.pnl == null ? <span className="hint">—</span> : `$${ev.pnl.toFixed(2)}`}
                          </td>
                          <td className="hint">{ev.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </>
      )}

      {mode !== "portfolio" && (
      <>
      <div className="card">
        <p className="hint">
          Replays a strategy's exact rules over past prices — the same code the live engine runs. Each strategy is
          tested on its <strong>own universe</strong> (a "today's risers" strategy replays the historical risers; a
          basket tests its symbols). Honest limits: fills are modeled as price ± the spread cost, and the free IEX feed
          sees a slice of the market. <strong>Past results predict nothing</strong> — a backtest can only kill bad ideas
          cheaply, not promise good ones.
        </p>
        <form className="backtest-form" onSubmit={run}>
          {/* WHAT to test: each strategy, with its OWN universe shown read-only
              below it. The universe is defined by the strategy — not chosen here. */}
          <div className="filter-grid backtest-strats">
            <div className="bt-strat-col">
              {/* Selector sized to its content, with Edit right beside it — a
                  strategy name doesn't need a full-column-wide control. */}
              <span className="field-cap">Strategy</span>
              <div className="picker-row">
                <select value={strategyId ?? ""} onChange={(e) => setStrategyId(Number(e.target.value))} required>
                  {strategies.length === 0 && <option value="">— create a strategy first —</option>}
                  {strategies.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} ({s.asset_class})
                    </option>
                  ))}
                </select>
                {strategy && (
                  <button
                    type="button"
                    className="small btn-icon btn-ghost"
                    onClick={() => requestNav({ tab: "strategies", strategyId: strategy.id })}
                    title="Open this strategy in the editor"
                  >
                    <IconEdit />
                    Edit
                  </button>
                )}
              </div>
              <UniverseChips uni={uniA} />
            </div>
            {mode === "compare" && (
              <div className="bt-strat-col">
                <span className="field-cap">Compare against</span>
                <div className="picker-row">
                  <select value={compareId ?? ""} onChange={(e) => setCompareId(e.target.value ? Number(e.target.value) : null)}>
                    <option value="">— pick a strategy —</option>
                    {strategies
                      .filter((s) => s.id !== strategyId)
                      .map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name} ({s.asset_class})
                        </option>
                      ))}
                  </select>
                  {compareStrat && (
                    <button
                      type="button"
                      className="small btn-icon btn-ghost"
                      onClick={() => requestNav({ tab: "strategies", strategyId: compareStrat.id })}
                      title="Open this strategy in the editor"
                    >
                      <IconEdit />
                      Edit
                    </button>
                  )}
                </div>
                {compareStrat && <UniverseChips uni={uniB} />}
              </div>
            )}
          </div>
          <p className="hint">
            {mode === "compare" ? (
              <>
                Both strategies run on the <strong>same period and bar size</strong>, each on its <strong>own universe
                and sleeve</strong> — a head-to-head where only the strategy rules differ. Symbols aren't editable here.
              </>
            ) : (
              <>
                Tested on <strong>its own universe</strong> (shown above) and its own sleeve — the backtest honours
                whatever the strategy is set to; the symbols aren't editable here.
              </>
            )}
          </p>
          {/* A scanner strategy ALWAYS replays the historical daily risers with
              its own top-N — no toggle, no knob: the backtest offers no way to
              test a universe the strategy doesn't actually trade. */}
          {scannerReplay && (
            <p className="hint">
              <strong>Scanner replay</strong> <InfoTip k="scanner_replay" /> — tests the historical{" "}
              <strong>top {replayTopN} risers each day</strong> (this strategy's own universe and top-N; change it on
              the strategy, not here). Needs a completed sweep first (Settings → Historical bar cache); with an{" "}
              <strong>intraday sweep</strong> it replays 15-minute bars so intraday exits behave for real, otherwise
              daily bars.
            </p>
          )}
          {/* HOW to test: period + spread only. Bar size and starting cash are
              DERIVED (shown below), never chosen. */}
          <div className="filter-grid">
            <label>
              <span className="field-cap">
                History <InfoTip k="history_days" />
              </span>
              <div className="affix">
                <NumberField min={7} max={730} step={1} value={days} onChange={setDays} />
                <span className="affix-unit">days</span>
              </div>
            </label>
            <label>
              <span className="field-cap">
                Spread cost per side <InfoTip k="spread_cost" />
              </span>
              <div className="affix">
                <NumberField min={0} max={2} step={0.05} value={spread} onChange={setSpread} />
                <span className="affix-unit">%</span>
              </div>
            </label>
          </div>
          {/* Bar size + account are derived, not chosen: an arbitrary starting
              cash makes idle money look like a strategy flaw, and a coarse bar
              hides intraday stops. Both come straight from the strategy. */}
          <p className="hint">
            <strong>Bar size {effectiveTimeframe === "1Day" ? "1 Day" : "15 Min"}</strong>
            {mixedResolution && effectiveTimeframe === "15Min"
              ? " · signals from daily closes (MACD/RSI), stops checked every 15 minutes"
              : usesDailySignals
                ? " (MACD/RSI are daily signals)"
                : usesVwap
                  ? " (VWAP is an intraday measure)"
                  : mode === "compare"
                    ? " (the finer of the two strategies)"
                    : strategy?.asset_class === "crypto"
                      ? " (crypto trades 24/7 — stops need intraday bars)"
                      : strategy?.swing_mode
                        ? " (swing strategy)"
                        : " (intraday strategy)"}{" "}
            ·{" "}
            {mode === "compare" ? (
              <>
                each strategy's account is <strong>its own sleeve</strong>
              </>
            ) : (
              <>
                account <strong>${singleCash.toLocaleString()}</strong> (this strategy's sleeve)
              </>
            )}{" "}
            — both set automatically from the strategy.
          </p>
          {mode === "compare" && compareMixedBars && (
            <p className="hint warn">
              <IconWarn className="icon-inline" /> These two strategies need <strong>different bar sizes</strong> (one
              is daily-only, the other intraday-only), so they can't share one timeline. Compare against a
              same-granularity strategy, or test each on its own.
            </p>
          )}
          <p className="hint">
            A backtest tests the strategy's <strong>whole universe symbol set</strong> over history — it can't
            reconstruct the historical daily top-N ranking that the live engine does, so top-N is a live entry-selection
            feature only.
          </p>
          {error && <div className="error">{error}</div>}
          <button
            disabled={busy || strategyId === null || (mode === "compare" && (!compareId || compareMixedBars))}
          >
            {busy ? "Replaying history…" : mode === "compare" ? "Run comparison" : "Run backtest"}
          </button>
        </form>
      </div>

      {result && (
        <>
          <div className="card">
            <h3>
              {result.strategy_name} ·{" "}
              {result.scanner_replay
                ? `scanner replay (top ${result.replay_top_n ?? replayTopN}, ${result.replay_intraday ? "intraday 15-min" : "daily bars"}) — ${result.days_replayed ?? 0} days, ${result.universe_size ?? 0} unique movers`
                : result.symbols.join(", ")}{" "}
              · last {result.days} days ({result.timeframe})
            </h3>
            <p className="hint">
              <strong>Simulation, not real trading.</strong> QT trades paper-first (fake money, real prices), and even
              paper — let alone live — will differ from this. Here fills are assumed at price ± the spread cost, but a
              real marketable-limit order can miss on a fast or thin move, gap overnight, or halt; the free IEX feed
              sees only a slice of volume; scanner replay uses today's tradable list (survivorship bias); and your own
              orders, market impact, and slippage beyond the spread aren't modeled. Treat a good result as{" "}
              <em>not yet disproven</em>, never proven.
            </p>
            {result.scanner_replay &&
              result.replay_intraday === false &&
              strategy &&
              (!strategy.swing_mode || strategy.params.exit.flatten_before_close) && (
                <p className="hint warn">
                  <IconWarn className="icon-inline" /> <strong>This ran on daily bars, not intraday.</strong> Your strategy trades intraday
                  (flatten-before-close / no overnight hold), which a daily-bar replay can't simulate — positions look
                  like they exit the next day and stops can gap overnight instead of firing intraday. Run a{" "}
                  <strong>{strategy.asset_class === "crypto" ? "crypto intraday sweep" : "intraday sweep"}</strong>{" "}
                  (Settings → Historical bar cache) so replay uses 15-minute bars, then re-run for a true test.
                </p>
              )}
            {/* The honest-run note: signals were still daily (as live reads
                them) but the stops were checked bar by intraday bar, so the
                warning below deliberately does NOT apply here. */}
            {result.mixed_resolution && (
              <p className="hint">
                <strong>Signals from daily closes, exits on 15-minute bars.</strong> MACD/RSI were computed
                from <em>completed</em> daily closes — exactly how the live engine reads them, with no peek at
                the day still in progress — while your stop-loss, trailing stop and take-profit were checked
                every 15 minutes. So a dip through your stop that recovered by the close is scored as the loss
                it really was, not as a winner.
              </p>
            )}
            {/* Daily bars call the exit logic ONCE per day, at the close — so a
                price-triggered exit (stop / trailing / take-profit) can't be
                simulated: a position that dipped through its stop and recovered
                scores as a winner. Say so on every daily run that has one, not
                just scanner replays. */}
            {result.timeframe === "1Day" &&
              !result.mixed_resolution &&
              !result.scanner_replay &&
              strategy &&
              ((strategy.params.exit.stop_loss_pct ?? 0) > 0 ||
                (strategy.params.exit.trailing_stop_pct ?? 0) > 0 ||
                (strategy.params.exit.take_profit_pct ?? 0) > 0) && (
                <p className="hint warn">
                  <IconWarn className="icon-inline" /> <strong>Your stops weren't simulated at intraday resolution.</strong>{" "}
                  On daily bars the exit rules are checked once per day, at the close, so a position that dipped through
                  your stop-loss and recovered by the close is scored as a <em>winner</em> here — live, it would have been
                  sold.
                  {strategy.asset_class === "crypto" && (
                    <>
                      {" "}
                      A crypto daily bar covers a full 24 hours of trading, so it hides even more than a stock's 6.5-hour
                      session.
                    </>
                  )}{" "}
                  {usesDailySignals ? (
                    <>
                      MACD/RSI have to come from daily closes (that's how the live engine reads them), so this run is an
                      honest test of the <strong>entry signal</strong> — treat the P&amp;L, win rate and drawdown as
                      indicative only.
                    </>
                  ) : (
                    <>Treat the P&amp;L, win rate and drawdown as indicative only.</>
                  )}
                </p>
              )}
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
            {/* Single mode: a Metric | Value table (was stat tiles). Compare mode
                uses the head-to-head table below — same look, so the two modes
                read consistently and nothing is duplicated. */}
            {!compareResult &&
              (() => {
                const rows: { label: string; value: string; tone?: "up" | "down" }[] = [
                  {
                    label: "Net P&L",
                    value: `$${result.net_pnl.toLocaleString()} (${result.net_pnl_pct >= 0 ? "+" : ""}${result.net_pnl_pct}%)`,
                    tone: result.net_pnl >= 0 ? "up" : "down",
                  },
                  { label: "Trades", value: String(result.trades) },
                  { label: "Win rate", value: result.win_rate != null ? `${result.win_rate}%` : "—" },
                  { label: "Avg win / loss", value: `${result.avg_win ?? "—"} / ${result.avg_loss ?? "—"}` },
                  { label: "Profit factor", value: result.profit_factor != null ? String(result.profit_factor) : "—" },
                  { label: "Max drawdown", value: `${result.max_drawdown_pct}%`, tone: result.max_drawdown_pct > 10 ? "down" : undefined },
                ];
                return (
                  <div className="compare-table">
                    <h4>Results</h4>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>Metric</th>
                            <th>Value</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rows.map((r) => (
                            <tr key={r.label}>
                              <td>{r.label}</td>
                              <td className={r.tone ?? ""}>{r.value}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                );
              })()}

            {compareResult &&
              (() => {
                const A = result;
                const B = compareResult;
                const aName = strategies.find((s) => s.id === strategyId)?.name ?? "Strategy A";
                const bName = strategies.find((s) => s.id === compareId)?.name ?? "Strategy B";
                // aWins: true → A is better, false → B is better, null → tie/n-a.
                // Higher is better except drawdown (lower is better).
                const rows = [
                  { label: "Net P&L", a: `${A.net_pnl_pct}%`, b: `${B.net_pnl_pct}%`, aWins: cmp(A.net_pnl_pct, B.net_pnl_pct, true) },
                  { label: "Win rate", a: pct(A.win_rate), b: pct(B.win_rate), aWins: cmp(A.win_rate, B.win_rate, true) },
                  { label: "Max drawdown", a: `${A.max_drawdown_pct}%`, b: `${B.max_drawdown_pct}%`, aWins: cmp(A.max_drawdown_pct, B.max_drawdown_pct, false) },
                  { label: "Trades", a: String(A.trades), b: String(B.trades), aWins: null },
                  { label: "Profit factor", a: A.profit_factor != null ? String(A.profit_factor) : "—", b: B.profit_factor != null ? String(B.profit_factor) : "—", aWins: cmp(A.profit_factor, B.profit_factor, true) },
                  { label: "Return on money used", a: pct(A.return_on_deployed_pct), b: pct(B.return_on_deployed_pct), aWins: cmp(A.return_on_deployed_pct, B.return_on_deployed_pct, true) },
                  // Capital-deployment context per strategy (neither "wins" — more
                  // deployed isn't inherently better, it's just how each behaved).
                  { label: "Most ever invested", a: `$${A.max_deployed_usd.toLocaleString()} (${A.pct_capital_deployed}%)`, b: `$${B.max_deployed_usd.toLocaleString()} (${B.pct_capital_deployed}%)`, aWins: null },
                  { label: "Time in market", a: `${A.time_in_market_pct}%`, b: `${B.time_in_market_pct}%`, aWins: null },
                ];
                return (
                  <div className="compare-table">
                    <h4>
                      Head-to-head{" "}
                      <span className="hint">(same period &amp; bar size — each strategy on its own universe and sleeve)</span>
                    </h4>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>Metric</th>
                            <th>{aName}</th>
                            <th>{bName}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rows.map((r) => (
                            <tr key={r.label}>
                              <td>{r.label}</td>
                              <td className={r.aWins === true ? "cmp-win" : ""}>{r.a}</td>
                              <td className={r.aWins === false ? "cmp-win" : ""}>{r.b}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                );
              })()}

            {result.trades > 0 && !compareResult && (
              <div className="deployment">
                <h4>
                  How much of your money actually worked? <InfoTip k="capital_deployed" />
                </h4>
                <div className="stats">
                  <Stat
                    label="Most ever invested"
                    value={`$${result.max_deployed_usd.toLocaleString()} (${result.pct_capital_deployed}%)`}
                    tone={result.pct_capital_deployed < 20 ? "down" : undefined}
                  />
                  <Stat label="Time holding anything" value={`${result.time_in_market_pct}%`} />
                  <Stat
                    label="Return on money used"
                    value={result.return_on_deployed_pct != null ? `${result.return_on_deployed_pct}%` : "—"}
                    tone={(result.return_on_deployed_pct ?? 0) >= 0 ? "up" : "down"}
                  />
                </div>
                {result.pct_capital_deployed < 20 && (
                  <p className="hint">
                    Only <strong>{result.pct_capital_deployed}%</strong> of your ${result.starting_cash.toLocaleString()}{" "}
                    was ever at risk — the rest sat in cash. That's why the account return (
                    {result.net_pnl_pct}%) is so much smaller than the return on the money actually used (
                    {result.return_on_deployed_pct}%). To deploy more: raise <em>$ per trade</em>, or test more
                    symbols so the bot can hold several positions at once.
                  </p>
                )}
              </div>
            )}

            <LineChart
              labels={result.equity_days}
              onZoomChange={setZoomRange}
              markers={compareResult ? compareMarkers : markers}
              noTradeReasons={compareResult ? undefined : result.no_trade_reasons}
              holdings={compareResult ? undefined : holdingsPct}
              series={[
                {
                  label: compareResult ? strategies.find((s) => s.id === strategyId)?.name ?? "Strategy A" : "This strategy",
                  color: SERIES_A_COLOR,
                  values: result.equity,
                },
                ...(compareResult
                  ? [
                      {
                        label: strategies.find((s) => s.id === compareId)?.name ?? "Strategy B",
                        color: SERIES_B_COLOR,
                        values: compareResult.equity,
                      },
                    ]
                  : []),
                ...(result.hold_benchmark
                  ? [
                      {
                        label: `Buy & hold ${result.hold_benchmark_label}`,
                        color: "var(--warn)",
                        values: result.hold_benchmark,
                      },
                    ]
                  : []),
                ...(result.benchmark
                  ? [
                      {
                        label: `Broad market (${result.benchmark_symbol})`,
                        color: "var(--ok)",
                        values: result.benchmark,
                      },
                    ]
                  : []),
              ]}
            />
            {result.trades > 0 && (
              <div className="verdicts">
                {result.hold_benchmark && (
                  <p className="verdict">
                    {(() => {
                      const bot = result.equity[result.equity.length - 1] ?? 0;
                      const hold = result.hold_benchmark[result.hold_benchmark.length - 1];
                      if (hold == null) return null;
                      return bot > hold
                        ? `Beat simply holding ${result.hold_benchmark_label} by ${(bot - hold).toFixed(2)} points — trading the symbol was worth it.`
                        : `Simply holding ${result.hold_benchmark_label} beat the strategy by ${(hold - bot).toFixed(2)} points — trading in and out cost you.`;
                    })()}
                  </p>
                )}
                {result.benchmark && (
                  <p className="verdict muted-verdict">
                    {(() => {
                      const bot = result.equity[result.equity.length - 1] ?? 0;
                      const bench = result.benchmark[result.benchmark.length - 1];
                      if (bench == null) return null;
                      return bot > bench
                        ? `Also beat the broad market (${result.benchmark_symbol}) by ${(bot - bench).toFixed(2)} points.`
                        : `The broad market (${result.benchmark_symbol}) returned ${(bench - bot).toFixed(2)} points more.`;
                    })()}
                  </p>
                )}
              </div>
            )}
            {/* Positions still held when the window ended — not force-sold, just
                marked to market (their unrealized P&L is already in the result).
                In compare mode both strategies' holdings show, tagged by colour. */}
            {(() => {
              const aName = strategies.find((s) => s.id === strategyId)?.name ?? "A";
              const bName = strategies.find((s) => s.id === compareId)?.name ?? "B";
              const rows = compareResult
                ? [
                    ...result.open_positions.map((p) => ({ p, name: aName, color: SERIES_A_COLOR })),
                    ...compareResult.open_positions.map((p) => ({ p, name: bName, color: SERIES_B_COLOR })),
                  ]
                : result.open_positions.map((p) => ({ p, name: undefined as string | undefined, color: undefined as string | undefined }));
              if (rows.length === 0) return null;
              return (
                <div className="deployment">
                  <h4>
                    Still open at test end{" "}
                    <span className="hint">(held, not sold — unrealized P&amp;L is already in the result)</span>
                  </h4>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          {compareResult && <th>Strategy</th>}
                          <th>Symbol</th>
                          <th>Entry</th>
                          <th>Now</th>
                          <th>Unrealized P&amp;L</th>
                          <th>Held since</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map(({ p, name, color }, i) => (
                          <tr key={i}>
                            {compareResult && (
                              <td style={{ color, fontWeight: 600 }}>{name}</td>
                            )}
                            <td className="sym">{p.symbol}</td>
                            <td>
                              ${p.entry_price.toFixed(4)}
                              <span className="hint"> ×{p.qty}</span>
                            </td>
                            <td>${p.mark_price.toFixed(4)}</td>
                            <td className={p.unrealized_pnl >= 0 ? "up" : "down"}>
                              {p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(2)}
                            </td>
                            <td>{new Date(`${p.entry_day}T00:00:00`).toLocaleDateString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              );
            })()}
            {/* Zoom in on a busy stretch → the trades inside that window, tagged
                by strategy in compare mode, so a divergence between two similar
                strategies can be read trade-by-trade without the full log. */}
            {zoomRange && (
              <div className="deployment">
                <h4>
                  Trades in view{" "}
                  <span className="hint">
                    {result.equity_days[Math.max(0, zoomRange[0])]} –{" "}
                    {result.equity_days[Math.min(result.equity_days.length - 1, zoomRange[1])]}
                  </span>
                </h4>
                {zoomEvents.length === 0 ? (
                  <p className="hint">No trades in this range.</p>
                ) : (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Date</th>
                          {compareResult && <th>Strategy</th>}
                          <th>Action</th>
                          <th>Symbol</th>
                          <th>Price</th>
                          <th>P&amp;L</th>
                          <th>Why</th>
                        </tr>
                      </thead>
                      <tbody>
                        {zoomEvents.map((ev, i) => (
                          <tr key={i}>
                            <td>{new Date(ev.at).toLocaleDateString()}</td>
                            {compareResult && (
                              <td style={{ color: ev.strategyColor, fontWeight: 600 }}>{ev.strategy}</td>
                            )}
                            <td className={ev.action === "Bought" ? "up" : "down"}>
                              {ev.action === "Bought" ? "▲ Bought" : "▼ Sold"}
                            </td>
                            <td className="sym">{ev.symbol}</td>
                            <td>
                              ${ev.price.toFixed(4)}
                              {ev.qty != null && <span className="hint"> ×{ev.qty}</span>}
                            </td>
                            <td className={ev.pnl == null ? "" : ev.pnl >= 0 ? "up" : "down"}>
                              {ev.pnl == null ? <span className="hint">—</span> : `$${ev.pnl.toFixed(2)}`}
                            </td>
                            <td className="hint">{ev.reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </div>
          {/* Two full trade logs side by side is noise — in compare mode the
              chart's hover markers cover "who traded when". Single mode only. */}
          {!compareResult && (
          <div className="card">
            <h3>
              Trade log — every buy, sell, and idle stretch in order{" "}
              <span className="hint">
                ({events.length} actions across {result.trade_list.length} trades)
              </span>
            </h3>
            <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Action</th>
                  <th>Symbol</th>
                  <th>Price</th>
                  <th>P&L</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {logRows.map((r, i) =>
                  r.kind === "trade" ? (
                    <tr key={i}>
                      <td>{new Date(r.ev.at).toLocaleDateString()}</td>
                      <td className={r.ev.action === "Bought" ? "up" : "down"}>
                        {r.ev.action === "Bought" ? "▲ Bought" : "▼ Sold"}
                      </td>
                      <td className="sym">{r.ev.symbol}</td>
                      <td>
                        ${r.ev.price.toFixed(4)}
                        {r.ev.qty != null && <span className="hint"> ×{r.ev.qty}</span>}
                      </td>
                      <td className={r.ev.pnl == null ? "" : r.ev.pnl >= 0 ? "up" : "down"}>
                        {r.ev.pnl == null ? <span className="hint">—</span> : `$${r.ev.pnl.toFixed(2)}`}
                      </td>
                      <td className="hint">{r.ev.reason}</td>
                    </tr>
                  ) : (
                    <tr key={i} className="log-gap">
                      <td>
                        {r.span.from_day === r.span.to_day
                          ? new Date(`${r.span.from_day}T00:00:00`).toLocaleDateString()
                          : `${new Date(`${r.span.from_day}T00:00:00`).toLocaleDateString()} – ${new Date(
                              `${r.span.to_day}T00:00:00`,
                            ).toLocaleDateString()}`}
                      </td>
                      <td colSpan={4} className="hint">
                        no entries · {r.span.days} day{r.span.days === 1 ? "" : "s"}
                      </td>
                      <td className="hint">
                        {r.span.reason}
                        {r.span.reason_symbol_days && (
                          <>
                            <br />
                            {r.span.reason_symbol_days}
                          </>
                        )}
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
            </div>
          </div>
          )}
        </>
      )}
      </>
      )}
    </>
  );
}
