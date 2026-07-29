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
import LineChart, { ChartMarker } from "../components/LineChart";
import NumberField from "../components/NumberField";
import SymbolPicker from "../components/SymbolPicker";
import { IconWarn } from "../components/icons";

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

export default function Backtest() {
  const [mode, setMode] = useState<"single" | "portfolio">("single");
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [baskets, setBaskets] = useState<Basket[]>([]);
  const [basketNote, setBasketNote] = useState<string | null>(null);
  const [symbols, setSymbols] = useState<string[]>([]);
  const [scannerReplay, setScannerReplay] = useState(false);
  const [replayTopN, setReplayTopN] = useState(10);
  const [days, setDays] = useState(90);
  const [timeframe, setTimeframe] = useState("1Hour");
  const [cash, setCash] = useState(5000);
  const [spread, setSpread] = useState(0.1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [compareId, setCompareId] = useState<number | null>(null); // optional 2nd strategy
  const [compareResult, setCompareResult] = useState<BacktestResult | null>(null);

  // Portfolio mode: N strategies sharing ONE account + the same global rails.
  const [portfolioIds, setPortfolioIds] = useState<number[]>([]);
  const [portfolioResult, setPortfolioResult] = useState<PortfolioBacktestResult | null>(null);
  const [portfolioBusy, setPortfolioBusy] = useState(false);
  const [portfolioError, setPortfolioError] = useState<string | null>(null);

  // Place each buy/sell on the equity curve's day index.
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
    return out;
  }, [result]);

  // Flatten round-trip trades into individual buy/sell actions in time order,
  // so the log reads like the chart markers: why it bought, then why it sold.
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
    out.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
    return out;
  }, [result]);

  useEffect(() => {
    getStrategies().then((rows) => {
      setStrategies(rows);
      if (rows.length && strategyId === null) setStrategyId(rows[0].id);
    });
    getBaskets().then(setBaskets).catch(() => setBaskets([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const strategy = strategies.find((s) => s.id === strategyId);
  const assetClass = strategy?.asset_class;

  // Which bar sizes are valid for this strategy. MACD/RSI are DAILY signals (the
  // live engine computes them from completed daily bars), so an intraday backtest
  // computes them on intraday closes — twitchy and nothing like live; those
  // strategies are locked to 1 Day. VWAP is the opposite — an intraday-only
  // measure — so a VWAP strategy can't use 1 Day. (VWAP wins if somehow both are
  // set; that strategy is misconfigured and the builder already warns about it.)
  const sp = strategy?.params;
  const usesVwap = !!sp?.entry.require_above_vwap;
  const usesDailySignals =
    !usesVwap &&
    (!!sp?.entry.require_macd_bullish ||
      !!sp?.exit.exit_on_macd_bearish ||
      (sp?.entry.rsi_min ?? 0) > 0 ||
      (sp?.entry.rsi_max ?? 0) > 0 ||
      (sp?.exit.exit_rsi_above ?? 0) > 0);

  // Default the backtest to the SELECTED strategy's own universe — scanner →
  // replay, basket → its symbols, custom → its list, watchlist → the watchlist.
  // The manual controls below still override. Re-runs only when you switch
  // strategies (or once baskets finish loading, for a basket strategy).
  useEffect(() => {
    const strat = strategies.find((s) => s.id === strategyId);
    if (!strat) return;
    setBasketNote(null);
    // Default the starting cash to the strategy's own sleeve: a single-strategy
    // backtest can never deploy more than its sleeve, so a fixed $5000 against a
    // $1000 sleeve would leave most of the account idle and dilute the account-%
    // return. Clamp to the field's ≥$100 minimum. Still editable below.
    setCash(Math.max(strat.sleeve_usd || 5000, 100));
    if (strat.universe === "scanner") {
      // Scanner replay works for both stocks (ET days) and crypto (UTC days) —
      // each reads its own cache, so a "today's risers" strategy replays either.
      setScannerReplay(true);
      setSymbols([]);
      setReplayTopN(strat.top_n || 10);
    } else if (strat.universe === "basket") {
      setScannerReplay(false);
      if (strat.basket_id != null && baskets.length) loadBasket(strat.basket_id);
      else setSymbols([]);
    } else if (strat.universe === "custom") {
      setScannerReplay(false);
      setSymbols((strat.symbols ?? []).slice(0, 50));
    } else {
      setScannerReplay(false); // watchlist | both — empty picker = the watchlist
      setSymbols([]);
    }
    // Snap the bar size to one this strategy can actually be tested on, so an
    // invalid combo can't be run: MACD/RSI → 1 Day; VWAP → intraday.
    const svwap = !!strat.params?.entry.require_above_vwap;
    const sdaily =
      !svwap &&
      (!!strat.params?.entry.require_macd_bullish ||
        !!strat.params?.exit.exit_on_macd_bearish ||
        (strat.params?.entry.rsi_min ?? 0) > 0 ||
        (strat.params?.entry.rsi_max ?? 0) > 0 ||
        (strat.params?.exit.exit_rsi_above ?? 0) > 0);
    if (sdaily) setTimeframe("1Day");
    else if (svwap) setTimeframe("1Hour");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId, strategies, baskets]);

  const universeLabel = strategy
    ? {
        scanner: "today's risers (scanner replay)",
        basket: "its basket",
        custom: "its own symbol list",
        watchlist: "your watchlist",
        both: "your watchlist",
      }[strategy.universe]
    : null;

  function loadBasket(id: number) {
    setBasketNote(null);
    const basket = baskets.find((b) => b.id === id);
    if (!basket) return;
    const all = basket.symbols.filter((m) => !assetClass || m.asset_class === assetClass).map((m) => m.symbol);
    const capped = all.slice(0, 50);
    setSymbols(capped);
    if (all.length === 0) {
      setBasketNote(`"${basket.name}" has no ${assetClass ?? ""} symbols to test.`);
    } else if (all.length > 50) {
      setBasketNote(
        `Loaded the first 50 of ${all.length} symbols from "${basket.name}" — a backtest is capped at 50 symbols (rate limits).`,
      );
    } else {
      setBasketNote(`Loaded ${capped.length} symbols from "${basket.name}".`);
    }
  }

  async function run(e: FormEvent) {
    e.preventDefault();
    if (strategyId === null) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setCompareResult(null);
    // Both strategies run over the SAME settings (universe, period, cash,
    // spread) so the only difference is the strategy's own rules — an
    // apples-to-apples head-to-head.
    const base = {
      symbols,
      scanner_replay: scannerReplay,
      replay_top_n: replayTopN,
      days,
      timeframe,
      starting_cash: cash,
      spread_pct: spread,
    };
    try {
      const [r, cr] = await Promise.all([
        runBacktest({ strategy_id: strategyId, ...base }),
        compareId !== null && compareId !== strategyId
          ? runBacktest({ strategy_id: compareId, ...base })
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
  const portfolioEvents = useMemo<(TradeEvent & { strategy: string })[]>(() => {
    if (!portfolioResult) return [];
    const out: (TradeEvent & { strategy: string })[] = [];
    for (const t of portfolioResult.trade_list) {
      out.push({ at: t.entry_at, action: "Bought", symbol: t.symbol, price: t.entry_price, qty: t.qty, reason: t.entry_reason, strategy: t.strategy_name });
      if (t.exit_at) {
        out.push({ at: t.exit_at, action: "Sold", symbol: t.symbol, price: t.exit_price, pnl: t.pnl, reason: t.exit_reason, strategy: t.strategy_name });
      }
    }
    out.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
    return out;
  }, [portfolioResult]);

  function togglePortfolioId(id: number) {
    setPortfolioIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  }

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
        timeframe,
        starting_cash: cash,
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
          <button type="button" className={mode === "single" ? "active" : ""} onClick={() => setMode("single")}>
            Single strategy
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
                    History (days) <InfoTip k="history_days" />
                  </span>
                  <NumberField min={7} max={730} step={1} value={days} onChange={setDays} />
                </label>
                <label>
                  <span className="field-cap">
                    Bar size <InfoTip k="bar" />
                  </span>
                  <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                    <option value="15Min">15 minutes (slow, precise)</option>
                    <option value="1Hour">1 hour (recommended)</option>
                    <option value="1Day">1 day (fast, coarse)</option>
                  </select>
                </label>
                <label>
                  <span className="field-cap">
                    Starting cash ($) <InfoTip k="starting_cash" />
                  </span>
                  <NumberField min={100} step="any" value={cash} onChange={setCash} />
                </label>
                <label>
                  <span className="field-cap">
                    Spread cost per side (%) <InfoTip k="spread_cost" />
                  </span>
                  <NumberField min={0} max={2} step={0.05} value={spread} onChange={setSpread} />
                </label>
              </div>
              <p className="hint">
                One shared account across all picked strategies — set the starting cash to the whole account you'd give
                the book, not a single sleeve.
              </p>
              {portfolioError && <div className="error">{portfolioError}</div>}
              <button disabled={portfolioBusy || portfolioIds.length === 0}>
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
                <div className="stats">
                  <Stat
                    label="Net P&L"
                    value={`$${portfolioResult.net_pnl.toLocaleString()} (${portfolioResult.net_pnl_pct >= 0 ? "+" : ""}${portfolioResult.net_pnl_pct}%)`}
                    tone={portfolioResult.net_pnl >= 0 ? "up" : "down"}
                  />
                  <Stat label="Trades" value={String(portfolioResult.trades)} />
                  <Stat label="Win rate" value={portfolioResult.win_rate != null ? `${portfolioResult.win_rate}%` : "—"} />
                  <Stat label="Avg win / loss" value={`${portfolioResult.avg_win ?? "—"} / ${portfolioResult.avg_loss ?? "—"}`} />
                  <Stat label="Profit factor" value={portfolioResult.profit_factor != null ? String(portfolioResult.profit_factor) : "—"} />
                  <Stat
                    label="Max drawdown"
                    value={`${portfolioResult.max_drawdown_pct}%`}
                    tone={portfolioResult.max_drawdown_pct > 10 ? "down" : undefined}
                  />
                </div>

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
                  <span className="hint">(realized P&amp;L sums to the portfolio total ${portfolioResult.realized_total.toLocaleString()})</span>
                </h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Strategy</th>
                        <th>Realized P&L</th>
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
                          <td>{c.share_pct != null ? `${c.share_pct}%` : "—"}</td>
                          <td>{c.trades}</td>
                          <td>{c.win_rate != null ? `${c.win_rate}%` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

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

      {mode === "single" && (
      <>
      <div className="card">
        <p className="hint">
          Replays a strategy's exact rules over past prices — the same code the live engine runs. It defaults to the
          strategy's <strong>own universe</strong> (a "today's risers" strategy replays the historical risers; a basket
          tests its symbols) — override below to test something else. Honest limits: fills are modeled as price ± the
          spread cost, and the free IEX feed sees a slice of the market. <strong>Past results predict nothing</strong> —
          a backtest can only kill bad ideas cheaply, not promise good ones.
        </p>
        <form className="backtest-form" onSubmit={run}>
          {/* WHAT to test: strategy + symbols. Kept apart from the params row
              because the symbol picker is a tall, composite control and would
              otherwise leave the shorter fields ragged. */}
          <div className="filter-grid">
            <label>
              <span className="field-cap">Strategy</span>
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
              <span className="field-cap">Compare against (optional)</span>
              <select value={compareId ?? ""} onChange={(e) => setCompareId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">— none —</option>
                {strategies
                  .filter((s) => s.id !== strategyId)
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} ({s.asset_class})
                    </option>
                  ))}
              </select>
            </label>
            {/* a composite widget, not a single control — <label> would
                re-dispatch clicks into it */}
            <div className="field">
              <span className="field-cap">Symbols — override (none picked = your watchlist)</span>
              <SymbolPicker assetClass={assetClass} value={symbols} onChange={setSymbols} multi disabled={scannerReplay} />
              {baskets.length > 0 && (
                <select
                  className="load-basket"
                  value=""
                  disabled={scannerReplay}
                  onChange={(e) => e.target.value && loadBasket(Number(e.target.value))}
                >
                  <option value="">Load basket ▾</option>
                  {baskets.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name} ({b.count})
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>
          {universeLabel && (
            <p className="hint">
              Testing this strategy as configured — universe: <strong>{universeLabel}</strong>. Change the controls here
              to override.
            </p>
          )}
          {/* Scanner replay is ONLY meaningful for a scanner-universe strategy —
              it reconstructs each day's top risers. A basket / custom / watchlist
              strategy has a FIXED universe, so replaying scanner risers would test
              a universe the strategy never trades. Hidden for those (the backtest
              honours whatever universe the strategy is set to). */}
          {strategy?.universe === "scanner" && (
            <>
              <label className="check">
                <input
                  type="checkbox"
                  checked={scannerReplay}
                  onChange={(e) => setScannerReplay(e.target.checked)}
                />
                Scanner replay — test against the historical <strong>top risers each day</strong> (not a fixed list){" "}
                <InfoTip k="scanner_replay" />
              </label>
              {scannerReplay && (
                <>
                  <label style={{ display: "block", marginTop: 8 }}>
                    <span className="field-cap">
                      Risers per day (top N) <InfoTip k="replay_top_n" />
                    </span>
                    <NumberField min={1} max={100} step={1} value={replayTopN} onChange={setReplayTopN} />
                  </label>
                  <p className="hint">
                    Each day, only that day's <strong>top {replayTopN}</strong> risers are eligible to enter; your
                    strategy's entry rules then decide. The cache stores a wide set, so changing this number re-runs
                    instantly — no re-sweep. Needs a completed sweep first (Settings → Historical bar cache). If you've
                    also run an <strong>intraday sweep</strong>, replay uses 15-minute bars so intraday exits
                    (flatten-before-close, VWAP, the entry window) behave for real; otherwise it falls back to daily
                    bars, which can't simulate those. Works for stock and crypto strategies — each replays off its own
                    cache.
                  </p>
                </>
              )}
            </>
          )}
          {/* HOW to test: numeric/timeframe params, all uniform height. */}
          <div className="filter-grid">
            <label>
              <span className="field-cap">
                History (days) <InfoTip k="history_days" />
              </span>
              <NumberField min={7} max={730} step={1} value={days} onChange={setDays} />
            </label>
            <label>
              <span className="field-cap">
                Bar size <InfoTip k="bar" />
              </span>
              <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                <option value="15Min" disabled={usesDailySignals}>15 minutes (slow, precise)</option>
                <option value="1Hour" disabled={usesDailySignals}>1 hour (recommended)</option>
                <option value="1Day" disabled={usesVwap}>1 day (fast, coarse)</option>
              </select>
              {usesDailySignals && (
                <span className="field-help warn">
                  <IconWarn className="icon-inline" /> This strategy uses MACD/RSI (daily signals), so the bar size
                  can't be changed — it's fixed to 1 Day so the backtest matches the live engine.
                </span>
              )}
              {usesVwap && (
                <span className="field-help warn">
                  <IconWarn className="icon-inline" /> This strategy uses VWAP (an intraday measure), so the bar size
                  can't be set to 1 Day.
                </span>
              )}
            </label>
            <label>
              <span className="field-cap">
                Starting cash ($) <InfoTip k="starting_cash" />
              </span>
              <NumberField min={100} step="any" value={cash} onChange={setCash} />
            </label>
            <label>
              <span className="field-cap">
                Spread cost per side (%) <InfoTip k="spread_cost" />
              </span>
              <NumberField min={0} max={2} step={0.05} value={spread} onChange={setSpread} />
            </label>
          </div>
          {basketNote && <p className="hint">{basketNote}</p>}
          <p className="hint">
            Loading a basket fills the symbols above so you can see exactly what's tested. A backtest tests the{" "}
            <strong>whole basket's symbol set</strong> over history — it can't reconstruct the historical daily top-N
            ranking that the live engine does, so top-N is a live entry-selection feature only.
          </p>
          {error && <div className="error">{error}</div>}
          <button disabled={busy || strategyId === null}>{busy ? "Replaying history…" : "Run backtest"}</button>
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
                ];
                return (
                  <div className="compare-table">
                    <h4>
                      Head-to-head{" "}
                      <span className="hint">(same universe, period &amp; cash — only the strategy rules differ)</span>
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

            {result.trades > 0 && (
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
              markers={compareResult ? [] : markers}
              noTradeReasons={compareResult ? undefined : result.no_trade_reasons}
              series={[
                {
                  label: compareResult ? strategies.find((s) => s.id === strategyId)?.name ?? "Strategy A" : "This strategy",
                  color: "var(--accent)",
                  values: result.equity,
                },
                ...(compareResult
                  ? [
                      {
                        label: strategies.find((s) => s.id === compareId)?.name ?? "Strategy B",
                        color: "#a78bfa",
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
          </div>
          <div className="card">
            <h3>
              Trade log — every buy and sell in order{" "}
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
                {events.map((ev, i) => (
                  <tr key={i}>
                    <td>{new Date(ev.at).toLocaleDateString()}</td>
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
    </>
  );
}
