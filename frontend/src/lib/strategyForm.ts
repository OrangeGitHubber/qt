/** Decisions the strategy form makes that depend on the ASSET CLASS.
 *
 * Three of them were written inline in JSX and each got the asset class wrong in
 * the same way — by not asking. A 24/7 book was handed a US-session default, two
 * warnings named a control crypto never renders, and a fee note branched on a
 * dollar total instead of on what is actually charged. They live here so a test
 * can execute them rather than grep for them, the same reason and the same
 * mechanism as `strategySummary.ts`.
 */

export type AssetClass = "stock" | "crypto";

/** The window to seed when "limit entries to a time window" is switched on.
 *
 * 09:30–15:30 is the US SESSION. Seeding it on a 24/7 book silently discards
 * about three quarters of the day, and `setAssetClass` clears the pair when you
 * switch to crypto for exactly that reason — so re-seeding it from the checkbox
 * without an asset-class branch quietly undid that. Crypto starts at the whole
 * day: switching the toggle on restricts nothing until you narrow it yourself.
 */
export function defaultEntryWindow(assetClass: AssetClass): { start: string; end: string } {
  return assetClass === "crypto"
    ? { start: "00:00", end: "23:59" }
    : { start: "09:30", end: "15:30" };
}

export interface WarningInputs {
  assetClass: AssetClass;
  swingMode: boolean;
  stopPct: number;
  requireAboveVwap: boolean;
}

/** The two warnings that are only meaningful for stocks.
 *
 * Both name the "Trading style" control as the remedy, and that control is
 * HIDDEN for crypto — so on a crypto strategy they told you to go and change
 * something you could not see. `swingMode` is inert for crypto anyway:
 * `evaluate_exit`'s same-day deferral is disabled outright by `is_crypto`, and
 * flatten-before-close is stock-only.
 */
export function stockOnlyWarnings(i: WarningInputs): {
  tightSwingStop: boolean;
  vwapOnSwing: boolean;
} {
  const isStock = i.assetClass !== "crypto";
  return {
    // A tight stop held overnight is hit by ordinary daily noise.
    tightSwingStop: isStock && i.swingMode && i.stopPct > 0 && i.stopPct < 3,
    // VWAP resets each session, so requiring it forces intraday-bar backtests
    // and does not belong in a daily/swing rotation.
    vwapOnSwing: isStock && i.swingMode && i.requireAboveVwap,
  };
}

export type FeeNote = "charged" | "crypto-untraded" | "stock-free";

/** Which fee note a finished backtest should show.
 *
 * Branching on `feesPaid === 0` alone was wrong: that is also true of a CRYPTO
 * run that simply took no trades, and the note then said Alpaca charges no
 * commission — the opposite of the truth, about the single largest cost in a
 * crypto strategy. 0.25% a side is roughly 98% of BTC's entire round trip.
 */
export function feeNote(assetClass: AssetClass, feesPaid: number): FeeNote {
  if (feesPaid > 0) return "charged";
  return assetClass === "crypto" ? "crypto-untraded" : "stock-free";
}
