"""An exit is only judgeable when the two sides opened the same trade.

MEASURED on "Crypto - many movements". Six rows judged an exit against a position
the replay bought at a completely different moment:

    SOL   live 20:57 -> 04:27      replay bought 09:55, five hours AFTER live sold
    PAXG  live 22:01 -> 22:22      replay bought 20:30, two hours earlier
    ETH   live 21:03 -> 08:56      replay bought 13 hours later
    AAVE  live 21:58 -> 12:44      replay bought 51 minutes earlier

"The replay was still holding it when the window ended" is true of every one, and
tells you nothing about the exit rules: a trailing stop trails from the entry
price, so two positions opened hours apart have different stop levels and are
simply different trades. The report already flags the ENTRY as "timing differs" —
the exit was then reporting the same difference a second time, dressed as an exit
fault.

The report has done this correctly for two other cases since long before tonight:
an exit the user closed by hand, and one that fell in a later stretch than its
entry. Both are set aside with `exit_comparable` and counted apart. This is the
third case of the same kind and it was missing.

WHAT IT MUST NOT DO is silence the real ones. After this, SOL on 08-04 (entries
04:28 vs 04:28) and XRP (02:46 vs 02:48) still read "replay never sold" — those
are entries that agree to the minute and exits that genuinely diverged, and they
are the only two left on the page worth chasing.
"""

from qt.services.fidelity import compare

DAY = "2026-08-04"
TOLERANCE = 120.0


def _live(at, out_at, reason="stop-loss: -1.35% <= -1%"):
    return {"symbol": "SOL/USD", "entry_day": DAY, "entry_at": at,
            "exit_day": DAY, "exit_at": out_at, "entry_price": 100.0,
            "exit_price": 98.0, "pnl": -2.0, "status": "closed",
            "entry_reason": "gain", "exit_reason": reason}


def _sim(at, out_at=None, reason=None):
    return {"symbol": "SOL/USD", "entry_day": DAY, "entry_at": at,
            "exit_day": DAY if out_at else None, "exit_at": out_at,
            "entry_price": 100.0, "exit_price": 98.0 if out_at else None,
            "pnl": -2.0 if out_at else None, "exit_reason": reason}


def _report(live, sim):
    return compare([live], {"trade_list": [], "open_positions": [sim]},
                   timing_tolerance_seconds=TOLERANCE)


def _sold(report):
    return next(r for r in report["log"] if r["action"] == "sold")


# The measured SOL row: live ran 00:57 -> 08:27 while the replay's paired
# position was not opened until 13:55, five hours after live had already sold.
FAR_APART = (_live("2026-08-04T00:57:00+00:00", "2026-08-04T08:27:00+00:00"),
             _sim("2026-08-04T13:55:00+00:00"))
TOGETHER = (_live("2026-08-04T04:28:00+00:00", "2026-08-04T10:01:00+00:00"),
            _sim("2026-08-04T04:28:00+00:00"))


def test_an_exit_whose_entry_did_not_line_up_is_set_aside():
    row = _sold(_report(*FAR_APART))
    assert row["verdict"] == "not compared", row
    assert "13 hours" in row["detail"], row["detail"]
    assert "by hand" not in row["detail"], "that is a different reason entirely"


def test_it_is_not_counted_as_a_hand_closed_exit():
    """`manual_exits` says "you closed these yourself, so the exit half of this
    report describes less than it looks like". Folding a timing difference into
    that number would blame the user for the replay's entry."""
    decision = _report(*FAR_APART)["decision"]
    assert decision["manual_exits"] == 0, decision
    assert decision["exits_entry_mismatched"] == 1, decision


def test_an_exit_on_a_trade_that_did_line_up_is_still_judged():
    """The control, and the whole point. SOL on 08-04 opened at 04:28 on both
    sides and live stopped out while the replay held — that is a real exit
    difference and must keep saying so."""
    row = _sold(_report(*TOGETHER))
    assert row["verdict"] == "replay never sold", row


def test_the_exit_percentages_skip_it_rather_than_scoring_it():
    """Same treatment the other two incomparable cases get: excluded from the
    denominator, not counted as a disagreement. Scoring it either way reports the
    entry difference a second time."""
    report = _report(*FAR_APART)
    assert report["decision"]["exits_compared"] == 0, report["decision"]
