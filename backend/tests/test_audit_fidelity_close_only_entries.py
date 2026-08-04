"""A missed ENTRY can be the bar size. A missed exit cannot, and the report
should not leave the reader to work out which.

The owner spotted the asymmetry from the reports: "at worst one would think there
would be timing mismatches, not trigger/buy/sell mismatches — they each have a
min and max." Half right, and the half that is wrong is the interesting one.

EXITS are judged against the bar's extremes. `evaluate_exit` is handed
`bar_high` and `bar_low`, and `_fill_price` clamps the fill into the bar's own
range, so a stop breached in the middle of a bar fires and fills at the level.
A move that happened inside a bar cannot hide from an exit.

ENTRIES are judged on the CLOSE alone. `evaluate_entry` receives
`price=bar["close"]` with a `change_pct` computed from that same close; no high
ever reaches it. So a signal that appeared and vanished inside one bar is
structurally invisible to the replay while the live engine, looking every sixty
seconds, could act on it — and the report says "the replay was watching this
symbol and passed, this is the kind that points at a real bug", which sends
somebody after an entry-rule bug that does not exist.

That was measured once already and is in the record: FIL bought live at 13:18:18,
between the replay's 13:15 and 13:30 bars, came back as a trade the replay
missed. Minute bars shrink the window; they do not close it.

This does not change what the replay DOES. Judging entries on a bar's high would
make the replay strictly more permissive and start inventing trades on wicks —
a much worse error than the one being explained. The bar size is simply named,
so the reader can tell "your rules disagree" from "your rules could not be
evaluated at this resolution".
"""

from qt.services.fidelity import compare

DAY = "2026-08-04"


def _live(symbol="SPY"):
    return {"symbol": symbol, "entry_day": DAY, "entry_at": "2026-08-04T18:58:00+00:00",
            "exit_day": None, "exit_at": None, "entry_price": 100.0, "pnl": None,
            "status": "open", "entry_reason": "gain", "exit_reason": None}


def _missed(bar_seconds):
    report = compare([_live()], {"trade_list": [], "open_positions": []},
                     replayed_symbols=["SPY"], bar_seconds=bar_seconds)
    return next(r for r in report["log"] if r["action"] == "bought")


def test_a_watched_and_passed_verdict_names_the_bar_size():
    """The reader has to be able to tell a signal difference from a resolution
    one, and the verdict alone cannot."""
    row = _missed(900.0)
    assert "15 minutes" in row["detail"], row["detail"]
    assert "close" in row["detail"].lower(), row["detail"]


def test_it_says_the_asymmetry_out_loud():
    """Naming the bar size without saying WHY it matters for entries and not for
    exits leaves the reader to guess, and the guess most people make — "the
    replay just didn't see the price" — is wrong for exits."""
    assert "exits" in _missed(60.0)["detail"].lower()


def test_the_verdict_itself_is_unchanged():
    """This is a caveat, not an excuse. The trade really was not reproduced, and
    softening the verdict would hide the rows that are genuinely worth chasing."""
    assert _missed(60.0)["verdict"] == "replay missed it"
    assert "watching this symbol and passed" in _missed(60.0)["detail"]


def test_an_unknown_bar_size_adds_nothing():
    """Same rule as everywhere else in this file: unknown is not a licence to
    make a claim. Without the resolution there is nothing truthful to say."""
    row = _missed(None)
    assert "minute" not in row["detail"], row["detail"]
    assert row["verdict"] == "replay missed it"


def test_a_symbol_outside_the_universe_is_not_given_the_excuse():
    """It was never being evaluated at any resolution, so the bar size explains
    nothing — and offering it would bury the real reason, which is coverage."""
    report = compare([_live("NVDA")], {"trade_list": [], "open_positions": []},
                     replayed_symbols=["SPY"], bar_seconds=900.0)
    row = next(r for r in report["log"] if r["action"] == "bought")
    assert "wasn't in the universe" in row["detail"], row["detail"]
    assert "15 minutes" not in row["detail"], row["detail"]
