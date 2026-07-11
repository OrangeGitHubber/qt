import { StatusResponse } from "../api";

function money(v: string | undefined, currency = "USD") {
  if (v === undefined) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(v));
}

function when(iso: string | undefined) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export default function Dashboard({ status, onRefresh }: { status: StatusResponse; onRefresh: () => void }) {
  const { broker, market, error } = status;
  return (
    <>
      {error && <div className="card error">{error}</div>}
      <div className="grid">
        <div className="card">
          <h3>Broker — Alpaca (paper)</h3>
          {broker ? (
            <dl>
              <dt>Account</dt>
              <dd>
                {broker.account_number} <span className={`pill ${broker.status === "ACTIVE" ? "ok" : "warn"}`}>{broker.status}</span>
              </dd>
              <dt>Equity</dt>
              <dd>{money(broker.equity, broker.currency)}</dd>
              <dt>Cash</dt>
              <dd>{money(broker.cash, broker.currency)}</dd>
              <dt>Buying power</dt>
              <dd>{money(broker.buying_power, broker.currency)}</dd>
            </dl>
          ) : (
            <p>No data.</p>
          )}
        </div>
        <div className="card">
          <h3>Market</h3>
          {market ? (
            <dl>
              <dt>US stock market</dt>
              <dd>
                <span className={`pill ${market.is_open ? "ok" : "muted"}`}>{market.is_open ? "OPEN" : "CLOSED"}</span>
              </dd>
              <dt>{market.is_open ? "Closes" : "Next open"}</dt>
              <dd>{when(market.is_open ? market.next_close : market.next_open)}</dd>
              <dt>Crypto market</dt>
              <dd>
                <span className="pill ok">OPEN 24/7</span>
              </dd>
            </dl>
          ) : (
            <p>No data.</p>
          )}
        </div>
        <div className="card">
          <h3>Engine</h3>
          <p>
            Trading engine arrives in Phase 2. Next up (Phase 1): the market scanner that finds the day's rising
            stocks and coins.
          </p>
          <button onClick={onRefresh}>Refresh now</button>
        </div>
      </div>
    </>
  );
}
