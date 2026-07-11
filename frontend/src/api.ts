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

export function saveAlpacaKeys(keyId: string, keySecret: string) {
  return fetch("/api/setup/alpaca", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key_id: keyId, key_secret: keySecret }),
  }).then((r) => handle<{ ok: boolean; account_number: string; status: string }>(r));
}
