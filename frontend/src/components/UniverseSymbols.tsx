import { useEffect, useState } from "react";
import {
  addBasketItem,
  addWatchlist,
  Basket,
  getWatchlist,
  removeBasketItem,
  removeWatchlist,
  StrategyRow,
} from "../api";
import InfoTip from "./InfoTip";
import SymbolPicker from "./SymbolPicker";
import { IconWarn } from "./icons";

// Diff two symbol lists into what was added and what was removed. SymbolPicker
// always emits the FULL new list, so a single add/remove is one element here.
function diff(prev: string[], next: string[]): { added: string[]; removed: string[] } {
  const P = new Set(prev);
  const N = new Set(next);
  return { added: next.filter((s) => !P.has(s)), removed: prev.filter((s) => !N.has(s)) };
}

/** Shows the concrete symbols a strategy will actually consider, the moment a
 *  universe is chosen — and lets you edit them right there:
 *   - custom   → the strategy's own list (owned by this strategy).
 *   - basket   → the basket's members (editing changes the SHARED basket).
 *   - watchlist/both → your watchlist for this asset class.
 *   - scanner  → dynamic (today's risers): nothing fixed to edit, just explained. */
export default function UniverseSymbols({
  universe,
  assetClass,
  basketId,
  baskets,
  onBasketsChanged,
  customSymbols,
  onCustomChange,
}: {
  universe: StrategyRow["universe"];
  assetClass: "stock" | "crypto";
  basketId: number | null;
  baskets: Basket[];
  onBasketsChanged: () => void;
  customSymbols: string[];
  onCustomChange: (symbols: string[]) => void;
}) {
  const noun = assetClass === "crypto" ? "crypto pairs" : "stocks";
  const placeholder = assetClass === "crypto" ? "Add: bitcoin or BTC/USD" : "Add: NVDA or a company name";

  if (universe === "custom") {
    return (
      <div className="field universe-symbols">
        <span className="field-cap">
          {noun[0].toUpperCase() + noun.slice(1)} in play ({customSymbols.length}) <InfoTip k="custom_symbols" />
        </span>
        <SymbolPicker assetClass={assetClass} value={customSymbols} onChange={onCustomChange} multi placeholder={placeholder} />
      </div>
    );
  }

  if (universe === "basket") {
    const basket = baskets.find((b) => b.id === basketId);
    if (!basket) return <p className="hint universe-symbols">Pick a basket above to see and edit the symbols in play.</p>;
    return (
      <BasketSymbols
        basket={basket}
        assetClass={assetClass}
        placeholder={placeholder}
        onChanged={onBasketsChanged}
      />
    );
  }

  if (universe === "watchlist" || universe === "both") {
    return <WatchlistSymbols assetClass={assetClass} placeholder={placeholder} noun={noun} both={universe === "both"} />;
  }

  // scanner — the universe is recomputed live each day, so there's no fixed list.
  return (
    <p className="hint universe-symbols">
      This universe is <strong>dynamic</strong> — each day the engine considers that day's <strong>top risers</strong>{" "}
      from the scanner, so there's no fixed list to edit here. To always include specific names, add them to your{" "}
      <strong>Watchlist</strong> and choose <em>Scanner + watchlist</em>.
    </p>
  );
}

function BasketSymbols({
  basket,
  assetClass,
  placeholder,
  onChanged,
}: {
  basket: Basket;
  assetClass: "stock" | "crypto";
  placeholder: string;
  onChanged: () => void;
}) {
  const members = basket.symbols.filter((m) => m.asset_class === assetClass).map((m) => m.symbol);
  // Optimistic local copy so a chip appears/disappears instantly; re-synced from
  // props once the parent refetch lands.
  const [local, setLocal] = useState(members);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setLocal(members), [members.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  function onChange(next: string[]) {
    const { added, removed } = diff(local, next);
    if (!added.length && !removed.length) return;
    setLocal(next);
    setBusy(true);
    setErr(null);
    Promise.all([
      ...added.map((s) => addBasketItem(basket.id, s, assetClass)),
      ...removed.map((s) => removeBasketItem(basket.id, s, assetClass)),
    ])
      .then(() => onChanged())
      .catch((e: Error) => {
        setErr(e.message);
        setLocal(members); // revert the optimistic change
      })
      .finally(() => setBusy(false));
  }

  return (
    <div className="field universe-symbols">
      <span className="field-cap">
        Symbols in “{basket.name}” ({local.length}){busy ? " · saving…" : ""}
      </span>
      <SymbolPicker assetClass={assetClass} value={local} onChange={onChange} multi placeholder={placeholder} />
      <p className="hint warn">
        <IconWarn className="icon-inline" /> These are the basket's members — editing here changes the “{basket.name}” basket <strong>everywhere</strong> it's
        used, not just this strategy. The strategy still trades only the top-ranked few (see “Rank by” / “Take top N”).
      </p>
      {err && <div className="error">{err}</div>}
    </div>
  );
}

function WatchlistSymbols({
  assetClass,
  placeholder,
  noun,
  both,
}: {
  assetClass: "stock" | "crypto";
  placeholder: string;
  noun: string;
  both: boolean;
}) {
  const [wl, setWl] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getWatchlist()
      .then((r) => {
        if (!cancelled) setWl(r.items.filter((i) => i.asset_class === assetClass).map((i) => i.symbol));
      })
      .catch(() => !cancelled && setWl([]));
    return () => {
      cancelled = true;
    };
  }, [assetClass]);

  function onChange(next: string[]) {
    if (wl === null) return;
    const { added, removed } = diff(wl, next);
    if (!added.length && !removed.length) return;
    const prev = wl;
    setWl(next);
    setBusy(true);
    setErr(null);
    Promise.all([
      ...added.map((s) => addWatchlist(s, assetClass)),
      ...removed.map((s) => removeWatchlist(s, assetClass)),
    ])
      .catch((e: Error) => {
        setErr(e.message);
        setWl(prev); // revert
      })
      .finally(() => setBusy(false));
  }

  return (
    <div className="field universe-symbols">
      <span className="field-cap">
        Your {noun} watchlist ({wl?.length ?? "…"}){busy ? " · saving…" : ""}
      </span>
      <SymbolPicker assetClass={assetClass} value={wl ?? []} onChange={onChange} multi placeholder={placeholder} />
      <p className="hint">
        These come from your <strong>Watchlist</strong> — editing here updates it everywhere.
        {both && " A “Scanner + watchlist” strategy ALSO trades the day's live scanner risers on top of these."}
      </p>
      {err && <div className="error">{err}</div>}
    </div>
  );
}
