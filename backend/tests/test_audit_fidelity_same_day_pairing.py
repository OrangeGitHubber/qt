"""When a symbol is traded twice in a day, pair the NEAREST two — not the first.

MEASURED on AVAX/USD, 2026-08-04, and it produced the most alarming row on the
whole report from a near-perfect agreement.

  replay #1   00:38 → 01:40   6.5538 → 6.8844   take-profit +5.15%
  replay #2   01:40 → 02:08   6.90   → 6.82     stop-loss   -1.04%
  live        01:42 → 01:48   6.892  → 6.842    stop-loss   -1.13%

Live's trade is obviously replay #2: two minutes apart, the same price to a tenth
of a cent, the same rule firing, near-identical loss. The comparison paired it
with replay #1 — `sim_by_key.setdefault` keeps whichever the replay took FIRST —
and reported "Both sold AVAX the same day, but you on stop-loss and the replay on
take-profit". Two systems that agreed, described as opposites.

And replay #1 then disappeared. It is a trade the replay took and live did not,
which is the definition of an invented one, but only the paired row survives to
be reported — so the report was simultaneously inventing a disagreement and
hiding a real invention.

The day-key is right and stays: a live fill at 14:03 and a 14:00 bar ARE the same
decision, and demanding equal timestamps would report every trade as a mismatch.
What was wrong is which of several candidates the key resolves to. A crypto day
is 24 hours, so a symbol bought twice inside one is ordinary rather than exotic.
"""

from qt.services.fidelity import compare

DAY = "2026-08-04"
TOLERANCE = 120.0


def _live(at, out_at=None, price=100.0, exit_price=None, reason=None):
    return {"symbol": "AVAX/USD", "entry_day": DAY, "entry_at": at,
            "exit_day": DAY if out_at else None, "exit_at": out_at,
            "entry_price": price, "exit_price": exit_price, "pnl": -1.0,
            "status": "closed" if out_at else "open",
            "entry_reason": "gain", "exit_reason": reason}


def _sim(at, out_at=None, price=100.0, exit_price=None, reason=None):
    return {"symbol": "AVAX/USD", "entry_day": DAY, "entry_at": at,
            "exit_day": DAY if out_at else None, "exit_at": out_at,
            "entry_price": price, "exit_price": exit_price, "pnl": -1.0,
            "exit_reason": reason}


# The measured rows, to the second and the tenth of a cent.
LIVE = _live("2026-08-04T01:42:09+00:00", "2026-08-04T01:48:25+00:00",
             6.892, 6.842, "stop-loss: -1.13% <= -1%")
SIM_FIRST = _sim("2026-08-04T00:38:00+00:00", "2026-08-04T01:40:00+00:00",
                 6.5538, 6.8844, "take-profit: +5.15% >= 4%")
SIM_SECOND = _sim("2026-08-04T01:40:00+00:00", "2026-08-04T02:08:00+00:00",
                  6.90, 6.82, "stop-loss: -1.04% <= -1%")


def _report(live, sim):
    return compare(live, {"trade_list": sim, "open_positions": []},
                   timing_tolerance_seconds=TOLERANCE)


def test_the_nearest_trade_is_the_one_paired():
    """Order must not decide it. The replay's first AVAX trade opened an hour
    before live's and its second opened two minutes before — only one of those
    is the same decision."""
    report = _report([LIVE], [SIM_FIRST, SIM_SECOND])

    assert len(report["matched"]) == 1, report["matched"]
    assert report["matched"][0]["sim_entry"] == SIM_SECOND["entry_price"], (
        "live's trade was paired against the replay's OTHER trade — the one an "
        "hour earlier at a different price"
    )
    assert report["matched"][0]["sim_exit_reason"] == SIM_SECOND["exit_reason"]


def test_the_unpaired_replay_trade_is_reported_as_invented():
    """It is a trade the replay took and live did not. Before this it vanished
    entirely — counted in `same_day_duplicates` and shown nowhere."""
    report = _report([LIVE], [SIM_FIRST, SIM_SECOND])

    assert [r["symbol"] for r in report["backtest_only"]] == ["AVAX/USD"]
    assert report["backtest_only"][0]["sim_entry"] == SIM_FIRST["entry_price"]
    assert report["decision"]["invented_by_backtest"] == 1


def test_the_agreement_is_reported_as_one():
    """The whole point. Both stopped out within eight minutes of each other, on
    the same rule, for almost the same loss — and the report said one took profit
    while the other stopped out."""
    log = _report([LIVE], [SIM_FIRST, SIM_SECOND])["log"]
    sold = [r for r in log if r["action"] == "sold" and r["symbol"] == "AVAX/USD"]

    assert sold, log
    assert sold[0]["verdict"] != "reason differs", sold[0]


def test_an_unpaired_live_trade_is_still_a_miss():
    """The mirror. Two live trades and one replayed: the nearest pairs and the
    other is a trade the replay missed, not a row that quietly disappears."""
    second_live = _live("2026-08-04T05:00:00+00:00", "2026-08-04T05:30:00+00:00",
                        7.5, 7.4, "stop-loss: -1.33% <= -1%")
    report = _report([LIVE, second_live], [SIM_SECOND])

    assert len(report["matched"]) == 1
    assert report["matched"][0]["live_entry"] == LIVE["entry_price"]
    assert [r["day"] for r in report["live_only"]] == [DAY]
    assert report["decision"]["missed_by_backtest"] == 1


def test_rows_without_a_clock_still_pair_one_to_one():
    """An imported journal carries days, not moments. Nearest-in-time cannot rank
    those, so they pair in order — which is what the old code did for everything,
    and is still right when there is nothing better to go on."""
    live_a = _live(None, price=1.0)
    live_b = _live(None, price=2.0)
    report = _report([live_a, live_b], [_sim(None, price=3.0), _sim(None, price=4.0)])

    assert len(report["matched"]) == 2, report["matched"]
    assert report["decision"]["invented_by_backtest"] == 0
    assert report["decision"]["missed_by_backtest"] == 0
