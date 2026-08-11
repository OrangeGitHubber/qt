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

/** What a strategy's MODE looks like everywhere it appears.
 *
 * Live money must be unmistakable at a glance, in every list, card and table.
 * The failure mode to design against is a live strategy sitting in the same
 * list as a paper one, distinguishable only by a small grey label — you scan
 * past it, edit the wrong row, and the mistake costs real money.
 *
 * So the three modes differ on FOUR channels at once — colour, wording, an icon,
 * and whether the row is emphasised — because any one of them can be lost. A
 * colour-blind reader, a greyscale screenshot pasted into Slack, a dark theme
 * that flattens the palette: each of those silently removes exactly one channel,
 * and none of them may leave live looking like paper.
 */
export type StrategyMode = "shadow" | "paper" | "live";

export interface ModeBadge {
  label: string;
  /** Tailwind-ish tone key the components map to a colour. */
  tone: "slate" | "sky" | "red";
  icon: string;
  /** Whether the whole ROW should be visually flagged, not just the badge. */
  emphasise: boolean;
  title: string;
}

export function modeBadge(mode: string | null | undefined): ModeBadge {
  const m = (mode ?? "").trim().toLowerCase();
  if (m === "live") {
    return {
      label: "LIVE",
      tone: "red",
      icon: "\u25CF", // filled circle — survives greyscale and copy/paste
      emphasise: true,
      title: "Trades REAL MONEY on your live Alpaca account.",
    };
  }
  if (m === "paper") {
    return {
      label: "Paper",
      tone: "sky",
      icon: "\u25CB",
      emphasise: false,
      title: "Places real orders on your Alpaca PAPER account. No real money.",
    };
  }
  // Anything unrecognised reads as shadow — the safest thing to imply, and the
  // engine agrees: an unknown mode resolves to off and trades nothing.
  return {
    label: "Shadow",
    tone: "slate",
    icon: "\u25CC",
    emphasise: false,
    title: "Journals decisions only. Places no orders at all.",
  };
}

/** The MASTER switch's label for one mode.
 *
 * Separate from `modeBadge` because the master has a fourth value — `off` — that
 * no strategy can hold, and because its unknown-value behaviour has to differ.
 *
 * A CHAIN OF TERNARIES ENDING IN `: "Paper"` IS WHAT THIS REPLACES, and it
 * shipped: adding `live` to ENGINE_MODES gave the dashboard a fourth button that
 * read "Paper" and set the mode to LIVE. Nothing about the button said what it
 * did. That is the same failure as the `*PAPER*` literals in the Slack alerts —
 * a hardcoded fallback asserting the safe answer for a case it has never seen.
 *
 * So an unrecognised mode renders its own RAW VALUE, upper-cased. It will look
 * odd, and odd is the point: a label nobody chose is a label nobody should
 * trust, and showing "PROD" is honest where showing "Paper" is a lie.
 */
export function masterModeLabel(mode: string): string {
  const known: Record<string, string> = {
    off: "Off",
    shadow: "Shadow",
    paper: "Paper",
    live: "LIVE",
  };
  return known[(mode ?? "").trim().toLowerCase()] ?? String(mode ?? "").toUpperCase();
}

/** Whether this master mode should be visually flagged as spending real money. */
export function masterModeIsLive(mode: string): boolean {
  return (mode ?? "").trim().toLowerCase() === "live";
}
