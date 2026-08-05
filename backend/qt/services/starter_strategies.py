"""Four shipped strategy TEMPLATES — one per trading style, clone-only.

WHY THESE EXIST. A long session spent debugging one underperforming strategy
kept turning up the same root cause: settings that belong to DIFFERENT styles
had been stacked on top of each other. A dip-buying entry (RSI crossing up out
of oversold) had been paired with three separate trend confirmations, and each
was measured as near-incompatible with it:

  * `require_macd_bullish` discarded 41% of the entry signals AND the survivors
    performed worse than the unfiltered set — MACD lags, so it only agreed once
    the bounce was over.
  * `require_above_vwap` / `min_day_gain_pct` select for "already moving up
    today", which is the opposite of buying something that has just stopped
    falling.
  * an "above the 200-day average" filter would have matched 16 of 156 signals,
    because a stock at RSI 30 is below its averages essentially by definition.

The result was not a safer strategy. It was one that took four trades in three
months. Each of these templates is internally coherent instead: every setting
serves the same thesis, and the notes say which thesis that is and what NOT to
add to it.

CLONE-ONLY. A template can never be enabled, edited or deleted (enforced in
qt.api.strategies). It is a fixed reference; cloning gives you an ordinary
strategy you own. Seeding is create-only — a template you already have is left
alone, so nothing is clobbered on reboot.

HONESTY ABOUT EVIDENCE. Only the dip buyer has been measured, and only weakly:
its entry beat a random entry into the same pool by ~10 points of hit rate, but
the signals clustered onto 99 market days, which puts the result nearer two
standard deviations than three. The other three are coherent designs, not
results. None of them is a recommendation to trade — they are starting points
whose reasoning is written down.
"""

import json
import logging

from sqlalchemy.orm import Session

from qt.models import Strategy

log = logging.getLogger("qt.strategies")

# A liquid, well-known large-cap starting list. Deliberately the same for the
# three stock templates so two clones can be compared without the symbol list
# being a second variable.
_CORE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AMD", "TSM", "ORCL",
    "DELL", "PLTR", "NFLX", "QQQ", "SPY",
]


DIP_BUYER_NOTES = """WHAT THIS IS
Buys weakness that has just turned: a stock sells off, RSI drops under 35, and
you buy the moment it crosses back up. Sells into the bounce.

THE ONE RULE THAT MATTERS
"Buy as RSI crosses up through 35". Not an RSI BAND — a band lets in a stock
sitting at 36 and rotting there for six weeks. A crossing only lets it in in the
window where selling pressure actually broke.

DO NOT ADD (all three were measured and all three fight this entry)
* Require MACD bullish — cut 41% of the signals and the survivors did WORSE.
  MACD lags, so it only confirms once the bounce is finished.
* Require above VWAP, or a min day gain above 0 — both mean "already up today".
  A stock crossing up out of oversold is frequently still red on the day.
* An "above the 200-day average" filter — 155 of 156 signals happened BELOW the
  50-day average. That is what being oversold means.

WHY THERE IS A TAKE-PROFIT
Without one the trailing stop gives most of the move back: measured trades
peaked at +17% and +20% and were exited at +5% and +4%. 12% banks it. The cost
is that 12% is now your ceiling on every trade — you will not ride a stock that
doubles. For this style that is usually the right trade.

WHAT IT CANNOT DO
Own a stock that never dips. A name that grinds from $230 to $474 keeps its RSI
between 42 and 69 and is invisible to this strategy. If you want those, run the
Trend Follower template as a second sleeve — do not bolt trend rules onto this.

EVIDENCE
The entry beat a random entry into the same 19 stocks by ~10 points of hit rate
over 2024-2026 (66% vs 55% positive after 10 sessions). But the 218 signals fell
on only 99 days — 15 stocks crossed together on one day — so the honest reading
is ~2 standard deviations, not 3. Suggestive. Not proven. Paper-trade it."""


TREND_FOLLOWER_NOTES = """WHAT THIS IS
The opposite thesis to the Dip Buyer: buys strength that is BUILDING and holds
while it keeps building. This is the style that owns the stock that doubles.

THE RANKING IS THE POINT
"MACD momentum building" (macd_slope) ranks by how fast the MACD gap is GROWING,
not how big it is. That distinction matters more than it sounds:
* Ranking by RSI puts the MOST OVERBOUGHT names at the top of your list, by
  construction. It is the single worst ranking for a momentum entry and it is
  what a previous strategy was accidentally using.
* Ranking by MACD STRENGTH (the level) is nearly as bad — the MACD gap is widest
  LATE in a move, so it also ranks what has already run.
The slope turns positive as momentum builds, so it favours a name that is
accelerating over one that is high and flattening.

WHY MAX DAY GAIN IS SET
Momentum entries chase. The 10% ceiling refuses the parabolic day where you are
buying someone else's exit.

WHY NO TAKE-PROFIT
A ceiling defeats the entire thesis. The wide ATR trail (3.5x) is what gets you
out — it is deliberately loose so an ordinary pullback does not end the trade.
A 3% fixed trail is INSIDE one average day for a volatile stock and will sell
you out of every winner; that was measured.

DO NOT ADD
* An RSI band or an RSI crossing — a trending stock rarely gets oversold, so
  these will simply stop it ever buying.
* A tight trailing stop. If you want tight stops, you want the Dip Buyer.

EVIDENCE
None. This is a coherent design, not a measured result. Backtest it against the
buy-and-hold line before believing it."""


SCANNER_INTRADAY_NOTES = """WHAT THIS IS
Rides today's biggest movers and is flat by the close. The only template that
does not hold overnight.

SWING MODE IS OFF, AND THAT IS THE CRITICAL SETTING
With swing mode ON, the soft exits (take-profit, VWAP, MACD, RSI) are deferred
until the DAY AFTER entry — sensible for a multi-day strategy, fatal for one
that closes the same day. Leave it off.

THE TIME WINDOW
10:00-15:30 ET. The first half hour is the widest spreads and the most reversals
of the day; the last half hour is where you want to be closing, not opening.
"Flatten before close" then guarantees no overnight gap risk.

WHY THE STOPS ARE TIGHT
Intraday moves are smaller, so a 10% trail would never trigger before the close
and the flatten would be doing all the work. 3% trail / 4% stop are sized to the
horizon.

THE REAL COST HERE
This is the ONLY style where slippage genuinely bites — many small trades, each
paying the spread twice. `max_trades_per_day` in Settings is the account-wide
brake and matters more for this style than any other. Keep the position size
small until you have seen it work.

BACKTESTING CAVEAT
This style needs intraday bars, so its backtests are slower and cover shorter
windows than the daily-bar styles. A 90-day test is a realistic maximum.

EVIDENCE
None. Coherent design, not a measured result."""


DCA_BASELINE_NOTES = """WHAT THIS IS
Buy the list every N days and hold. No timing, no signals. This is the baseline
every other strategy has to beat, and it is here so you can actually run it
rather than assume you would have beaten it.

IT WILL BEAT YOUR CLEVER STRATEGIES MORE OFTEN THAN YOU EXPECT
That is the point of running it. In one measured three-month window the
equal-weight buy-and-hold line returned +13.9% while an actively-managed
strategy over the same 19 symbols returned +1.6%. 40% of that came from a single
stock that a dip-buying entry could never have owned.

WHY IT HAS NO STOP-LOSS
A DCA sleeve is the only strategy the app allows to run with all exits off — the
thesis is that you hold through drawdowns, and a stop would sell exactly the
weakness you are supposed to be buying. If that makes you uncomfortable, that
discomfort is worth sitting with before you switch on anything riskier.

THE ONE OPTIONAL EXIT
"Sell to cash when the market turns down" (SPY below its 200-day average) is off
by default here. Turning it on makes this a trend-aware hold rather than a true
baseline — a reasonable thing to want, but then it is no longer the yardstick.

WHAT TO CHANGE
The symbol list, and the interval. Nothing else. Every filter you add makes it
less of a baseline and more of a strategy you have to justify."""


# name -> the full row. `params` mirrors StrategyParams; anything absent falls
# back to that model's defaults on read.
STARTER_STRATEGIES: list[dict] = [
    {
        "name": "Template · Dip buyer (mean reversion)",
        "notes": DIP_BUYER_NOTES,
        "asset_class": "stock",
        "universe": "custom",
        "symbols": _CORE,
        "rank_by": "momentum_today",
        "rank_enabled": False,
        "sizing_usd": 500.0,
        "sleeve_usd": 5000.0,
        "max_positions": 8,
        "swing_mode": True,
        "params": {
            "entry": {
                "min_day_gain_pct": 0,       # OFF — it buys red days on purpose
                "rsi_cross_above": 35,       # the entry
                "require_above_vwap": False,
                "require_macd_bullish": False,
            },
            "exit": {
                "stop_loss_pct": 4.0,        # fallback; the ATR stop overrides
                "trailing_stop_pct": 10.0,   # fallback; the ATR trail overrides
                "take_profit_pct": 12.0,
            },
            "atr": {"period": 14, "stop_mult": 2.5, "trail_mult": 2.5},
        },
    },
    {
        "name": "Template · Trend follower",
        "notes": TREND_FOLLOWER_NOTES,
        "asset_class": "stock",
        "universe": "custom",
        "symbols": _CORE,
        "rank_by": "macd_slope",             # momentum BUILDING, not already-run
        "rank_enabled": True,
        "top_n": 8,
        "sizing_usd": 500.0,
        "sleeve_usd": 5000.0,
        "max_positions": 8,
        "swing_mode": True,
        "params": {
            "entry": {
                "min_day_gain_pct": 1.0,
                "max_day_gain_pct": 10.0,    # refuse the parabolic day
                "require_above_vwap": True,
                "require_macd_bullish": True,
            },
            "exit": {
                "stop_loss_pct": 6.0,
                "trailing_stop_pct": 12.0,
                "take_profit_pct": 0,        # OFF — a ceiling defeats the thesis
            },
            "atr": {"period": 14, "stop_mult": 3.0, "trail_mult": 3.5},
        },
    },
    {
        "name": "Template · Intraday scanner rider",
        "notes": SCANNER_INTRADAY_NOTES,
        "asset_class": "stock",
        "universe": "scanner",               # the day's risers
        "symbols": [],
        "rank_by": "momentum_today",
        "rank_enabled": False,
        "sizing_usd": 250.0,                 # small: this style churns
        "sleeve_usd": 2000.0,
        "max_positions": 4,
        "swing_mode": False,                 # CRITICAL — see the notes
        "params": {
            "entry": {
                "min_day_gain_pct": 3.0,
                "max_day_gain_pct": 15.0,
                "require_above_vwap": True,
                "entry_window_start": "10:00",
                "entry_window_end": "15:30",
            },
            "exit": {
                "stop_loss_pct": 4.0,
                "trailing_stop_pct": 3.0,
                "take_profit_pct": 5.0,
                "max_holding_hours": 6,
                "flatten_before_close": True,
            },
        },
    },
    {
        "name": "Template · Long-term DCA baseline",
        "notes": DCA_BASELINE_NOTES,
        "asset_class": "stock",
        "universe": "custom",
        "symbols": _CORE,
        "rank_by": "momentum_today",
        "rank_enabled": False,
        "sizing_usd": 250.0,
        "sleeve_usd": 10000.0,
        "max_positions": 20,
        "swing_mode": True,
        "params": {
            "entry": {"min_day_gain_pct": 0},   # no timing at all
            "exit": {
                "stop_loss_pct": 0,             # allowed ONLY for a DCA sleeve
                "trailing_stop_pct": 0,
                "take_profit_pct": 0,
            },
            "dca": {"interval_days": 14},
        },
    },
]


def seed_starter_strategies(session: Session) -> int:
    """Create the shipped templates if absent. Returns how many were created.

    CREATE-ONLY, unlike the starter baskets, which refresh their membership on
    every boot. A template is inert — it cannot be enabled, edited or deleted —
    so there is nothing for a refresh to converge, and rewriting rows on every
    restart would only risk clobbering a database the user is mid-way through
    reading. A template the user has somehow removed stays removed.

    Matched on (name, template=True), so a user's own strategy that happens to
    share a name is never touched or mistaken for a shipped one.
    """
    created = 0
    for spec in STARTER_STRATEGIES:
        existing = (
            session.query(Strategy)
            .filter(Strategy.name == spec["name"], Strategy.template.is_(True))
            .one_or_none()
        )
        if existing is not None:
            continue
        session.add(
            Strategy(
                name=spec["name"],
                notes=spec["notes"],
                template=True,
                enabled=False,          # belt and braces; the API refuses anyway
                asset_class=spec["asset_class"],
                universe=spec["universe"],
                symbols=json.dumps(spec.get("symbols") or []),
                rank_by=spec["rank_by"],
                rank_enabled=spec.get("rank_enabled", False),
                top_n=spec.get("top_n", 10),
                preset="template",
                params=json.dumps(spec["params"]),
                sizing_usd=spec["sizing_usd"],
                sleeve_usd=spec["sleeve_usd"],
                max_positions=spec["max_positions"],
                swing_mode=spec["swing_mode"],
            )
        )
        created += 1
    if created:
        log.info("seeded %d starter strategy templates", created)
    return created
