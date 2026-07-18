import { useCallback, useEffect, useState } from "react";
import {
  addBasketItem,
  Basket,
  createBasket,
  deleteBasket,
  getBaskets,
  removeBasketItem,
  renameBasket,
} from "../api";
import InfoTip from "../components/InfoTip";
import SymbolPicker from "../components/SymbolPicker";

function BasketCard({ basket, onChange }: { basket: Basket; onChange: () => void }) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(basket.name);
  const [assetClass, setAssetClass] = useState<"stock" | "crypto">("stock");
  const [error, setError] = useState<string | null>(null);

  async function guard(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      onChange();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="card">
      <h3>
        {renaming ? (
          <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        ) : (
          basket.name
        )}{" "}
        {basket.builtin && <span className="pill muted">built-in</span>}
        <span className="pill">{basket.count} symbols</span>
      </h3>

      <div className="chips">
        {basket.symbols.map((s) => (
          <span className={`chip ${s.in_directory ? "" : "chip-warn"}`} key={`${s.asset_class}:${s.symbol}`}>
            {s.symbol}
            {!s.in_directory && <span title="Not in Alpaca's tradable list right now"> ⚠</span>}
            <button
              type="button"
              aria-label={`Remove ${s.symbol}`}
              onClick={() => guard(() => removeBasketItem(basket.id, s.symbol, s.asset_class))}
            >
              ×
            </button>
          </span>
        ))}
        {basket.count === 0 && <span className="hint">No symbols yet — add some below.</span>}
      </div>

      <div className="filter-grid">
        <label>
          Add to this basket
          <select value={assetClass} onChange={(e) => setAssetClass(e.target.value as "stock" | "crypto")}>
            <option value="stock">Stocks</option>
            <option value="crypto">Crypto</option>
          </select>
        </label>
        <div className="field">
          &nbsp;
          <SymbolPicker
            assetClass={assetClass}
            value={[]}
            onChange={(syms) => syms[0] && guard(() => addBasketItem(basket.id, syms[0], assetClass))}
          />
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      <div className="toolbar">
        {renaming ? (
          <>
            <button
              className="small"
              onClick={() =>
                guard(() => renameBasket(basket.id, name.trim())).then(() => setRenaming(false))
              }
            >
              Save name
            </button>
            <button className="small" onClick={() => {
              setName(basket.name);
              setRenaming(false);
            }}>
              Cancel
            </button>
          </>
        ) : (
          <button className="small" onClick={() => setRenaming(true)}>
            Rename
          </button>
        )}
        <button className="small danger" onClick={() => guard(() => deleteBasket(basket.id))}>
          Delete
        </button>
      </div>
    </div>
  );
}

export default function Baskets() {
  const [baskets, setBaskets] = useState<Basket[] | null>(null);
  const [newName, setNewName] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getBaskets()
      .then(setBaskets)
      .catch((e: Error) => setNote(e.message));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setNote(null);
    try {
      await createBasket(newName.trim());
      setNewName("");
      refresh();
    } catch (err) {
      setNote((err as Error).message);
    }
  }

  return (
    <>
      <div className="toolbar">
        <h2>
          Baskets <InfoTip k="basket" />
        </h2>
      </div>
      <div className="card">
        <p className="hint">
          A basket is a <strong>curated list of symbols</strong> grouped by theme (Defense, Banking, …) — not an
          authoritative sector database. Alpaca ships no sector or industry data on this plan, so these lists are
          hand-picked and yours to edit; they drift over time as companies change. Use a basket as a strategy's
          universe (with top-N ranking) or load one into a backtest.
        </p>
        <form className="toolbar" onSubmit={create}>
          <input
            value={newName}
            placeholder="New basket name (e.g. My Watchlist Theme)"
            onChange={(e) => setNewName(e.target.value)}
          />
          <button className="small">+ Create basket</button>
        </form>
      </div>
      {note && (
        <div className="card note" onClick={() => setNote(null)}>
          {note}
        </div>
      )}
      {!baskets ? (
        <div className="card">Loading…</div>
      ) : (
        <div className="grid">
          {baskets.map((b) => (
            <BasketCard key={b.id} basket={b} onChange={refresh} />
          ))}
        </div>
      )}
    </>
  );
}
