export interface BrokerInfo {
  account_number: string;
  status: string;
  equity: string;
  cash: string;
  buying_power: string;
  currency: string;
}

export interface MarketInfo {
  is_open: boolean;
  next_open: string;
  next_close: string;
  timestamp: string;
}

export interface StatusResponse {
  version: string;
  trading_mode: string;
  alpaca_configured: boolean;
  data_persistent: boolean | null;
  data_persistent_reason: string;
  secrets_without_key: boolean;
  instance_key_created_at: string | null;
  last_tick_at: string | null;
  broker: BrokerInfo | null;
  market: MarketInfo | null;
  error: string | null;
}

export interface StrategyParams {
  entry: {
    min_day_gain_pct: number;
    max_day_gain_pct: number;
    min_price: number;
    max_price: number;
    require_above_vwap: boolean;
    require_macd_bullish?: boolean; // optional daily-MACD entry filter (off by default)
    rsi_min?: number; // RSI entry band floor (0 = off)
    rsi_max?: number; // RSI entry band cap — avoid overbought entries (0 = off)
    entry_window_start: string | null;
    entry_window_end: string | null;
    entry_slippage_pct: number;
  };
  exit: {
    trailing_stop_pct: number;
    stop_loss_pct: number;
    take_profit_pct: number;
    max_holding_hours: number;
    flatten_before_close: boolean;
    exit_below_vwap: boolean;
    exit_on_macd_bearish?: boolean; // optional daily-MACD exit signal (off by default)
    exit_rsi_above?: number; // sell when RSI >= this (overbought take-profit); 0 = off
    exit_on_regime_bear?: boolean; // stocks: sell to cash when SPY < its 200-day MA (live-only)
    rotate_on_rank_dropout?: boolean; // basket rotation: sell when it leaves the top-N
    exit_slippage_pct: number;
    exit_slippage_max_pct: number;
  };
  // Shared MACD periods for the optional entry/exit MACD signals. Absent = the
  // 12/26/9 defaults; only meaningful when a MACD toggle above is on.
  macd?: { fast: number; slow: number; signal: number };
  // Present only on a DCA baseline sleeve: buy the fixed symbol list every
  // interval_days as independent lots. Absent or <= 0 = not a DCA strategy.
  dca?: { interval_days: number };
  // Optional ATR-based stops & sizing (both off by default). stop_mult > 0 sets
  // the hard stop at stop_mult × ATR% below entry; risk_usd > 0 (needs stop_mult)
  // sizes each position so a stop-out loses ~risk_usd. period is the ATR lookback.
  atr?: { period: number; stop_mult: number; risk_usd: number };
  // Order execution mode. Off/absent = marketable LIMIT orders + whole shares
  // (the price-protected default). market_orders = plain MARKET orders sized by
  // dollar notional, so a small $-per-trade can buy a fractional slice of an
  // expensive name — no limit protection.
  execution?: { market_orders: boolean };
}

export type RankBy = "momentum_today" | "return_30d" | "relative_strength" | "rs_vs_spy" | "rsi";

export interface StrategyRow {
  id: number;
  name: string;
  enabled: boolean;
  asset_class: "stock" | "crypto";
  universe: "scanner" | "watchlist" | "both" | "basket" | "custom";
  basket_id: number | null;
  symbols: string[];
  rank_by: RankBy;
  top_n: number;
  rank_enabled?: boolean; // rank the pool + keep top N (basket always; watchlist/custom opt in)
  preset: string;
  params: StrategyParams;
  sizing_usd: number;
  sleeve_usd: number;
  max_positions: number;
  swing_mode: boolean;
  ignore_regime: boolean;
  // Your own freeform notes. Never read by the engine, and editing them does not
  // create a new config version — a note changes no behaviour.
  notes?: string;
  // Set only on a strategy a parameter search produced: what it was searched
  // from, and over how many days. Lets a later run say which generation it is.
  optimized_from_id?: number | null;
  optimized_days?: number | null;
  // Let this strategy hold a symbol another strategy already holds. Never
  // relaxes the wash-sale guard or the loss cooldown — those stay account-wide.
  allow_concurrent_symbol?: boolean;
  open_trades?: number;
  version?: number;
}

export interface Preset {
  label: string;
  description: string;
  asset_class: "stock" | "crypto";
  universe: string;
  swing_mode: boolean;
  rank_by?: RankBy; // basket presets carry a ranking + count
  top_n?: number;
  symbols?: string[]; // custom-universe presets (e.g. DCA sleeve) can seed a symbol list
  params: StrategyParams;
}

export interface RiskConfig {
  max_daily_loss_usd: number;
  max_daily_loss_pct: number;
  max_total_positions: number;
  max_total_exposure_usd: number;
  max_trades_per_day: number;
  cooldown_hours_after_loss: number;
  wash_sale_guard: "block" | "warn" | "off";
  leverage_enabled: boolean;
}

export interface EngineState {
  mode: string;
  modes: string[];
  risk: RiskConfig;
  regime: { ok: boolean; detail: string; insufficient_data?: boolean } | null;
  regime_filter_enabled: boolean;
  leverage: { unlockable: boolean; enabled: boolean };
  slack_configured: boolean;
  today: { realized_pnl: number; open_positions: number; entries: number };
}

export interface JournalRow {
  id: number;
  strategy: string;
  mode: string;
  symbol: string;
  asset_class: string;
  status: string;
  logged_at: string | null;
  qty: number;
  notional: number;
  entry_price: number | null;
  entry_at: string | null;
  entry_reason: string;
  exit_price: number | null;
  exit_at: string | null;
  exit_reason: string;
  pnl: number | null;
  // Broker fees for this one trade. Null = not known per trade, and that is the
  // normal case: Alpaca's fee activities carry no order id and only a date, so
  // no fee can be tied to a single fill. Render "—", never $0.00. Account-level
  // totals come from getFeeSummary().
  fees: number | null;
  config_version_id: number | null;
}

export interface FeeSummary {
  total_usd: number | null; // null = unknown (nothing synced), NOT zero
  is_estimate: boolean; // true if any part was valued at the broker's mark
  activities: number;
  unvalued: number; // fees Alpaca sent that we could not value in dollars
  synced_through: string | null;
  by_symbol: { symbol: string; usd: number }[];
}

export interface ScoreboardTrade {
  kind: "buy" | "sell";
  symbol: string;
  strategy: string;
  qty: number;
  price: number | null;
  pnl: number | null; // sells only
  reason: string;
}

export interface Scoreboard {
  days: string[];
  bot: (number | null)[];
  spy: (number | null)[];
  btc: (number | null)[];
  verdict: string | null;
  account?: string | null; // the broker account this series is scoped to
  base_equity?: number; // equity every point is measured against ($ → % points)
  // {day -> that day's buys and sells}, same UTC day key as `days` and scoped to
  // the same account. The chart had none of this and still claimed "no trades".
  trades?: Record<string, ScoreboardTrade[]>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const getStrategies = () => fetch("/api/strategies").then((r) => handle<StrategyRow[]>(r));

export interface Holding {
  symbol: string;
  asset_class: string;
  mode: string;
  qty: number;
  entry_price: number | null;
  notional: number;
  entry_at: string | null;
  current_price: number | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  unrealized_pct: number | null;
}

export interface StrategyHoldings {
  strategy_id: number;
  holdings: Holding[];
  total_cost: number;
  total_value: number;
  total_unrealized_pnl: number;
}

export const getStrategyHoldings = (id: number) =>
  fetch(`/api/strategies/${id}/holdings`).then((r) => handle<StrategyHoldings>(r));

export interface LastRunCandidate {
  symbol: string;
  price: number;
  change_pct: number;
  macd_bullish: boolean | null;
  // Where it placed in the strategy's ranking this cycle (1 = best). null on an
  // unranked universe. The engine takes candidates strictly best-first, so a
  // high number means everything above it was held or failed the rules.
  rank: number | null;
  rank_of: number | null;
  decision: string; // bought | skipped | blocked
  reason: string;
}

export interface StrategyLastRun {
  ran: boolean;
  enabled?: boolean;
  ran_at?: string;
  mode?: string;
  universe?: string;
  outcome?: string;
  candidates?: LastRunCandidate[];
}

export const getStrategyLastRun = (id: number) =>
  fetch(`/api/strategies/${id}/last-run`).then((r) => handle<StrategyLastRun>(r));

export interface RankingRow {
  symbol: string;
  rank: number | null;
  value: number | null;
  in_top_n: boolean;
  price: number | null;
  change_pct: number | null;
  macd_bullish?: boolean | null; // daily MACD direction — informational, doesn't affect rank
}

export interface StrategyRanking {
  ranked: boolean;
  reason?: string;
  rank_by?: string;
  rank_label?: string;
  top_n?: number;
  rows?: RankingRow[];
  error?: string | null;
}

export const getStrategyRanking = (id: number) =>
  fetch(`/api/strategies/${id}/ranking`).then((r) => handle<StrategyRanking>(r));
export const getPresets = () => fetch("/api/strategies/presets").then((r) => handle<Record<string, Preset>>(r));
export const createStrategy = (b: Partial<StrategyRow>) =>
  fetch("/api/strategies", json(b)).then((r) => handle<StrategyRow>(r));
export const updateStrategy = (id: number, b: Partial<StrategyRow>) =>
  fetch(`/api/strategies/${id}`, { ...json(b), method: "PUT" }).then((r) => handle<StrategyRow>(r));
export const toggleStrategy = (id: number) =>
  fetch(`/api/strategies/${id}/toggle`, { method: "POST" }).then((r) => handle<StrategyRow>(r));
export const deleteStrategy = (id: number) =>
  fetch(`/api/strategies/${id}`, { method: "DELETE" }).then((r) => handle(r));

export interface BasketMember {
  symbol: string;
  asset_class: "stock" | "crypto";
  in_directory: boolean;
}

export interface Basket {
  id: number;
  name: string;
  builtin: boolean;
  created_at: string | null;
  count: number;
  symbols: BasketMember[];
}

export const getBaskets = () => fetch("/api/baskets").then((r) => handle<Basket[]>(r));
export const createBasket = (name: string) =>
  fetch("/api/baskets", json({ name })).then((r) => handle<Basket>(r));
export const renameBasket = (id: number, name: string) =>
  fetch(`/api/baskets/${id}`, { ...json({ name }), method: "PUT" }).then((r) => handle<Basket>(r));
export const deleteBasket = (id: number) =>
  fetch(`/api/baskets/${id}`, { method: "DELETE" }).then((r) => handle(r));
export const addBasketItem = (id: number, symbol: string, assetClass: "stock" | "crypto") =>
  fetch(`/api/baskets/${id}/items`, json({ symbol, asset_class: assetClass })).then((r) => handle<Basket>(r));
export const removeBasketItem = (id: number, symbol: string, assetClass: string) =>
  fetch(`/api/baskets/${id}/items/${assetClass}/${encodeURIComponent(symbol)}`, { method: "DELETE" }).then((r) =>
    handle<Basket>(r),
  );

export const getEngine = () => fetch("/api/engine").then((r) => handle<EngineState>(r));

// Account-wide open positions with their owning strategy — the Dashboard view
// that answers "which strategy already holds the symbol that blocked my entry".
export interface OpenPositionRow {
  trade_id: number;
  strategy_id: number;
  strategy_name: string;
  symbol: string;
  asset_class: string;
  mode: string;
  qty: number;
  entry_price: number;
  notional: number | null;
  entry_at: string | null;
  entry_reason: string;
  current_price: number | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  unrealized_pct: number | null;
}

export interface OpenPositionsResponse {
  positions: OpenPositionRow[];
  total_cost: number;
  total_value: number;
  total_unrealized_pnl: number;
}

export const getOpenPositions = () =>
  fetch("/api/engine/positions").then((r) => handle<OpenPositionsResponse>(r));
/** Sell one open position immediately, at market, ignoring its strategy's rules. */
export const forceClosePosition = (tradeId: number) =>
  fetch(`/api/engine/positions/${tradeId}/close`, { method: "POST" }).then((r) =>
    handle<{ ok: boolean; symbol: string; mode: string }>(r),
  );
export const setEngineMode = (mode: string, confirm = false) =>
  fetch("/api/engine/mode", json({ mode, confirm })).then((r) => handle<{ mode: string }>(r));
export const setRisk = (risk: RiskConfig & { leverage_confirm?: string }) =>
  fetch("/api/engine/risk", { ...json(risk), method: "PUT" }).then((r) => handle<RiskConfig>(r));
export const setRegimeEnabled = (enabled: boolean) =>
  fetch("/api/engine/regime", { ...json({ enabled }), method: "PUT" }).then((r) => handle(r));
export const setSlack = (url: string) =>
  fetch("/api/engine/slack", { ...json({ url }), method: "PUT" }).then((r) => handle(r));
export const testSlack = () => fetch("/api/engine/slack/test", { method: "POST" }).then((r) => handle(r));

export interface SlackPrefCategory {
  key: string;
  label: string;
  description: string;
  enabled: boolean;
}
export interface SlackPrefs {
  configured: boolean;
  categories: SlackPrefCategory[];
}
export const getSlackPrefs = () => fetch("/api/engine/slack/prefs").then((r) => handle<SlackPrefs>(r));
export const setSlackPrefs = (prefs: Record<string, boolean>) =>
  fetch("/api/engine/slack/prefs", { ...json({ prefs }), method: "PUT" }).then((r) => handle<{ prefs: Record<string, boolean> }>(r));
/** Row cap. Passed explicitly so the page can tell a full page from a truncated
 *  one — a silently cut list reads as "this is everything". */
export const JOURNAL_LIMIT = 300;

export const getJournal = (
  mode?: string,
  status?: string,
  assetClass?: string,
  account?: string,
  limit: number = JOURNAL_LIMIT,
) => {
  const qs = new URLSearchParams();
  if (mode) qs.set("mode", mode);
  if (status) qs.set("status", status);
  if (assetClass) qs.set("asset_class", assetClass);
  if (account) qs.set("account", account);
  qs.set("limit", String(limit));
  const q = qs.toString();
  return fetch(`/api/engine/journal${q ? `?${q}` : ""}`).then((r) => handle<JournalRow[]>(r));
};
export const getFeeSummary = (account?: string) => {
  const qs = account ? `?account=${encodeURIComponent(account)}` : "";
  return fetch(`/api/engine/fees${qs}`).then((r) => handle<FeeSummary>(r));
};
export const getScoreboard = () => fetch("/api/engine/scoreboard").then((r) => handle<Scoreboard>(r));

export interface AccountRow {
  id: string | null;
  trades: number;
  is_current: boolean;
  untagged: boolean;
}
export interface AccountsResponse {
  current: string | null;
  accounts: AccountRow[];
}
export const getAccounts = () => fetch("/api/engine/accounts").then((r) => handle<AccountsResponse>(r));

export interface StrategyPnl {
  mode: string;
  realized_total: number;
  // Open positions marked to live quotes. Separate from realized on purpose —
  // one is money you have, the other is money you might.
  unrealized_total: number;
  // Open positions with no live mark, account-wide. Non-zero means the
  // unrealized figures are a floor, not the whole picture.
  unpriced_positions: number;
  strategies: {
    strategy_id: number;
    name: string;
    realized_pnl: number;
    trades: number;
    wins: number;
    win_rate: number | null;
    open_positions: number;
    // 0 = nothing held. null = holding something we couldn't price — not the
    // same fact, and not shown the same way.
    unrealized_pnl: number | null;
    unpriced_positions: number;
  }[];
}
export interface StrategyPnlDaily {
  mode: string;
  days: string[];
  strategies: { strategy_id: number; name: string; values: number[]; total: number }[];
}
export const getStrategyPnl = (account?: string) =>
  fetch(`/api/engine/strategy-pnl${account ? `?account=${encodeURIComponent(account)}` : ""}`).then((r) =>
    handle<StrategyPnl>(r),
  );
export const getStrategyPnlDaily = (days = 30, account?: string) => {
  const qs = new URLSearchParams({ days: String(days) });
  if (account) qs.set("account", account);
  return fetch(`/api/engine/strategy-pnl-daily?${qs}`).then((r) => handle<StrategyPnlDaily>(r));
};

export interface BarCacheStats {
  daily_symbols: number;
  movers_days: number;
  intraday_bars: number;
  latest_day: string | null;
  freshest_mover: { symbol: string; day: string; change_pct: number; has_intraday: boolean } | null;
}

export interface BarCacheStatus {
  running: boolean;
  kind: string; // daily | reconstruct | intraday
  market: string; // stock | crypto — which cache the current/last run touched
  phase: string; // reconstruct sub-phase: "loading bars" | "ranking days"
  started_at: string | null;
  last_run_at: string | null;
  batches_total: number;
  batches_done: number;
  symbols_total: number;
  symbols_saved: number;
  days_reconstructed: number;
  intraday_bars: number;
  has_intraday: boolean;
  crypto_has_intraday: boolean;
  errors: number;
  last_error: string | null;
  // Persisted cache totals (from the DB) — survive redeploys; null while a sweep runs.
  cache: BarCacheStats | null;
  crypto_cache: BarCacheStats | null;
  backend: { kind: string; scheme: string; host: string | null };
}

export const getBarCacheStatus = () => fetch("/api/barcache/status").then((r) => handle<BarCacheStatus>(r));
export const runBarSweep = (days?: number) =>
  fetch(`/api/barcache/sweep${days ? `?days=${days}` : ""}`, { method: "POST" }).then((r) => handle(r));
// Re-rank movers from bars already cached — no re-download.
export const runBarReconstruct = () =>
  fetch("/api/barcache/reconstruct", { method: "POST" }).then((r) => handle(r));
// Pull intraday bars for the movers — enables intraday scanner replay.
export const runIntradaySweep = () =>
  fetch("/api/barcache/sweep-intraday", { method: "POST" }).then((r) => handle(r));
// Crypto: sweep every USD pair's daily bars + rank movers, then its 15-min bars.
export const runCryptoSweep = (days?: number) =>
  fetch(`/api/barcache/sweep-crypto${days ? `?days=${days}` : ""}`, { method: "POST" }).then((r) => handle(r));
export const runCryptoIntradaySweep = () =>
  fetch("/api/barcache/sweep-crypto-intraday", { method: "POST" }).then((r) => handle(r));

export interface AssetRow {
  symbol: string;
  name: string;
  asset_class: "stock" | "crypto";
  exchange: string;
  fractionable: boolean;
}

export interface AssetStatus {
  count: number;
  stocks: number;
  crypto: number;
  updated_at: string | null;
  stale: boolean;
}

export function searchAssets(q: string, assetClass?: string): Promise<AssetRow[]> {
  const params = new URLSearchParams({ q });
  if (assetClass) params.set("asset_class", assetClass);
  return fetch(`/api/assets/search?${params}`).then((r) => handle(r));
}

export const getAssetStatus = () => fetch("/api/assets/status").then((r) => handle<AssetStatus>(r));
export const syncAssets = () => fetch("/api/assets/sync", { method: "POST" }).then((r) => handle<AssetStatus>(r));

export interface BacktestTrade {
  symbol: string;
  qty: number;
  entry_price: number;
  entry_at: string;
  entry_day: string;
  entry_reason: string;
  exit_price: number;
  exit_at: string | null;
  exit_day: string | null;
  exit_reason: string;
  pnl: number | null;
}

export interface BacktestResult {
  strategy_name: string;
  symbols: string[];
  scanner_replay?: boolean;
  replay_intraday?: boolean;
  intraday_topped_up?: boolean; // bars were downloaded during this run
  // Symbol-days an intraday replay covered with the DAILY bar because no 15-min
  // bars were cached for them — those days had stops checked at daily resolution.
  daily_filled_days?: number;
  // Stretches of the window with NO bars. The chart draws a straight line across
  // them; positions were marked at a stale price and no stop could fire there.
  bar_gaps?: { after: string; before: string; days: number }[];
  replay_top_n?: number;
  universe_size?: number; // symbols actually REPLAYED (not merely movers)
  universe_dropped?: string[]; // movers with no bars at the chosen resolution
  intraday_covered?: number; // how many movers the intraday cache covers
  days_replayed?: number;
  // The bar size actually REPLAYED — what the entries and exits were checked on.
  timeframe: string;
  // Mixed-resolution run: the indicators came from COMPLETED DAILY closes (like
  // the live engine) while entries/exits replayed on `timeframe` bars, so
  // price-triggered stops were simulated for real. Absent/false = one resolution.
  mixed_resolution?: boolean;
  signal_timeframe?: string; // where MACD/RSI came from on a mixed run ("1Day")
  days: number;
  starting_cash: number;
  final_equity: number;
  net_pnl: number;
  net_pnl_pct: number;
  trades: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  max_drawdown_pct: number;
  spread_cost_pct_per_side: number;
  // Commission per side (%) and what it actually took in dollars. Crypto isn't
  // free on Alpaca — 0.15-0.25% a side — so a busy strategy pays real money.
  fee_pct_per_side?: number;
  fees_paid?: number;
  max_deployed_usd: number;
  pct_capital_deployed: number;
  return_on_deployed_pct: number | null;
  time_in_market_pct: number;
  hold_benchmark: (number | null)[] | null;
  hold_benchmark_label: string | null;
  diagnosis: {
    bars_evaluated: number;
    rejected_day_gain: number;
    rejected_vwap: number;
    rejected_entry_window: number;
    entry_ok_but_rail_blocked: number;
    too_small_or_no_cash: number;
    max_day_gain_pct: number | null;
    days_reaching_min_gain: number;
    summary: string | null;
  };
  no_trade_reasons?: Record<string, string>; // {day -> why no entry that day} (chart)
  // Consecutive no-entry days collapsed into spans for the trade log.
  // `reason` counts every bar-check; `reason_symbol_days` re-counts the same
  // rejections as distinct symbol-days (both are shown — see _no_trade_spans).
  no_trade_spans?: {
    from_day: string;
    to_day: string;
    days: number;
    reason: string;
    reason_symbol_days?: string;
  }[];
  equity_days: string[];
  equity: number[];
  benchmark: (number | null)[] | null;
  benchmark_symbol: string | null;
  trade_list: BacktestTrade[];
  // Positions still held when the window ended — marked to market (not sold), so
  // their unrealized P&L is already in net_pnl / the equity curve.
  open_positions: OpenPosition[];
  // {day -> holdings}: what was held each day and each holding's dollar
  // contribution to that day's move (qty 0 = closed that day). Chart hover.
  daily_positions?: Record<string, DayHolding[]>;
}

export interface DayHolding {
  symbol: string;
  qty: number;
  price: number | null;
  day_pnl: number;
}

export interface OpenPosition {
  symbol: string;
  qty: number;
  entry_price: number;
  entry_at: string;
  entry_day: string;
  entry_reason: string;
  mark_price: number;
  unrealized_pnl: number;
  strategy_id?: number; // portfolio only
  strategy_name?: string; // portfolio only
}

/** Run a backtest as a background JOB and wait for it, polling.
 *
 *  A long replay outlives an HTTP request. A 350-day, 30-symbol backtest takes
 *  minutes, and every proxy gives up first: nginx's default read timeout is 60
 *  seconds and Cloudflare's is a FIXED 100 (HTTP 524 — no plan setting raises
 *  it). The replay itself was fine; the connection died and took the answer with
 *  it. Polling keeps every request to milliseconds, so there's nothing to time
 *  out however long the run takes.
 *
 *  Callers see the same Promise<Result> as before — the waiting lives here so no
 *  page has to know a job exists.
 */
export interface JobProgress {
  phase: string;
  pct: number | null;
}

async function runAsJob<T>(
  startUrl: string,
  body: unknown,
  onProgress?: (p: JobProgress) => void,
): Promise<T> {
  const { job_id } = await fetch(startUrl, json(body)).then((r) => handle<{ job_id: string }>(r));
  // Fast enough to feel instant on a short run, slow enough not to hammer a home
  // server through a 20-minute one.
  const POLL_MS = 1500;
  for (;;) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    const st = await fetch(`/api/backtest/job/${job_id}`).then((r) =>
      handle<{ running: boolean; phase: string; pct: number | null; error: string | null; result: T | null }>(r),
    );
    if (st.running) {
      onProgress?.({ phase: st.phase, pct: st.pct });
      continue;
    }
    if (st.error) throw new Error(st.error);
    if (st.result === null) throw new Error("The backtest finished without a result — check the server log.");
    return st.result;
  }
}

export const runBacktest = (body: {
  strategy_id: number;
  symbols: string[];
  scanner_replay?: boolean;
  replay_top_n?: number;
  days: number;
  timeframe: string;
  starting_cash: number;
  spread_pct: number;
}, onProgress?: (p: JobProgress) => void) =>
  runAsJob<BacktestResult>("/api/backtest/start", body, onProgress);

// Portfolio (multi-strategy) backtest: N strategies over the SAME period sharing
// ONE account + the global rails, with a per-strategy contribution breakdown.
export interface PortfolioContribution {
  strategy_id: number;
  strategy_name: string;
  realized_pnl: number;
  unrealized_pnl: number; // from positions still open at the window end
  trades: number;
  wins: number;
  win_rate: number | null;
  share_pct: number | null; // sign-preserving share of the portfolio total P&L
}

export interface PortfolioBacktestTrade extends BacktestTrade {
  strategy_id: number;
  strategy_name: string;
}

export interface PortfolioBacktestResult {
  strategy_count: number;
  strategy_names: string[];
  timeframe: string;
  days: number;
  starting_cash: number;
  final_equity: number;
  net_pnl: number;
  net_pnl_pct: number;
  trades: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  max_drawdown_pct: number;
  spread_cost_pct_per_side: number;
  // Commission per side (%) and what it actually took in dollars. Crypto isn't
  // free on Alpaca — 0.15-0.25% a side — so a busy strategy pays real money.
  fee_pct_per_side?: number;
  fees_paid?: number;
  max_deployed_usd: number;
  pct_capital_deployed: number;
  return_on_deployed_pct: number | null;
  time_in_market_pct: number;
  realized_total: number;
  contributions: PortfolioContribution[];
  equity_days: string[];
  equity: number[];
  hold_benchmark: (number | null)[] | null;
  hold_benchmark_label: string | null;
  trade_list: PortfolioBacktestTrade[];
  open_positions: OpenPosition[];
}

export const runPortfolioBacktest = (body: {
  strategy_ids: number[];
  days: number;
  timeframe: string;
  starting_cash: number;
  spread_pct: number;
}, onProgress?: (p: JobProgress) => void) =>
  runAsJob<PortfolioBacktestResult>("/api/backtest/portfolio/start", body, onProgress);

// ---- Strategy optimizer (parameter search) ----
// Searches a momentum strategy's parameter space with the SAME backtester,
// splitting history into in-sample (searched) and out-of-sample (validation).
// Only the out-of-sample number is treated as real. NOT "AI" — a parameter search.

export interface OptimizerMetrics {
  net_pnl_pct: number | null;
  trades: number | null;
  // Closed trades + positions still open at the slice end — the honest sample
  // size now that held-to-end positions aren't force-sold into fake trades.
  entries?: number | null;
  win_rate: number | null;
  return_on_deployed_pct: number | null;
  max_drawdown_pct: number | null;
}

export interface OptimizerComboParams {
  min_day_gain_pct: number;
  trailing_stop_pct: number;
  // The two stops are mutually exclusive: an ATR-stop strategy searches the
  // multiplier INSTEAD of the fixed %, because the ATR stop replaces it outright
  // (see evaluate_exit) — so exactly one of these is present on any given row.
  stop_loss_pct?: number;
  atr_stop_mult?: number;
  take_profit_pct: number;
  rsi_max?: number; // searched only when the strategy uses an RSI entry cap
  rsi_min?: number; // searched only when the strategy uses an RSI entry floor
  exit_rsi_above?: number; // searched only when the strategy uses the RSI exit
  macd_slow?: number; // MACD slow-EMA period — searched only when the strategy uses MACD
}

export interface OptimizerResultRow {
  rank: number;
  params: OptimizerComboParams;
  in_sample_score: number | null;
  in_sample: OptimizerMetrics | null;
  out_of_sample: OptimizerMetrics | null;
  is_best: boolean;
}

export interface OptimizerWindow {
  start: string;
  end: string;
  days: number;
}

export interface OptimizerNeighbourPoint {
  value: number;
  score: number | null;
  is_best: boolean;
}

export interface OptimizerResult {
  tested_combinations: number;
  search_space_size?: number;
  no_trade_reason?: string | null;
  iterations: number;
  in_sample_window: OptimizerWindow;
  out_of_sample_window: OptimizerWindow;
  results: OptimizerResultRow[];
  best: OptimizerResultRow | null;
  best_draft_params: StrategyParams;
  relative_step?: number; // the step size every grid was built from
  // How many times this strategy's ancestry has already been searched. generation
  // 1 = a hand-built strategy; 2+ means the out-of-sample slice has been picked
  // against before and is no longer independent.
  lineage?: { generation: number; windows: number[]; same_window: number; root: string | null } | null;
  // The strategy's value for each searched knob AT THE TIME OF THE SEARCH — the
  // "before" of the before/after. in_grid is false when that value wasn't one of
  // the coarse grid values, i.e. the search never actually evaluated it.
  baseline_params?: Record<string, { value: number | null; in_grid: boolean }>;
  neighbourhood: Record<string, OptimizerNeighbourPoint[]>;
  hold_benchmark_comparison: {
    strategy_out_of_sample_pct: number | null;
    hold_out_of_sample_pct: number | null;
    hold_label: string | null;
    beat_hold: boolean | null;
  };
  symbols: string[];
  warnings: string[];
  strategy_name?: string;
  timeframe?: string; // the bar size REPLAYED (what the searched stops were checked on)
  // Mixed resolution: signals from `signal_timeframe` (daily), replay on `timeframe`.
  mixed_resolution?: boolean;
  signal_timeframe?: string;
  days?: number;
  scanner_replay?: boolean;
  replay_intraday?: boolean;
  intraday_topped_up?: boolean; // bars were downloaded during this run
  // Symbol-days an intraday replay covered with the DAILY bar because no 15-min
  // bars were cached for them — those days had stops checked at daily resolution.
  daily_filled_days?: number;
  // Stretches of the window with NO bars. The chart draws a straight line across
  // them; positions were marked at a stale price and no stop could fire there.
  bar_gaps?: { after: string; before: string; days: number }[];
  replay_top_n?: number;
  universe_size?: number; // movers actually SEARCHED (not merely listed)
  universe_dropped?: string[]; // movers with no bars at the chosen resolution
  intraday_covered?: number;
  days_replayed?: number;
}

export interface OptimizerStatus {
  running: boolean;
  phase: string; // "downloading bars" | "searching" | "validating" | "done"
  strategy_name: string;
  started_at: string | null;
  finished_at: string | null;
  combos_total: number;
  combos_done: number;
  error: string | null;
  result: OptimizerResult | null;
}

export const startOptimizer = (body: {
  strategy_id: number;
  symbols: string[];
  scanner_replay?: boolean;
  replay_top_n?: number;
  days: number;
  timeframe: string;
  iterations: number;
  starting_cash: number;
  spread_pct: number;
  relative_step?: number; // 0.15 = each value tried is 15% up or down from the last
}) => fetch("/api/optimizer", json(body)).then((r) => handle<{ ok: boolean; started: boolean; symbols: string[]; iterations: number; scanner_replay: boolean }>(r));

export const getOptimizerStatus = () =>
  fetch("/api/optimizer/status").then((r) => handle<OptimizerStatus>(r));

// Basket sweep: the parameter search across EVERY basket, ranked by the
// out-of-sample margin over SPY. Same background-task + status shape.
export interface SweepRow {
  rank: number;
  untested: boolean; // no OOS trades / no margin — ranked last regardless of numbers
  basket_id: number;
  basket_name: string;
  symbols: string[];
  tested_combinations: number;
  search_space_size: number;
  best_params: Record<string, number>;
  best_draft_params: StrategyParams;
  in_sample_pct: number | null;
  oos_pct: number | null;
  oos_trades: number | null;
  oos_entries?: number | null; // closed + still open at the OOS end

  oos_win_rate: number | null;
  oos_max_drawdown_pct: number | null;
  oos_window: OptimizerWindow;
  hold_oos_pct: number | null;
  beat_hold: boolean | null;
  spy_oos_pct: number | null;
  margin_vs_spy: number | null;
  warnings: string[];
  no_trade_reason: string | null;
}

export interface SweepResult {
  rows: SweepRow[];
  skipped: { basket_name: string; reason: string }[];
  iterations: number;
  days: number;
  spy_available: boolean;
  template_sizing: { sizing_usd: number; sleeve_usd: number; max_positions: number };
}

export interface SweepStatus {
  running: boolean;
  phase: string; // "downloading bars" | "searching" | "done"
  baskets_total: number;
  baskets_done: number;
  current_basket: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  result: SweepResult | null;
}

export const startBasketSweep = (body: { days: number; iterations: number; spread_pct?: number }) =>
  fetch("/api/optimizer/sweep", json(body)).then(
    (r) => handle<{ ok: boolean; started: boolean; baskets: number; iterations: number }>(r),
  );

export const getBasketSweepStatus = () =>
  fetch("/api/optimizer/sweep/status").then((r) => handle<SweepStatus>(r));

/** Build an Error whose message is NEVER empty.
 *
 *  An empty message is worse than a wrong one. Every page renders its error with
 *  a truthiness check (`{error && <div…>}`), so `new Error("")` shows the user
 *  absolutely nothing — the button just appears not to work, and clicking again
 *  "fixes" it whenever the next request happens to succeed.
 *
 *  Three ways the old code produced an empty message:
 *    - `resp.statusText` is ALWAYS "" over HTTP/2 (the spec dropped reason
 *      phrases), which is what any reverse proxy in front of this container
 *      serves. A 500 became a no-op click.
 *    - A proxy's own error page is HTML, so `.json()` throws and we fell back to
 *      that same empty statusText.
 *    - FastAPI validation errors put an ARRAY in `detail`, which stringified to
 *      "[object Object]".
 */
async function failure(resp: Response): Promise<Error> {
  const body = await resp.text().catch(() => "");
  let detail = "";
  try {
    const j = JSON.parse(body);
    if (typeof j?.detail === "string") detail = j.detail;
    else if (Array.isArray(j?.detail))
      // Pydantic: [{loc: ["body", "days"], msg: "..."}] — drop the "body" prefix.
      detail = j.detail
        .map((d: { loc?: (string | number)[]; msg?: string }) =>
          `${(d.loc ?? []).slice(1).join(".") || "request"}: ${d.msg ?? "invalid"}`,
        )
        .join("; ");
    else if (typeof j?.error === "string") detail = j.error;
  } catch {
    /* not our JSON — a proxy error page, or an empty body */
  }
  if (detail) return new Error(detail); // the app's own wording, already user-facing
  // 524 is Cloudflare's own: the origin took longer than its FIXED 100-second
  // limit. 520-527 is the rest of that range; 502/503/504 are the generic ones.
  if (resp.status === 524)
    return new Error(
      "HTTP 524 — Cloudflare gave up waiting for QT after 100 seconds. That limit can't be raised. " +
        "Long backtests now run in the background and poll, so if you see this, something else took too long.",
    );
  if (resp.status === 502 || resp.status === 503 || resp.status === 504 || (resp.status >= 520 && resp.status <= 527))
    return new Error(
      `HTTP ${resp.status} — the server didn't answer in time. A long sweep can outlast a reverse ` +
        `proxy's timeout (nginx defaults to 60 seconds); raise it, or run it in smaller pieces.`,
    );
  if (resp.status === 401 || resp.status === 403)
    return new Error(`HTTP ${resp.status} — your sign-in expired. Reload the page to sign in again.`);
  // A proxy's error page is HTML. Pasting 200 characters of doctype and IE
  // conditional comments at the user tells them nothing — name it instead.
  const html = /^\s*<(!doctype|html)/i.test(body);
  const fallback = resp.statusText || (html ? "the server returned an error page, not a reply" : body.trim().slice(0, 200));
  return new Error(`HTTP ${resp.status}${fallback ? ` — ${fallback}` : ""}`);
}

async function handle<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw await failure(resp);
  if (resp.status === 204) return undefined as T; // no body to parse
  return (await resp.json()) as T;
}

export function getStatus(): Promise<StatusResponse> {
  return fetch("/api/status").then((r) => handle<StatusResponse>(r));
}

export function getSetupState(): Promise<{ alpaca_configured: boolean }> {
  return fetch("/api/setup/state").then((r) => handle(r));
}

export interface AuthState {
  configured: boolean;
  email: string | null;
  auth_disabled: boolean;
  redirect_uri: string;
}

export function getAuthState(): Promise<AuthState> {
  return fetch("/api/auth/state").then((r) => handle(r));
}

export function bootstrapAuth(clientId: string, clientSecret: string, ownerEmail: string) {
  return fetch("/api/auth/bootstrap", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: clientId, client_secret: clientSecret, owner_email: ownerEmail }),
  }).then((r) => handle<{ ok: boolean }>(r));
}

export function logout() {
  return fetch("/api/auth/logout", { method: "POST" });
}

export function getAllowlist(): Promise<{ emails: string[]; owner: string }> {
  return fetch("/api/auth/allowlist").then((r) => handle(r));
}

export function addAllowlist(email: string) {
  return fetch("/api/auth/allowlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  }).then((r) => handle<{ emails: string[] }>(r));
}

export function removeAllowlist(email: string) {
  return fetch(`/api/auth/allowlist/${encodeURIComponent(email)}`, { method: "DELETE" }).then((r) => handle(r));
}

export interface ScannerRow {
  symbol: string;
  asset_class: "stock" | "crypto";
  price: number;
  change_pct: number;
  dollar_volume: number;
}

export interface ScannerMeta {
  scanned: number;
  // How many cleared the filters, BEFORE the list was cut to top_n. Greater than
  // the rows shown means real movers are hidden below the cut.
  passed?: number;
  // {reason: count} — which of YOUR filters rejected how many symbols. The
  // answer to "why isn't my mover in here?" for any symbol, not just the
  // strongest one.
  rejected?: Record<string, number>;
  best_symbol: string | null;
  best_change_pct: number | null;
  best_price: number | null;
  best_dollar_volume: number | null;
}

export interface ScannerResult {
  stocks: ScannerRow[];
  crypto: ScannerRow[];
  errors: string[];
  market_open: boolean | null;
  stocks_meta: ScannerMeta | null;
  crypto_meta: ScannerMeta | null;
}

export interface ScannerClassFilters {
  enabled: boolean;
  min_price: number;
  max_price: number;
  min_change_pct: number;
  min_dollar_volume: number;
}

export interface ScannerConfig {
  top_n: number;
  exclude_symbols: string[];
  stocks: ScannerClassFilters;
  crypto: ScannerClassFilters;
}

export interface WatchlistRow {
  symbol: string;
  asset_class: "stock" | "crypto";
  price: number | null;
  change_pct: number | null;
  added_at: string;
  change_30d_pct: number | null;
  atr_pct: number | null;
  vs_sma200_pct: number | null;
  rsi: number | null;
  bars_available: number;
}

export interface HistoryResponse {
  symbol: string;
  asset_class: string;
  bars: { t: string; c: number; h?: number | null; l?: number | null; v?: number | null }[];
  stats: {
    change_30d_pct: number | null;
    atr_pct: number | null;
    vs_sma200_pct: number | null;
    bars_available: number;
  };
}

export function getHistory(symbol: string, assetClass: string, years = 10): Promise<HistoryResponse> {
  const params = new URLSearchParams({ symbol, asset_class: assetClass, years: String(years) });
  return fetch(`/api/market/history?${params}`).then((r) => handle(r));
}

export function getScanner(): Promise<ScannerResult> {
  return fetch("/api/scanner").then((r) => handle(r));
}

export function getScannerConfig(): Promise<ScannerConfig> {
  return fetch("/api/scanner/config").then((r) => handle(r));
}

export function saveScannerConfig(cfg: ScannerConfig): Promise<ScannerConfig> {
  return fetch("/api/scanner/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  }).then((r) => handle(r));
}

export function getWatchlist(): Promise<{ items: WatchlistRow[]; errors: string[] }> {
  return fetch("/api/watchlist").then((r) => handle(r));
}

export function addWatchlist(symbol: string, assetClass: "stock" | "crypto") {
  return fetch("/api/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, asset_class: assetClass }),
  }).then((r) => handle<{ ok: boolean; symbol: string }>(r));
}

export function removeWatchlist(symbol: string, assetClass: string) {
  return fetch(`/api/watchlist/${assetClass}/${encodeURIComponent(symbol)}`, { method: "DELETE" }).then((r) =>
    handle(r),
  );
}

export function getBars(symbol: string, assetClass: string): Promise<{ symbol: string; bars: { t: string; c: number }[] }> {
  const params = new URLSearchParams({ symbol, asset_class: assetClass });
  return fetch(`/api/market/bars?${params}`).then((r) => handle(r));
}

export interface AboutInfo {
  name: string;
  version: string;
  git_sha: string;
  build_date: string;
  license: string;
  repo_url: string;
}

export const getAbout = () => fetch("/api/about").then((r) => handle<AboutInfo>(r));
export const getChangelogMarkdown = () =>
  fetch("/api/about/changelog").then((r) => handle<{ markdown: string }>(r));
export const getRoadmapMarkdown = () =>
  fetch("/api/about/roadmap").then((r) => handle<{ markdown: string }>(r));

export function saveAlpacaKeys(keyId: string, keySecret: string) {
  return fetch("/api/setup/alpaca", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key_id: keyId, key_secret: keySecret }),
  }).then((r) => handle<{ ok: boolean; account_number: string; status: string }>(r));
}

export function liquidateAll(includeOrphans = false) {
  return fetch("/api/broker/liquidate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ include_orphans: includeOrphans }),
  }).then((r) =>
    handle<{
      ok: boolean;
      mode: "full" | "qt_only";
      positions_closed: number;
      trades_reconciled: number;
      orphans_cleared: string[];
      orphans_left: string[];
      errors: string[];
    }>(r),
  );
}

// --- Backtest fidelity (validation tool) -----------------------------------
// Two halves that mean different things. `decision` asks whether the replay
// reproduced the same trades — it should reach near-perfect agreement and then
// stay there, so it is a development instrument. `execution` measures how far
// real fills sat from simulated ones, which never becomes "solved": it depends
// on your broker, symbols and order sizes, and the number it yields belongs in
// the backtest's spread setting.
export interface FidelityMatch {
  symbol: string;
  day: string;
  live_entry: number | null;
  sim_entry: number | null;
  entry_delta_pct: number | null;
  live_exit: number | null;
  sim_exit: number | null;
  exit_delta_pct: number | null;
  live_exit_day: string | null;
  sim_exit_day: string | null;
  exit_day_matches: boolean;
  live_pnl: number | null;
  sim_pnl: number | null;
  live_exit_reason: string | null;
  sim_exit_reason: string | null;
  exit_reason_matches: boolean | null;
  exit_comparable: boolean;
}

export interface FidelityReport {
  strategy_name: string;
  mode: string;
  days: number;
  imported: boolean;
  timeframe?: string;
  bar_gaps?: { after: string; before: string; days: number }[];
  // False for paper: the broker simulates those fills, so the cost half would be
  // measuring a simulation rather than a market.
  execution_is_measurable: boolean;
  matched: FidelityMatch[];
  live_only: { symbol: string; day: string; entry_price: number | null; entry_reason: string }[];
  backtest_only: { symbol: string; day: string; sim_entry: number | null; sim_exit_reason: string }[];
  rails_blocked: { symbol: string; day: string; blocked_by: string | null }[];
  decision: {
    live_trades: number;
    backtest_trades: number;
    matched: number;
    missed_by_backtest: number;
    invented_by_backtest: number;
    match_rate_pct: number | null;
    same_exit_rule_pct: number | null;
    same_exit_day_pct: number | null;
    // Trades ended by a force-close, an account reset or a reconciliation. Their
    // ENTRY still counts; every exit-side number skips them, because the replay
    // was never given the chance to make that decision.
    manual_exits: number;
    enough_to_judge: boolean;
  };
  execution: {
    fills_compared: number;
    median_entry_delta_pct: number | null;
    median_exit_delta_pct: number | null;
    measured_cost_per_side_pct: number | null;
    assumed_spread_pct: number;
    assumed_fee_pct: number;
    suggested_spread_pct: number | null;
    backtest_pnl_optimism_usd: number | null;
    enough_to_judge: boolean;
  };
}

export const runFidelity = (body: {
  strategy_id: number;
  days: number;
  mode: string;
  imported_trades?: unknown[];
}) => fetch("/api/fidelity/compare", json(body)).then((r) => handle<FidelityReport>(r));
