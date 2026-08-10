"""Every reconciled close booked a profit, because it could not book anything else.

A "reconciled close" means the position left the account without QT seeing the
exit. QT then had to write down an exit price, and what it wrote down was
`high_water` — the highest price the position traded at while held.

`high_water >= entry_price` by construction. So

    pnl = (high_water - entry_price) * qty

cannot be negative. Measured in the live journal on 2026-08-10:

    105 reconciled closes
    total P&L          +$101.36
    sum of GAINS only  +$101.36     <- identical, so not one loss in the set

105 for 105 is not a good run, it is an arithmetic guarantee, and it flattered
every strategy that ever had a position reconciled.

THE FIX IS TO ASK RATHER THAN GUESS. Alpaca records every fill under account
activities, so the exit price is not unknowable — it was simply never fetched.
`exit_fill_price` takes the last sell of that symbol at or after the entry, the
one that emptied the position, and books that.

WHEN THERE IS NO FILL TO FIND — it aged out of the window, or the call failed —
the close books at the ENTRY price, so the P&L reads zero. Zero is wrong too.
It is chosen because it is wrong without LEANING: an exit QT could not observe
must not be allowed to flatter the strategy that owned it. The trade says which
of the two happened, in `exit_reason`, so a zero from ignorance is never mistaken
for a genuine scratch.
"""

from datetime import datetime, timedelta, timezone

from qt.services.reconcile import _fill_time, exit_fill_price

ENTRY = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)


def _fill(symbol="AVAX/USD", side="sell", price="6.40", minutes=30, **over):
    row = {
        "symbol": symbol, "side": side, "price": price,
        "transaction_time": (ENTRY + timedelta(minutes=minutes)).isoformat().replace(
            "+00:00", "Z"),
    }
    row.update(over)
    return row


# ── the core claim ───────────────────────────────────────────────────────────
def test_the_brokers_fill_is_used():
    assert exit_fill_price([_fill(price="6.40")], "AVAX/USD", ENTRY) == 6.40


def test_a_sale_below_entry_is_reported_as_such():
    """THE BUG, stated as a value. The old code could not return a price below
    the entry; this one must, or nothing has changed."""
    got = exit_fill_price([_fill(price="5.00")], "AVAX/USD", ENTRY)
    assert got == 5.00
    assert got < 6.462, "a loss must be representable"


def test_the_last_sell_wins_not_the_best_one():
    """The fill that emptied the position is the last one. Picking the highest
    would reintroduce exactly the optimism this replaces."""
    fills = [_fill(price="9.00", minutes=10), _fill(price="5.00", minutes=90)]
    assert exit_fill_price(fills, "AVAX/USD", ENTRY) == 5.00
    assert exit_fill_price(list(reversed(fills)), "AVAX/USD", ENTRY) == 5.00


def test_nothing_to_find_returns_none():
    """Which the caller turns into a zero-P&L close, not a guess."""
    assert exit_fill_price([], "AVAX/USD", ENTRY) is None


# ── which fills count ────────────────────────────────────────────────────────
def test_buys_are_ignored():
    """An entry fill sits in the same feed and is usually the nearest one in
    time. Booking it would report the position as a perfect scratch."""
    assert exit_fill_price([_fill(side="buy", price="6.46")], "AVAX/USD", ENTRY) is None


def test_another_symbol_is_ignored():
    assert exit_fill_price([_fill(symbol="SOL/USD")], "AVAX/USD", ENTRY) is None


def test_the_slashless_broker_form_still_matches():
    """Alpaca returns crypto slash-less. Without normalising, no crypto close
    would EVER find its fill and every one would silently book zero — the fix
    would look like it worked while doing nothing."""
    assert exit_fill_price([_fill(symbol="AVAXUSD", price="6.40")], "AVAX/USD", ENTRY) == 6.40


def test_a_sale_before_this_entry_is_ignored():
    """The same symbol is bought and sold repeatedly — AVAX has 17 closed trades
    in the live journal. A sell that happened BEFORE this trade opened belongs
    to an earlier position."""
    assert exit_fill_price([_fill(minutes=-60)], "AVAX/USD", ENTRY) is None


def test_a_sale_at_the_exact_entry_instant_counts():
    """Boundary. Fills are timestamped to the microsecond and an exclusive test
    would drop a legitimate same-instant fill."""
    assert exit_fill_price([_fill(minutes=0, price="6.40")], "AVAX/USD", ENTRY) == 6.40


def test_no_entry_time_does_not_reject_everything():
    """`entry_at` is nullable on Trade. Comparing against None must not silently
    discard every fill and send every close down the zero-P&L path."""
    assert exit_fill_price([_fill(price="6.40")], "AVAX/USD", None) == 6.40


# ── malformed input, because this runs inside the recovery path ──────────────
def test_a_junk_row_is_dropped_not_raised():
    """A crash here leaves the journal in the state reconciliation was called to
    repair. Each of these must cost one fill, not the run."""
    for bad in [{}, {"symbol": "AVAX/USD"}, _fill(price=None), _fill(price="abc"),
                _fill(transaction_time="not a date"), _fill(transaction_time=None),
                _fill(side=None), {"symbol": None, "side": "sell", "price": "1"}]:
        assert exit_fill_price([bad], "AVAX/USD", ENTRY) is None, bad


def test_a_junk_row_does_not_hide_a_good_one():
    """The control for the above: dropping bad rows must not drop the run."""
    assert exit_fill_price([{}, _fill(price="6.40")], "AVAX/USD", ENTRY) == 6.40


def test_a_zero_or_negative_price_is_not_a_fill():
    """Alpaca has returned "0" for an unsettled row. Booking it would report the
    position as a total loss."""
    assert exit_fill_price([_fill(price="0")], "AVAX/USD", ENTRY) is None
    assert exit_fill_price([_fill(price="-1")], "AVAX/USD", ENTRY) is None


# ── the timestamp parser ─────────────────────────────────────────────────────
def test_timestamps_arrive_in_several_shapes():
    """Z suffix, numeric offset, and naive — all three appear depending on the
    endpoint, and a parser that only handled one would silently discard fills."""
    assert _fill_time("2026-08-06T09:14:25Z") == datetime(
        2026, 8, 6, 9, 14, 25, tzinfo=timezone.utc)
    assert _fill_time("2026-08-06T09:14:25+00:00") == datetime(
        2026, 8, 6, 9, 14, 25, tzinfo=timezone.utc)
    naive = _fill_time("2026-08-06T09:14:25")
    assert naive is not None and naive.tzinfo is not None, "must be made aware"


def test_an_offset_is_respected_not_stripped():
    """Treating a +02:00 stamp as UTC would shift a fill two hours and could put
    it the wrong side of the entry-time cutoff."""
    assert _fill_time("2026-08-06T11:14:25+02:00") == datetime(
        2026, 8, 6, 9, 14, 25, tzinfo=timezone.utc)


def test_unparseable_timestamps_return_none():
    for bad in ["", None, "not a date", "2026-13-45T99:99:99Z"]:
        assert _fill_time(bad) is None, bad
