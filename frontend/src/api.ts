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
  broker: BrokerInfo | null;
  market: MarketInfo | null;
  error: string | null;
}

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

export interface ScannerRow {
  symbol: string;
  asset_class: "stock" | "crypto";
  price: number;
  change_pct: number;
  dollar_volume: number;
}

export interface ScannerResult {
  stocks: ScannerRow[];
  crypto: ScannerRow[];
  errors: string[];
}

export interface ScannerConfig {
  stocks_enabled: boolean;
  crypto_enabled: boolean;
  top_n: number;
  min_price: number;
  max_price: number;
  min_change_pct: number;
  min_dollar_volume: number;
  exclude_symbols: string[];
}

export interface WatchlistRow {
  symbol: string;
  asset_class: "stock" | "crypto";
  price: number | null;
  change_pct: number | null;
  added_at: string;
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

export function saveAlpacaKeys(keyId: string, keySecret: string) {
  return fetch("/api/setup/alpaca", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key_id: keyId, key_secret: keySecret }),
  }).then((r) => handle<{ ok: boolean; account_number: string; status: string }>(r));
}
