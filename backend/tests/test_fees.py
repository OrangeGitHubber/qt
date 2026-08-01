"""Commissions in the backtest.

Alpaca's US equities are commission-free, but CRYPTO is not: 0.15%-0.25% per
side at the entry volume tier, so a round trip costs roughly half a percent
(https://docs.alpaca.markets/docs/crypto-fees). A strategy taking 1-3% moves
several times a day hands over a real share of its edge, and a backtest that
ignores that reports a profit the account would never have seen.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import run_backtest

RISK = {"max_daily_loss_usd": 1e9, "max_daily_loss_pct": 100, "max_total_positions": 50,
        "max_total_exposure_usd": 1e9, "max_trades_per_day": 200,
        "cooldown_hours_after_loss": 0, "wash_sale_guard": "off", "leverage_enabled": False}


def _sawtooth(cycles: int = 12) -> list[dict]:
    """Climbs, with a sharp dip every fourth bar — round-trips often, which is
    exactly the shape fees punish.

    It has to CLIMB: crypto day-gain is measured over a rolling 24 hours, and
    bars are 6h apart, so a series that returns to flat each day never clears the
    entry threshold and the fixture would test nothing."""
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    out, price, t = [], 100.0, start
    for _ in range(cycles):
        for step in (1.02, 1.02, 1.02, 0.97):
            price *= step
            t += timedelta(hours=6)
            out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": price, "h": price,
                        "l": price, "c": price, "v": 1000, "vw": price})
    return out


def _strategy() -> dict:
    return {
        "asset_class": "crypto", "swing_mode": False, "sizing_usd": 1000,
        "sleeve_usd": 5000, "max_positions": 1,
        "params": {
            "entry": {"min_day_gain_pct": 1.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 2, "stop_loss_pct": 5, "take_profit_pct": 3,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
    }


def _run(fee_pct: float) -> dict:
    return run_backtest(_strategy(), {"BTC/USD": _sawtooth()}, RISK,
                        starting_cash=5000, spread_pct=0, fee_pct=fee_pct, market="crypto")


def test_fees_reduce_the_result_and_are_reported():
    free, charged = _run(0.0), _run(0.25)
    assert free["trades"] > 0, "the fixture needs round trips to say anything"
    assert charged["fees_paid"] > 0
    assert charged["net_pnl"] < free["net_pnl"], "charging a commission left P&L unchanged"
    assert charged["fee_pct_per_side"] == 0.25


def test_the_fee_is_charged_on_both_sides_of_a_round_trip():
    """Roughly 2 x rate x notional per completed trade. Checked as a band, not an
    exact figure — the point is that BOTH sides are charged, not one."""
    charged = _run(0.25)
    closed = charged["trades"]
    per_trade_notional = 1000.0
    expected = closed * 2 * 0.0025 * per_trade_notional
    assert 0.5 * expected < charged["fees_paid"] < 1.6 * expected, (
        f"{charged['fees_paid']} isn't consistent with charging both sides"
    )


def test_a_free_run_reports_zero_rather_than_omitting_it():
    """A stock backtest is genuinely fee-free; the field must still be present so
    the UI can say '$0.00' instead of leaving a blank that reads as unknown."""
    free = _run(0.0)
    assert free["fees_paid"] == 0.0 and free["fee_pct_per_side"] == 0.0


def test_the_entry_fee_is_paid_out_of_cash_not_conjured():
    """The buy costs price x qty PLUS the fee, so a run that can just barely
    afford a position must not be able to afford it once the fee is added."""
    bars = {"BTC/USD": _sawtooth(2)}
    strat = _strategy() | {"sizing_usd": 1000}
    # Exactly enough cash for the notional, nothing spare for a commission.
    tight = run_backtest(strat, bars, RISK, starting_cash=1000, spread_pct=0,
                         fee_pct=0.25, market="crypto")
    assert tight["trades"] + len(tight["open_positions"]) == 0, "spent cash it didn't have"
