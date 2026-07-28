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
}

export type RankBy = "momentum_today" | "return_30d" | "relative_strength" | "rs_vs_spy";

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
  config_version_id: number | null;
}

export interface Scoreboard {
  days: string[];
  bot: (number | null)[];
  spy: (number | null)[];
  btc: (number | null)[];
  verdict: string | null;
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
export const getJournal = (mode?: string, status?: string, assetClass?: string) => {
  const qs = new URLSearchParams();
  if (mode) qs.set("mode", mode);
  if (status) qs.set("status", status);
  if (assetClass) qs.set("asset_class", assetClass);
  const q = qs.toString();
  return fetch(`/api/engine/journal${q ? `?${q}` : ""}`).then((r) => handle<JournalRow[]>(r));
};
export const getScoreboard = () => fetch("/api/engine/scoreboard").then((r) => handle<Scoreboard>(r));

export interface StrategyPnl {
  mode: string;
  realized_total: number;
  strategies: {
    strategy_id: number;
    name: string;
    realized_pnl: number;
    trades: number;
    wins: number;
    win_rate: number | null;
    open_positions: number;
  }[];
}
export interface StrategyPnlDaily {
  mode: string;
  days: string[];
  strategies: { strategy_id: number; name: string; values: number[]; total: number }[];
}
export const getStrategyPnl = () => fetch("/api/engine/strategy-pnl").then((r) => handle<StrategyPnl>(r));
export const getStrategyPnlDaily = (days = 30) =>
  fetch(`/api/engine/strategy-pnl-daily?days=${days}`).then((r) => handle<StrategyPnlDaily>(r));

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
  replay_top_n?: number;
  universe_size?: number;
  days_replayed?: number;
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
  equity_days: string[];
  equity: number[];
  benchmark: (number | null)[] | null;
  benchmark_symbol: string | null;
  trade_list: BacktestTrade[];
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
}) => fetch("/api/backtest", json(body)).then((r) => handle<BacktestResult>(r));

// Portfolio (multi-strategy) backtest: N strategies over the SAME period sharing
// ONE account + the global rails, with a per-strategy contribution breakdown.
export interface PortfolioContribution {
  strategy_id: number;
  strategy_name: string;
  realized_pnl: number;
  trades: number;
  wins: number;
  win_rate: number | null;
  share_pct: number | null; // sign-preserving share of the portfolio realized total
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
}

export const runPortfolioBacktest = (body: {
  strategy_ids: number[];
  days: number;
  timeframe: string;
  starting_cash: number;
  spread_pct: number;
}) => fetch("/api/backtest/portfolio", json(body)).then((r) => handle<PortfolioBacktestResult>(r));

// ---- Strategy optimizer (parameter search) ----
// Searches a momentum strategy's parameter space with the SAME backtester,
// splitting history into in-sample (searched) and out-of-sample (validation).
// Only the out-of-sample number is treated as real. NOT "AI" — a parameter search.

export interface OptimizerMetrics {
  net_pnl_pct: number | null;
  trades: number | null;
  win_rate: number | null;
  return_on_deployed_pct: number | null;
  max_drawdown_pct: number | null;
}

export interface OptimizerComboParams {
  min_day_gain_pct: number;
  trailing_stop_pct: number;
  stop_loss_pct: number;
  take_profit_pct: number;
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
  iterations: number;
  in_sample_window: OptimizerWindow;
  out_of_sample_window: OptimizerWindow;
  results: OptimizerResultRow[];
  best: OptimizerResultRow | null;
  best_draft_params: StrategyParams;
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
  timeframe?: string;
  days?: number;
  scanner_replay?: boolean;
  replay_intraday?: boolean;
  replay_top_n?: number;
  universe_size?: number;
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
}) => fetch("/api/optimizer", json(body)).then((r) => handle<{ ok: boolean; started: boolean; symbols: string[]; iterations: number; scanner_replay: boolean }>(r));

export const getOptimizerStatus = () =>
  fetch("/api/optimizer/status").then((r) => handle<OptimizerStatus>(r));

async function handle<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return resp.json() as Promise<T>;
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
