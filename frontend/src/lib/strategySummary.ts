/** The one-line ENTRY / EXIT / SIZING summaries on a strategy card.
 *
 *  Pulled out of Strategies.tsx and given its own file for one reason: the
 *  summary kept falling behind the rules. It is written once, next to the
 *  editor, and then never touched again when a new rule is added — so a rule
 *  can be SET, ACTING on real money, and invisible on the card that is meant to
 *  describe it. `exit_giveback_pct` shipped that way: 20% of every gain handed
 *  back by a rule the card never mentioned.
 *
 *  Two properties are worth stating, because the tests pin both:
 *
 *  1. ATR SUBSTITUTION IS SYMMETRIC. When `atr.trail_mult > 0` the fixed
 *     `trailing_stop_pct` is NOT in use — it is only the fallback for when ATR
 *     can't be computed, exactly as `atr.stop_mult` relates to `stop_loss_pct`.
 *     The card used to substitute for the stop and not for the trail, so it
 *     read "trail 6% · stop 1.5×ATR" for a strategy whose trail was 1.5×ATR,
 *     contradicting the editor's own help text one click away.
 *
 *  1b. A RULE CAN BE OVERRIDDEN WHOLESALE, not just substituted. `dca.interval_days`
 *     is the extreme case: the engine hands a DCA sleeve to its own entry path
 *     and never calls evaluate_entry, so every EntryRules field is dead and the
 *     row said "+3% day · above VWAP" about a strategy that buys on a calendar.
 *     The same branch skips the ATR-derived size, so it reaches sizing too.
 *
 *  2. EVERY RULE THAT IS ON IS NAMED. Rules that are off contribute nothing, so
 *     a plain strategy still reads in one line; a strategy with ten rules on is
 *     long because it IS long. The only deliberate omissions are the slippage
 *     knobs (entry_slippage_pct, exit_slippage_pct, exit_slippage_max_pct):
 *     they price the order once a decision is already made and never decide
 *     whether to trade, so they belong to execution, not to entry/exit.
 *     `test_strategy_summary.py` enforces that list — any OTHER field that can
 *     be switched on without changing the summary fails the suite.
 *
 *  SIZING is the same story a third time, and property 1 is why: `atr.risk_usd`
 *  displaces `sizing_usd` exactly as the ATR multiples displace the exit
 *  percentages, and the row still read "$100 / trade" for a strategy sized by a
 *  risk budget. See `sizingSummary` for the one wrinkle the exits don't have —
 *  ATR sizing needs TWO fields on, so one of them can be set and inert.
 */
import type { StrategyParams } from "../api";

/** The card's list separator, shared with the Trades and Lineage rows. */
const SEP = " · ";

/** Trim the float noise a JSON round-trip leaves behind (1.5 stays 1.5, 6.0
 *  becomes 6) without imposing a fixed number of decimals on either. */
function num(v: unknown): string {
  return String(Number(v));
}

/** Grouped dollars, no trailing ".00" — "$1,000", "$62.50". The locale is
 *  pinned rather than left to the browser because these are US-market dollars
 *  either way, and a summary that reads differently per machine can't be
 *  asserted on. */
function money(v: unknown): string {
  return `$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

/** What `entrySummary` reads. Like `SizingInputs`, wider than `params`: the
 *  regime gate is a column on the strategy, not an entry rule, and it decides
 *  entries all the same. `StrategyRow` satisfies it structurally. */
export interface EntryInputs {
  params: StrategyParams;
  asset_class?: "stock" | "crypto";
  ignore_regime?: boolean;
}

/** `every 7 days`, `every day` — DCA's cadence, which is the whole entry rule. */
function cadence(days: number): string {
  return days === 1 ? "every day" : `every ${num(days)} days`;
}

export function entrySummary(s: EntryInputs): string {
  const params = s.params;
  const e = params.entry;
  const parts: string[] = [];

  // A DCA sleeve never reaches evaluate_entry: _consider_entries hands it to
  // _consider_dca_entries and `continue`s, so the sleeve buys its fixed list on
  // a calendar and EVERY rule below is dead. Rendering them anyway was not one
  // number wrong, it was the whole row wrong — the strongest form of the bug
  // this file was written for. The exit side already knows DCA is different
  // ("no exit rules — held"); the entry side didn't.
  const dcaDays = Number(params.dca?.interval_days) || 0;
  if (dcaDays > 0) return `${cadence(dcaDays)} on schedule — entry rules don't apply`;

  // 0 = off, so "+0% day" would advertise a rule that isn't there.
  const minDay = Number(e.min_day_gain_pct) || 0;
  parts.push(minDay !== 0 ? `${minDay > 0 ? "+" : ""}${num(minDay)}% day` : "any day move");

  const maxDay = Number(e.max_day_gain_pct) || 0;
  if (maxDay > 0) parts.push(`max +${num(maxDay)}% day`);

  // A price band is one rule with two bounds — three phrasings, never two parts.
  const minPx = Number(e.min_price) || 0;
  const maxPx = Number(e.max_price) || 0;
  if (minPx > 0 && maxPx > 0) parts.push(`$${num(minPx)}–$${num(maxPx)}`);
  else if (minPx > 0) parts.push(`$${num(minPx)}+`);
  else if (maxPx > 0) parts.push(`under $${num(maxPx)}`);

  if (e.require_above_vwap) parts.push("above VWAP");
  if (e.require_macd_bullish) parts.push("MACD bullish");

  const rsiMin = Number(e.rsi_min) || 0;
  const rsiMax = Number(e.rsi_max) || 0;
  if (rsiMin > 0 && rsiMax > 0) parts.push(`RSI ${num(rsiMin)}–${num(rsiMax)}`);
  else if (rsiMin > 0) parts.push(`RSI above ${num(rsiMin)}`);
  else if (rsiMax > 0) parts.push(`RSI below ${num(rsiMax)}`);

  const cross = Number(e.rsi_cross_above) || 0;
  if (cross > 0) parts.push(`RSI crosses ${num(cross)}`);

  if (e.entry_window_start && e.entry_window_end) {
    parts.push(`${e.entry_window_start}–${e.entry_window_end} ET`);
  }

  // The regime gate lives on the strategy, not in EntryRules, but _consider_entries
  // checks it and `continue`s — it blocks BUYS, so the Entry row is its home. Only
  // the override is named: leaving it off is the default, and whether the filter
  // then actually runs also depends on the account-wide `regime_filter_enabled`
  // setting, which a card cannot see. Stock-only, matching the engine.
  if (s.ignore_regime && s.asset_class === "stock") {
    parts.push("buys even when SPY is below its 200-day");
  }

  return parts.join(SEP);
}

export function exitSummary(params: StrategyParams, topN?: number): string {
  const x = params.exit;
  const atr = params.atr;
  const parts: string[] = [];

  // The trail and the stop take the SAME shape: an ATR multiple replaces the
  // fixed percentage outright, and the percentage survives only as a fallback.
  const trailMult = Number(atr?.trail_mult) || 0;
  const trailPct = Number(x.trailing_stop_pct) || 0;
  if (trailMult > 0) parts.push(`trail ${num(trailMult)}×ATR`);
  else if (trailPct > 0) parts.push(`trail ${num(trailPct)}%`);

  const stopMult = Number(atr?.stop_mult) || 0;
  const stopPct = Number(x.stop_loss_pct) || 0;
  if (stopMult > 0) parts.push(`stop ${num(stopMult)}×ATR`);
  else if (stopPct > 0) parts.push(`stop ${num(stopPct)}%`);

  const target = Number(x.take_profit_pct) || 0;
  if (target > 0) parts.push(`target ${num(target)}%`);

  const giveback = Number(x.exit_giveback_pct) || 0;
  if (giveback > 0) parts.push(`give back ${num(giveback)}%`);

  const hold = Number(x.max_holding_hours) || 0;
  if (hold > 0) parts.push(`max hold ${num(hold)}h`);

  if (x.flatten_before_close) parts.push("flat by close");

  const rsiAbove = Number(x.exit_rsi_above) || 0;
  if (rsiAbove > 0) parts.push(`RSI above ${num(rsiAbove)}`);
  const rsiBelow = Number(x.exit_rsi_below) || 0;
  if (rsiBelow > 0) parts.push(`RSI below ${num(rsiBelow)}`);
  if (x.exit_rsi_falling) parts.push("RSI turns down");

  if (x.exit_below_vwap) parts.push("below VWAP");
  if (x.exit_on_macd_bearish) parts.push("MACD bearish");
  if (x.exit_on_regime_bear) parts.push("SPY below 200-day");
  if (x.rotate_on_rank_dropout) {
    parts.push(topN && topN > 0 ? `out of top ${topN}` : "out of the top N");
  }

  // A DCA sleeve is allowed to run with every exit off. Saying so is the point;
  // "trail 0% · stop 0%" said the opposite while looking like a configuration.
  return parts.length ? parts.join(SEP) : "no exit rules — held";
}

/** What `sizingSummary` reads. `StrategyRow` satisfies it structurally, so the
 *  card passes the row straight through; the shape is named separately because
 *  sizing is the one summary that needs fields from OUTSIDE `params`. */
export interface SizingInputs {
  params: StrategyParams;
  sizing_usd: number;
  sleeve_usd: number;
  max_positions: number;
  asset_class?: "stock" | "crypto";
  allow_concurrent_symbol?: boolean;
}

export function sizingSummary(s: SizingInputs): string {
  const atr = s.params.atr;
  const riskUsd = Number(atr?.risk_usd) || 0;
  const stopMult = Number(atr?.stop_mult) || 0;
  // The engine's own gate, `_atr_sizing_enabled`: the position size is derived
  // FROM the ATR stop distance (risk_usd ÷ stop_mult × ATR%), so a risk budget
  // with no stop multiple computes nothing and sizing falls back to the fixed
  // dollar amount. Two fields, one rule — and the only summary here where a
  // field can be set and still not be what's running.
  // A DCA sleeve is the third way ATR sizing can be configured and not running:
  // _consider_dca_entries calls open_trade WITHOUT the sizing_usd override that
  // the momentum path computes from atr_position_size, so a scheduled lot is
  // always the fixed dollar amount. Found while auditing the Entry row, in this
  // row — the same override that kills the entry rules also kills ATR sizing.
  const isDca = (Number(s.params.dca?.interval_days) || 0) > 0;
  const atrSizing = riskUsd > 0 && stopMult > 0 && !isDca;
  const parts: string[] = [];

  if (atrSizing) {
    // Same substitution as trail/stop above: the ATR form replaces the fixed
    // number outright, and sizing_usd survives only as the fallback for when
    // ATR can't be computed. The row used to show that fallback as the rule.
    parts.push(`risk ${money(riskUsd)} / trade (${num(stopMult)}×ATR stop)`);
  } else {
    parts.push(`${money(s.sizing_usd)} / trade`);
    // Half-configured ATR sizing is worth a line of its own. Silence here reads
    // as "the risk budget you typed is in force", which is the failure this
    // whole file exists to stop. The two reasons it can be inert are different
    // problems, so they don't share a sentence.
    if (riskUsd > 0) {
      parts.push(
        isDca
          ? `ATR risk ${money(riskUsd)} unused — DCA lots buy the fixed size`
          : `ATR risk ${money(riskUsd)} unused — needs an ATR stop`,
      );
    }
  }

  // With ATR sizing on the sleeve does double duty: atr_position_size caps every
  // computed size at it, so on a calm name the sleeve IS the per-trade size.
  parts.push(atrSizing ? `${money(s.sleeve_usd)} sleeve & per-trade cap` : `${money(s.sleeve_usd)} sleeve`);
  parts.push(`max ${num(s.max_positions)} positions`);

  // Not a dollar figure, but it decides how many shares a dollar figure buys:
  // off, a stock buy is rounded down to WHOLE shares, so $100 / trade buys none
  // of a $400 name and the order is skipped. Crypto is fractional either way, so
  // there only the order type changes.
  if (s.params.execution?.market_orders) {
    parts.push(s.asset_class === "crypto" ? "market orders" : "market orders, fractional shares");
  }

  // An exposure rail rather than a rule: it does gate an entry (the already-open
  // -symbol check), but what it CHANGES is how much of one name you can end up
  // owning, so it reads here and not on the Entry row. `ignore_regime`, which
  // sits beside it in the editor, went the other way — see entrySummary.
  if (s.allow_concurrent_symbol) parts.push("may double up on a symbol held elsewhere");

  return parts.join(SEP);
}
