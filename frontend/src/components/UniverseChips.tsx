import { Basket, StrategyRow } from "../api";

export interface ResolvedUniverse {
  symbols: string[];
  label: string;
  scannerReplay: boolean;
}

/** A strategy's universe, resolved READ-ONLY from its own config.
 *
 *  Both the backtest and the optimizer test a strategy — so they test the
 *  universe that strategy trades. Letting either one swap in a different symbol
 *  list produces a result about a strategy that doesn't exist: tune a basket
 *  rotator against three hand-picked winners and the numbers describe those
 *  three names, not the rotator you'd actually run.
 *
 *  Shared so the two pages can't drift into disagreeing about what a strategy's
 *  universe even is. Mirrors the backend's own resolution order.
 */
export function resolveUniverse(
  strat: StrategyRow | undefined,
  baskets: Basket[],
): ResolvedUniverse {
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

/** Read-only display of that universe, shown under the strategy dropdown. */
export default function UniverseChips({ uni }: { uni: { symbols: string[]; label: string } }) {
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
