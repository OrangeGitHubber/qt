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
    require_above_vwap: boolean;
    entry_window_start: string | null;
    entry_window_end: string | null;
  };
  exit: {
    trailing_stop_pct: number;
    stop_loss_pct: number;
    take_profit_pct: number;
    max_holding_hours: number;
    flatten_before_close: boolean;
    exit_below_vwap: boolean;
  };
}

export type RankBy = "momentum_today" | "return_30d" | "relative_strength";

export interface StrategyRow {
  id: number;
  name: string;
  enabled: boolean;
  asset_class: "stock" | "crypto";
  universe: "scanner" | "watchlist" | "both" | "basket";
  basket_id: number | null;
  rank_by: RankBy;
  top_n: number;
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
export const getJournal = (mode?: string) =>
  fetch(`/api/engine/journal${mode ? `?mode=${mode}` : ""}`).then((r) => handle<JournalRow[]>(r));
export const getScoreboard = () => fetch("/api/engine/scoreboard").then((r) => handle<Scoreboard>(r));

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
  days: number;
  timeframe: string;
  starting_cash: number;
  spread_pct: number;
}) => fetch("/api/backtest", json(body)).then((r) => handle<BacktestResult>(r));

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
  bars: { t: string; c: number }[];
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
