"""Two independent faults turned "check manually" into ~500 Slack messages a day.

Werner's actual channel, five messages inside one minute and repeating all day:

    PAPER QT's open trades total 15.1506 AVAX/USD but the broker holds 0.149527
    PAPER Alpaca holds 1e-09 DOGEUSD that QT has no open trade for
    PAPER Alpaca holds 1e-09 TEAM  that QT has no open trade for
    PAPER Alpaca holds 1e-09 UNIUSD that QT has no open trade for
    PAPER Alpaca holds 1e-09 XRPUSD that QT has no open trade for

FAULT 1 — DUST READ AS A POSITION. `1e-09` is what Alpaca's paper broker leaves
behind a full liquidation. The test for "is anything held here" was
`abs(qty) > 1e-12`, a FLOAT epsilon rather than an economic one, so a residue
worth two ten-millionths of a cent counted as a holding. It is now judged in
dollars where a price is known, and dust is treated as ABSENT everywhere rather
than merely un-alerted — the same reading the code already gives a position that
is missing outright.

FAULT 2 — NO SUPPRESSION, which is what made it a flood rather than a bug
report. Both alerts are deliberately not self-healing: they say "check manually"
precisely because QT must not guess. So the condition survives by design until a
human acts, and reconciliation runs every 15 minutes — 96 identical messages per
stuck symbol per day. Five stuck symbols is ~480.

The two are independent and both were needed. Fixing the dust alone still leaves
the AVAX mismatch — which is REAL drift and deserves an alert — repeating four
times an hour for ever. Fixing suppression alone leaves four meaningless
conditions occupying a daily alert each.

WHAT IS DELIBERATELY NOT CHANGED: neither alert became self-healing, and the
audit log still records the condition on EVERY cycle. Only the notification is
spaced. A silenced problem and a solved problem must not look the same.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.models import AuditLog
from qt.services.reconcile import (
    DUST_MAX_QTY,
    DUST_MAX_VALUE_USD,
    STANDING_ALERT_REPEAT_HOURS,
    OpenTradeView,
    PositionView,
    _alerted_since,
    _is_dust,
    reconcile,
)


def _kinds(actions) -> list[str]:
    return [a.kind for a in actions]


def _trade(symbol: str, qty: float, *, asset_class: str = "crypto") -> OpenTradeView:
    return OpenTradeView(
        id=1, symbol=symbol, qty=qty, entry_order_id="o-1",
        entry_confirmed=True, last_price=100.0, asset_class=asset_class,
    )


# ── fault 1: the 1e-09 residues ──────────────────────────────────────────────
@pytest.mark.parametrize("symbol", ["DOGEUSD", "TEAM", "UNIUSD", "XRPUSD"])
def test_the_four_symbols_from_the_channel_raise_nothing(symbol):
    """The exact reported condition: a residue with no QT trade behind it."""
    actions = reconcile([], [PositionView(symbol=symbol, qty=1e-09, current_price=180.0)], [])
    assert actions == [], actions


def test_dust_is_judged_in_dollars_not_in_quantity():
    """A quantity rule alone cannot separate these two: 0.0015 BTC is a SMALLER
    number than many dust residues and is a $97 position."""
    assert _is_dust(PositionView(symbol="BTCUSD", qty=1e-09, current_price=64000.0))
    assert not _is_dust(PositionView(symbol="BTCUSD", qty=0.0015, current_price=64000.0))


def test_the_dollar_threshold_is_where_it_says_it_is():
    price = 100.0
    just_under = (DUST_MAX_VALUE_USD / price) * 0.9
    just_over = (DUST_MAX_VALUE_USD / price) * 1.1
    assert _is_dust(PositionView(symbol="X", qty=just_under, current_price=price))
    assert not _is_dust(PositionView(symbol="X", qty=just_over, current_price=price))


def test_without_a_price_it_falls_back_to_quantity():
    """`current_price` is optional on PositionView and Alpaca does omit it. The
    fallback must still catch 1e-09 and still spare a real position.

    The "spare a real position" half is asserted at an ABSOLUTE quantity — the
    smallest genuine holding in the live account is 0.0015 BTC. Writing it as
    `DUST_MAX_QTY * 10` made the assertion move with the constant it was meant
    to constrain, and a fallback widened to 0.1 — which would silently discard
    that BTC position — passed it."""
    assert _is_dust(PositionView(symbol="X", qty=1e-09, current_price=None))
    assert not _is_dust(PositionView(symbol="X", qty=0.0015, current_price=None))
    assert DUST_MAX_QTY < 0.0015, "the fallback must sit below any real position"


def test_a_zero_price_does_not_make_everything_dust():
    """`if pos.current_price` is falsy for 0.0 as well as None. A zero price is
    missing data, not a worthless position — sending a real holding down the
    dollar branch would value it at $0 and silently discard it."""
    assert not _is_dust(PositionView(symbol="X", qty=500.0, current_price=0.0))


def test_dust_under_an_open_trade_closes_it_instead_of_alerting_for_ever():
    """The other half of fault 1. With a QT trade on the symbol the residue
    produced a QUANTITY MISMATCH rather than an orphan — a different message,
    the same endless loop. Dust means the position is gone, so the trade is
    closed the way an outright-missing position already is."""
    actions = reconcile(
        [_trade("XRP/USD", 94.6)],
        [PositionView(symbol="XRPUSD", qty=1e-09, current_price=1.03)],
        [],
    )
    assert _kinds(actions) == ["close_reconciled"], actions


def test_a_real_orphan_still_alerts():
    """THE CONTROL. Widen the dust rule too far and QT stops reporting positions
    it genuinely cannot account for — much worse than the noise being fixed."""
    actions = reconcile([], [PositionView(symbol="TEAM", qty=3.0, current_price=180.0)], [])
    assert _kinds(actions) == ["alert_orphan_position"]


def test_werners_avax_mismatch_is_real_and_still_alerts():
    """THE OTHER CONTROL, with his numbers. QT claims 15.1506 AVAX and the
    broker holds 0.149527 — that is 99% of a position missing, not dust, and not
    a crypto fee (the fee band is 0.5%). It must survive both fixes."""
    actions = reconcile(
        [_trade("AVAX/USD", 15.150551025)],
        [PositionView(symbol="AVAXUSD", qty=0.149527, current_price=6.46)],
        [],
    )
    assert _kinds(actions) == ["alert_qty_mismatch"], actions


# ── fault 2: the repetition ──────────────────────────────────────────────────
KEYS = (
    "[paper] ORPHAN position at broker: TEAM",
    "[paper] ORPHAN position at broker: UNIUSD",
)


@pytest.fixture(autouse=True)
def _no_leftover_alerts(db_session):
    """The test database is session-scoped with no per-test rollback, so audit
    rows written by one test are visible to the next — and this file's whole
    subject is "has this been logged recently", which every leftover row
    answers wrongly. Two tests failed exactly that way before this existed."""
    def clear():
        db_session.query(AuditLog).filter(AuditLog.message.in_(KEYS)).delete(
            synchronize_session=False)
        db_session.flush()

    clear()
    yield
    clear()


def _log(session, key: str, *, hours_ago: float, category: str = "reconcile"):
    session.add(AuditLog(
        category=category, message=key, detail="",
        at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    ))
    session.flush()


KEY = "[paper] ORPHAN position at broker: TEAM"


def test_the_first_alert_is_not_suppressed(db_session):
    since = datetime.now(timezone.utc) - timedelta(hours=STANDING_ALERT_REPEAT_HOURS)
    assert _alerted_since(db_session, KEY, since) is False


def test_a_repeat_inside_the_window_is_suppressed(db_session):
    _log(db_session, KEY, hours_ago=1)
    since = datetime.now(timezone.utc) - timedelta(hours=STANDING_ALERT_REPEAT_HOURS)
    assert _alerted_since(db_session, KEY, since) is True


def test_the_alert_returns_once_the_window_passes(db_session):
    """Suppression must EXPIRE. A standing problem that goes quiet for ever is
    a problem you stop knowing about, which is worse than the flood."""
    _log(db_session, KEY, hours_ago=STANDING_ALERT_REPEAT_HOURS + 1)
    since = datetime.now(timezone.utc) - timedelta(hours=STANDING_ALERT_REPEAT_HOURS)
    assert _alerted_since(db_session, KEY, since) is False


def test_a_different_symbol_is_a_different_alert(db_session):
    """Suppression is per-condition. One noisy symbol must not silence the next
    one — that would turn a fix for noise into a fix for signal."""
    _log(db_session, KEY, hours_ago=1)
    other = "[paper] ORPHAN position at broker: UNIUSD"
    since = datetime.now(timezone.utc) - timedelta(hours=STANDING_ALERT_REPEAT_HOURS)
    assert _alerted_since(db_session, other, since) is False


def test_an_unrelated_audit_category_does_not_suppress(db_session):
    """The audit log carries every category. Matching on message alone would let
    an entry written by the engine silence a reconciliation alert."""
    _log(db_session, KEY, hours_ago=1, category="engine")
    since = datetime.now(timezone.utc) - timedelta(hours=STANDING_ALERT_REPEAT_HOURS)
    assert _alerted_since(db_session, KEY, since) is False


def test_the_window_is_long_enough_to_actually_help(db_session):
    """At one reconcile every 15 minutes, a window under an hour would still
    send 24+ messages a day and would not have fixed the reported problem."""
    assert STANDING_ALERT_REPEAT_HOURS >= 4
