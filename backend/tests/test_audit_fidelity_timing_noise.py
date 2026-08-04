"""Two minutes is not a finding; two hours is. And a replay that already holds a
symbol did not "miss" your buy.

BOTH MEASURED, on the first reports where same-day trades paired correctly.

THE THRESHOLD WAS CUTTING THROUGH THE NOISE BAND. One bar plus one poll is 120
seconds on a 1-minute replay, and real gaps land right on it:

    XRP  02:46 vs 02:48  ->  match            (118s)
    ADA  23:12 vs 23:10  ->  timing differs   (129s)

Two rows that read identically to a human, given opposite verdicts by a
three-second difference nobody can act on. And the reason the band is noisy at
all is physical: live's `entry_at` is a FILL, the replay's is a BAR CLOSE. The
engine polls on a 60-second cycle at a phase nothing records, then places an
order that takes time to fill — none of which the replay models, and all of which
pushes live later than the replay for reasons that are not disagreements.

THE OTHER ONE IS A WORDING FAULT WITH TEETH. Strategy 25 bought SPY at 11:25,
rotated out at 14:37, straight back in at 14:37, out at 14:58, in again, out at
15:00, in again — three round trips on relative-strength ranking in thirty-five
minutes. The replay bought once and held. Every one of those live re-entries came
back "the replay was watching this symbol and passed — this is the kind that
points at a real bug", at moments when the replay WAS HOLDING SPY and could not
have bought again: `run_backtest` rejects it outright with "this strategy already
holds this symbol". That is a position-state difference — the replay never sold,
so it never re-bought — and calling it a missed signal sends someone hunting for
an entry-rule bug that does not exist.
"""

from datetime import datetime, timedelta, timezone

from qt.services.fidelity import compare

DAY = "2026-08-04"
ONE_MINUTE_REPLAY = 60.0 + 60.0


def _live(at, symbol="ADA/USD", out_at=None):
    return {"symbol": symbol, "entry_day": DAY, "entry_at": at,
            "exit_day": DAY if out_at else None, "exit_at": out_at,
            "entry_price": 100.0, "exit_price": 99.0 if out_at else None,
            "pnl": -1.0, "status": "closed" if out_at else "open",
            "entry_reason": "gain", "exit_reason": "stop-loss" if out_at else None}


def _sim(at, symbol="ADA/USD", out_at=None):
    return {"symbol": symbol, "entry_day": DAY, "entry_at": at,
            "exit_day": DAY if out_at else None, "exit_at": out_at,
            "entry_price": 100.0, "exit_price": 99.0 if out_at else None,
            "pnl": -1.0, "exit_reason": "stop-loss" if out_at else None}


def _verdict(live, sim, action="bought"):
    report = compare(live, {"trade_list": [], "open_positions": sim},
                     replayed_symbols=["ADA/USD", "SPY"],
                     timing_tolerance_seconds=ONE_MINUTE_REPLAY)
    return next(r for r in report["log"] if r["action"] == action)


def test_the_two_minute_band_reads_the_same_on_both_sides_of_it():
    """The measured pair. 118 seconds and 129 seconds are the same event to
    anybody reading the page, and were being given opposite verdicts."""
    early = _verdict([_live("2026-08-04T02:46:00+00:00")],
                     [_sim("2026-08-04T02:48:00+00:00")])
    late = _verdict([_live("2026-08-04T23:12:09+00:00")],
                    [_sim("2026-08-04T23:10:00+00:00")])
    assert early["verdict"] == late["verdict"] == "match", (early, late)


def test_a_gap_the_fill_latency_cannot_explain_is_still_flagged():
    """The floor must not swallow the ones that matter. Eighteen minutes was the
    real SPY gap on strategy 25, and thirteen hours the real SOL gap on 18."""
    start = datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)
    for minutes in (18, 51, 780):
        row = _verdict(
            [_live(start.isoformat())],
            [_sim((start + timedelta(minutes=minutes)).isoformat())],
        )
        assert row["verdict"] == "timing differs", (minutes, row)


# SPY as it really happened: live in at 11:25, out at 14:37, straight back in at
# 14:58 — while the replay bought once at 11:43 and never let go. The first live
# entry pairs with the replay's; the RE-entry is the row that was being called a
# missed signal.
SPY_IN = _live("2026-08-04T11:25:00+00:00", symbol="SPY",
               out_at="2026-08-04T14:37:00+00:00")
SPY_AGAIN = _live("2026-08-04T14:58:00+00:00", symbol="SPY")


def test_a_buy_the_replay_could_not_make_because_it_was_holding_says_so():
    """The replay did not pass on the signal — `run_backtest` refused the
    candidate with "this strategy already holds this symbol" before any entry
    rule was read."""
    still_holding = _sim("2026-08-04T11:43:00+00:00", symbol="SPY")
    report = compare([SPY_IN, SPY_AGAIN],
                     {"trade_list": [], "open_positions": [still_holding]},
                     replayed_symbols=["SPY"],
                     timing_tolerance_seconds=ONE_MINUTE_REPLAY)
    row = next(r for r in report["log"]
               if r["action"] == "bought" and r["at"].startswith("2026-08-04T14:58"))

    assert row["verdict"] != "replay missed it", row
    assert "points at a real bug" not in row["detail"], row
    assert "already holding" in row["detail"], row


def test_a_buy_after_the_replay_let_go_is_a_real_miss_again():
    """The control. Once the replay has sold, a live buy it did not make is a
    signal difference and must keep saying so — this is the verdict the whole
    report exists to produce, and silencing it would be the same fault pointing
    the other way."""
    let_go = _sim("2026-08-04T11:43:00+00:00", symbol="SPY",
                  out_at="2026-08-04T12:00:00+00:00")
    report = compare([SPY_IN, SPY_AGAIN],
                     {"trade_list": [let_go], "open_positions": []},
                     replayed_symbols=["SPY"],
                     timing_tolerance_seconds=ONE_MINUTE_REPLAY)
    row = next(r for r in report["log"]
               if r["action"] == "bought" and r["at"].startswith("2026-08-04T14:58"))

    assert row["verdict"] == "replay missed it", row
    assert "watching this symbol and passed" in row["detail"], row
