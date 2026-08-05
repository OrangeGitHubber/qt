"""After the first difference, the two accounts are different accounts.

The owner's reading of the SPY evidence, and it is the correct one:

    "what this boils down to is simply a timing issue. If the replay was running
    at the exact same time as the real life engine then everything would have
    matched perfectly... the playback runs at slightly different times, seconds
    off, and because of that with these close margins we take some losses in the
    playback which trigger some real rules. I don't think we should change
    anything. We should just very carefully explain what the possible reason was
    this one was missed, and that when one was missed it has a knock-on effect
    for the rest of that cycle."

MEASURED on strategy 25, SPY, 2026-08-04:

    18:53  EXIT rotated out of the top 5 by relative strength   (closed RED)
    18:54  reject-rail: cooldown after loss (0.0h of 24.0h)
    ...    every bar to the end of the window

Live's rotation exits closed a few cents GREEN and it re-entered twice. The
replay's fill was a hair worse, closed RED, and the 24-hour after-loss cooldown —
a rail both sides genuinely have — locked SPY out for the rest of the day. Not a
rule disagreeing: the same rule, correctly applied, to two accounts that were no
longer in the same state.

SO THE RAILS ARE NOT THE BUG AND MUST NOT BE "FIXED". What was missing is that
the report presented every later row as independent evidence when it was not.
Position slots, cash, cooldowns and the composition of a ranked top-N all depend
on what came before, so a single early difference re-writes the rest of the
comparison. A reader counting eight mismatches was counting one difference and
seven of its consequences.

Two things are added, both report-side only, and nothing about the engine or the
simulator changes:

  * the FIRST divergence is identified, because everything after it is downstream
    of it;
  * and where the replay's own earlier losing exit is what blocked a later entry,
    the row says so instead of leaving "the replay was watching and passed" to
    imply a signal difference.
"""

from qt.services.fidelity import compare

DAY = "2026-08-04"


def _live(symbol, at, out_at=None, pnl=None, reason=None):
    return {"symbol": symbol, "entry_day": DAY, "entry_at": at,
            "exit_day": DAY if out_at else None, "exit_at": out_at,
            "entry_price": 100.0, "exit_price": 101.0 if out_at else None,
            "pnl": pnl, "status": "closed" if out_at else "open",
            "entry_reason": "gain", "exit_reason": reason}


def _sim(symbol, at, out_at=None, pnl=None, reason=None):
    return {"symbol": symbol, "entry_day": DAY, "entry_at": at,
            "exit_day": DAY if out_at else None, "exit_at": out_at,
            "entry_price": 100.0, "exit_price": 99.9 if out_at else None,
            "pnl": pnl, "exit_reason": reason}


# SPY as it happened. Both sides bought at 18:20 and rotated out at ~18:53; the
# replay's fill was a hair worse and closed RED. Live then re-entered at 18:58
# and the replay could not.
ROTATED = "rotated out of the top 5 by relative strength"
LIVE_ROUND_ONE = _live("SPY", "2026-08-04T18:20:00+00:00", "2026-08-04T18:53:00+00:00",
                       pnl=1.2, reason=ROTATED)
SIM_ROUND_ONE = _sim("SPY", "2026-08-04T18:20:00+00:00", "2026-08-04T18:53:00+00:00",
                     pnl=-0.4, reason=ROTATED)
LIVE_AGAIN = _live("SPY", "2026-08-04T18:58:00+00:00")


def _report(live, sim):
    return compare(live, {"trade_list": sim, "open_positions": []},
                   replayed_symbols=["SPY", "MSFT"], bar_seconds=60.0,
                   timing_tolerance_seconds=120.0)


def _bought(report, at):
    return next(r for r in report["log"]
                if r["action"] == "bought" and r["at"].startswith(at))


def test_a_block_caused_by_the_replays_own_earlier_loss_says_so():
    """The measured row. "The replay was watching this symbol and passed" implies
    it looked at your rules and disagreed. It never got that far — its own
    earlier exit had armed a rail that both engines have."""
    report = _report([LIVE_ROUND_ONE, LIVE_AGAIN], [SIM_ROUND_ONE])
    row = _bought(report, "2026-08-04T18:58")

    assert "at a loss" in row["detail"], row["detail"]
    assert "cooldown" in row["detail"].lower(), row["detail"]
    assert "consequence" in row["detail"].lower(), row["detail"]


def test_the_same_row_without_an_earlier_losing_exit_is_untouched():
    """The control. A miss with no prior losing exit behind it is a real
    difference and must keep reading like one — this excuse is only available
    when the evidence for it is on the page."""
    winner = dict(SIM_ROUND_ONE, pnl=0.9)
    row = _bought(_report([LIVE_ROUND_ONE, LIVE_AGAIN], [winner]), "2026-08-04T18:58")

    assert "at a loss" not in row["detail"], row["detail"]
    assert "watching this symbol and passed" in row["detail"], row["detail"]


def test_the_first_divergence_is_identified():
    """Everything after it is downstream of it, so the reader needs to know where
    the comparison stopped being independent."""
    report = _report([LIVE_ROUND_ONE, LIVE_AGAIN], [SIM_ROUND_ONE])
    first = report["first_divergence"]

    assert first is not None
    assert first["symbol"] == "SPY"
    assert first["at"].startswith("2026-08-04T18:58"), first
    assert "consequence" in first["note"].lower(), first["note"]


def test_a_comparison_that_never_diverged_says_nothing():
    """No divergence, no caveat. A report that warns about cascade on a run where
    both sides agreed throughout is crying wolf, and the whole point of this work
    has been not doing that."""
    sim = _sim("SPY", "2026-08-04T18:20:00+00:00", "2026-08-04T18:53:00+00:00",
               pnl=1.2, reason=ROTATED)
    assert _report([LIVE_ROUND_ONE], [sim])["first_divergence"] is None


def test_the_earliest_difference_wins_not_the_worst():
    """Ordered by TIME, not by severity. A dramatic disagreement at 3pm is a
    consequence of a two-minute timing difference at 10am, and naming the 3pm one
    would send the reader to the end of the chain."""
    early = _live("MSFT", "2026-08-04T14:00:00+00:00")     # missed, early
    report = _report([early, LIVE_ROUND_ONE, LIVE_AGAIN], [SIM_ROUND_ONE])

    assert report["first_divergence"]["symbol"] == "MSFT"
    assert report["first_divergence"]["at"].startswith("2026-08-04T14:00")
