"""Audit (2026-08-03) of the PORTFOLIO backtester — the multi-strategy replay.

Two faults, both of which put a better number on the screen than the account
could ever have earned:

  1. A FULL DAY OF LOOK-AHEAD on an all-crypto book. run_portfolio_backtest
     bucketed its bars with `_day_fn(market)` (UTC for crypto) but called the
     MACD/RSI/ATR annotators without `day_of`, so they fell back to `_et_day`. A
     crypto daily bar opens at 00:00Z, which ET files under the PREVIOUS day, so
     _daily_frontier's "daily bars strictly before day D" quietly admitted day
     D's own close — an intraday bar at 02:00Z reading an indicator derived from
     a price 22 hours in its future.

  2. NO FEES AT ALL. The portfolio replay had no fee parameter, while both
     single-strategy paths charge crypto 0.25% a side (qt.api.backtest
     .DEFAULT_FEE_PCT). Half a percent a round trip, unmodelled.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.services import stats
from qt.services.backtest import run_portfolio_backtest
from qt.services.engine import RISK_DEFAULTS

RISK = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)

D_MINUS_1 = datetime(2026, 5, 4, tzinfo=timezone.utc)
D = datetime(2026, 5, 5, tzinfo=timezone.utc)


def _daily(closes: list[float]) -> list[dict]:
    """Crypto daily bars, newest last, stamped 00:00Z — Alpaca's crypto daily
    convention, and the stamp that lands on UTC day D but ET day D−1."""
    return [
        {
            "t": (D - timedelta(days=len(closes) - 1 - i)).strftime("%Y-%m-%dT00:00:00Z"),
            "c": c, "h": c, "l": c, "v": 100, "vw": c,
        }
        for i, c in enumerate(closes)
    ]


# Twenty straight losing days, then one huge up day. Where that up day sits is
# the whole experiment: on D it is invisible to any bar of D; on D−1 it is not.
JUMP_ON_D = [140.0 - 2 * i for i in range(20)] + [130.0]
JUMP_ON_D_MINUS_1 = [140.0 - 2 * i for i in range(19)] + [130.0, 131.0]


def _intraday() -> list[dict]:
    """Hourly bars: flat through D−1, rising through D. The rise gives every bar
    of D a positive rolling-24h day-gain, so the day-gain gate is never what
    decides these tests."""
    out = []
    for h in range(24):
        t = D_MINUS_1 + timedelta(hours=h)
        out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": 100.0, "h": 100.0,
                    "l": 100.0, "v": 10, "vw": 100.0})
    for h in range(24):
        c = 100.0 + h * 0.5
        t = D + timedelta(hours=h)
        out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": c, "h": c, "l": c,
                    "v": 10, "vw": c})
    return out


def _crypto_strategy(sid: int = 1, **over) -> dict:
    base = {
        "id": sid,
        "name": "Crypto RSI",
        "asset_class": "crypto",
        "swing_mode": True,
        "sizing_usd": 1000.0,
        "sleeve_usd": 5000.0,
        "max_positions": 3,
        "params": {
            "entry": {"min_day_gain_pct": 1.0, "rsi_min": 5.0, "require_above_vwap": False},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False},
        },
    }
    base.update(over)
    return base


def _run(daily_closes: list[float], **kw) -> dict:
    return run_portfolio_backtest(
        [_crypto_strategy()], {1: {"AAA/USD": _intraday()}}, RISK,
        starting_cash=5000, spread_pct=0, market="crypto",
        daily_bars_by_strategy={1: {"AAA/USD": _daily(daily_closes)}},
        **kw,
    )


def _entries(r: dict) -> int:
    return r["trades"] + len(r["open_positions"])


def test_the_fixture_really_straddles_the_day_boundary():
    """The guard on the two tests below: they mean nothing unless day D's own
    daily close is what flips the RSI rule from fail to pass. Asserted on
    stats.rsi_from_closes directly, so a fixture that drifted into 'passes either
    way' fails HERE rather than silently turning the look-ahead test vacuous."""
    assert stats.rsi_from_closes(JUMP_ON_D[:-1]) == 0.0   # through D−1: below rsi_min 5
    assert stats.rsi_from_closes(JUMP_ON_D) > 5.0          # including D: above it
    # And the positive control: with the jump a day earlier, D−1's own history
    # already clears the bar, so an entry on D needs no look-ahead at all.
    assert stats.rsi_from_closes(JUMP_ON_D_MINUS_1[:-1]) > 5.0


def test_a_crypto_portfolio_cannot_read_todays_daily_close():
    """THE LOOK-AHEAD. Day D's RSI is 0 on every bar of D, so nothing may enter —
    and it must stay that way whatever the daily bar for D says."""
    result = _run(JUMP_ON_D)
    assert _entries(result) == 0, (
        "an entry on day D can only have come from day D's own daily close — "
        f"{[p['entry_reason'] for p in result['open_positions']]}"
    )


def test_the_same_book_still_trades_on_history_it_is_allowed_to_see():
    """The control. Move the up day to D−1 — genuinely completed history — and the
    identical intraday stream now trades. Without this the test above would pass
    just as well against a replay that had stopped trading altogether."""
    result = _run(JUMP_ON_D_MINUS_1)
    assert _entries(result) == 1
    entered = (result["open_positions"] + result["trade_list"])[0]
    assert "RSI" in entered["entry_reason"]


# ─────────────────────────── fees ───────────────────────────

FEES = {"stock": 0.0, "crypto": 0.25}


def test_an_all_crypto_portfolio_is_charged_a_commission():
    """0.25% a side on Alpaca crypto. Charged, reported in dollars, and reported
    at the rate that was applied."""
    charged = _run(JUMP_ON_D_MINUS_1, fee_pct_by_class=FEES)
    assert charged["fees_paid"] > 0
    assert charged["fee_pct_per_side"] == 0.25
    assert charged["fee_pct_per_side_by_class"] == {"crypto": 0.25}


def test_the_fee_comes_out_of_the_result_and_not_just_the_header():
    """A rate nobody subtracts is decoration. The charged book must end up worth
    strictly less than the free one over the identical bars."""
    free = _run(JUMP_ON_D_MINUS_1)
    charged = _run(JUMP_ON_D_MINUS_1, fee_pct_by_class=FEES)
    assert free["fees_paid"] == 0.0
    assert charged["final_equity"] < free["final_equity"]


def test_the_entry_fee_has_to_clear_the_cash_check():
    """The buy costs notional PLUS the fee, so a book that can only just afford a
    position must not be able to afford it once the commission is added — else
    the sim spends money the account never had."""
    sizing = 1000.0
    just_enough = sizing * 1.001  # room for the notional, not for 0.25% on top
    strat = _crypto_strategy(sizing_usd=sizing)
    common = dict(
        starting_cash=just_enough, spread_pct=0, market="crypto",
        daily_bars_by_strategy={1: {"AAA/USD": _daily(JUMP_ON_D_MINUS_1)}},
    )
    free = run_portfolio_backtest([strat], {1: {"AAA/USD": _intraday()}}, RISK, **common)
    charged = run_portfolio_backtest(
        [strat], {1: {"AAA/USD": _intraday()}}, RISK, fee_pct_by_class=FEES, **common
    )
    assert _entries(free) == 1
    assert _entries(charged) == 0


def test_a_mixed_book_charges_each_sleeve_its_own_rate():
    """A portfolio is the one replay that can hold both asset classes. One flat
    rate would either invent a stock commission or excuse the crypto one, so
    there is no single `fee_pct_per_side` to report and it says None rather than
    picking one of the two."""
    stock = {
        "id": 2, "name": "Stock", "asset_class": "stock", "swing_mode": True,
        "sizing_usd": 1000.0, "sleeve_usd": 5000.0, "max_positions": 3,
        "params": _crypto_strategy()["params"],
    }
    result = run_portfolio_backtest(
        [_crypto_strategy(), stock],
        {1: {"AAA/USD": _intraday()}, 2: {"BBB": _intraday()}},
        RISK, starting_cash=5000, spread_pct=0, market="stock",
        fee_pct_by_class=FEES,
    )
    assert result["fee_pct_per_side"] is None
    assert result["fee_pct_per_side_by_class"] == {"crypto": 0.25, "stock": 0.0}


@pytest.mark.parametrize("field", ["fees_paid", "fee_pct_per_side_by_class"])
def test_the_fee_fields_are_present_even_when_nothing_is_charged(field):
    """"No fees were charged" and "this backtest doesn't model fees" have to be
    distinguishable — the second is what the portfolio result used to say by
    omission, and it is why an all-crypto book showed a profit nobody could have
    banked."""
    assert field in _run(JUMP_ON_D_MINUS_1)
