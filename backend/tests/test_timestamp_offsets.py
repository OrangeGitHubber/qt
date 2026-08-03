"""Every timestamp the API emits must carry a UTC offset.

QT stores UTC, but SQLite returns NAIVE datetimes, and an offset-less ISO string
is read as LOCAL time by a browser (and by anything else that follows the spec).
The frontend currently compensates by appending "Z" to unstamped values — these
tests hold the line at the source instead, so the API stops misrepresenting its
own data and the compensation becomes a no-op rather than a load-bearing patch.

Two things are asserted throughout, because either alone is passable while the
other is broken:

  1. the string carries an offset at all, and
  2. it still denotes the same INSTANT as the row in the database — a helper
     that stamped the SERVER's local zone onto a UTC value would satisfy (1)
     while moving the moment by hours.
"""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Basket, Strategy, Trade, WatchlistItem
from qt.timeutil import iso_utc, utc_aware

# The frontend's own test for "is this already stamped?" (frontend/src/lib/
# datetime.ts, parseInstant). Anything matching it is passed through untouched;
# anything else gets a "Z" bolted on. Copied here so the backend can prove it
# emits the form the browser will NOT have to repair.
FRONTEND_STAMPED = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")

STRAT = {
    "name": "Offset test strategy",
    "asset_class": "stock",
    "universe": "scanner",
    "preset": "custom",
    "params": {
        "entry": {"min_day_gain_pct": 3, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 12,
                 "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
    },
    "sizing_usd": 200, "sleeve_usd": 1000, "max_positions": 3,
    "swing_mode": True, "ignore_regime": False,
}

# A moment with a distinctive wall clock: 23:30 UTC is the PREVIOUS day west of
# Greenwich and the next hour east of it, so mislabelling it is visible.
MOMENT = datetime(2026, 7, 15, 23, 30, 5, tzinfo=timezone.utc)


def assert_stamped(value: str | None, expected: datetime) -> None:
    """`value` is an ISO string that (a) carries an offset the browser accepts
    and (b) denotes `expected`."""
    assert value, "the API returned no timestamp at all"
    assert FRONTEND_STAMPED.search(value), f"{value!r} carries no UTC offset"
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() is not None, f"{value!r} parsed as a naive datetime"
    assert parsed == expected, f"{value!r} is not the moment that was stored"


# ---------------------------------------------------------------------------
# The shared helper
# ---------------------------------------------------------------------------


def test_naive_input_is_declared_utc():
    """The whole point: a naive datetime — what SQLite hands back — comes out
    stamped UTC, with its wall clock untouched."""
    naive = datetime(2026, 7, 15, 23, 30, 5)

    assert utc_aware(naive) == datetime(2026, 7, 15, 23, 30, 5, tzinfo=timezone.utc)
    assert utc_aware(naive).tzinfo is timezone.utc
    assert iso_utc(naive) == "2026-07-15T23:30:05+00:00"
    assert FRONTEND_STAMPED.search(iso_utc(naive))


def test_an_already_aware_input_is_left_alone():
    """A value that already knows its offset is NOT relabelled. Forcing UTC on
    a +02:00 timestamp would move the instant two hours, which is exactly the
    class of bug this helper exists to prevent."""
    aware = datetime(2026, 7, 15, 23, 30, 5, tzinfo=timezone(timedelta(hours=2)))

    assert utc_aware(aware) is aware
    assert iso_utc(aware) == "2026-07-15T23:30:05+02:00"
    # Same instant, different label — the helper must not have shifted it.
    assert datetime.fromisoformat(iso_utc(aware)) == datetime(2026, 7, 15, 21, 30, 5, tzinfo=timezone.utc)


def test_none_stays_none():
    """"No timestamp" is a real answer (an open trade has no exit), and it must
    survive as null rather than becoming an epoch or a crash."""
    assert utc_aware(None) is None
    assert iso_utc(None) is None


# ---------------------------------------------------------------------------
# End to end: strategies.py
# ---------------------------------------------------------------------------


def test_strategy_enabled_at_and_holdings_entry_at_are_stamped(client):
    """Both of strategies.py's timestamps, through the real endpoints: the
    go-live stamp on the strategy list, and the entry time of a held position."""
    sid = client.post("/api/strategies", json=STRAT).json()["id"]
    try:
        with session_scope() as s:
            s.get(Strategy, sid).enabled_at = MOMENT.replace(tzinfo=None)  # as SQLite stores it
            s.add(Trade(strategy_id=sid, mode="shadow", symbol="AAA", asset_class="stock",
                        qty=1, notional=100, status="open", entry_price=100,
                        entry_at=MOMENT.replace(tzinfo=None)))

        row = next(r for r in client.get("/api/strategies").json() if r["id"] == sid)
        assert_stamped(row["enabled_at"], MOMENT)

        holdings = client.get(f"/api/strategies/{sid}/holdings").json()
        assert_stamped(holdings["holdings"][0]["entry_at"], MOMENT)
    finally:
        with session_scope() as s:
            s.query(Trade).filter(Trade.strategy_id == sid).delete()
        client.delete(f"/api/strategies/{sid}")


def test_a_strategy_that_never_went_live_reports_no_time(client):
    """The null path stays null — "not enabled yet" must not become a fake
    moment just because everything else now gets stamped."""
    sid = client.post("/api/strategies", json=STRAT | {"name": "Never enabled"}).json()["id"]
    try:
        row = next(r for r in client.get("/api/strategies").json() if r["id"] == sid)
        assert row["enabled_at"] is None
    finally:
        client.delete(f"/api/strategies/{sid}")


# ---------------------------------------------------------------------------
# End to end: market.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def configured():
    """Fake keys so require_client lets the watchlist endpoint run."""
    with session_scope() as session:
        security.set_secret(session, SECRET_KEY_ID, "k")
        security.set_secret(session, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as session:
        security.delete_secret(session, SECRET_KEY_ID)
        security.delete_secret(session, SECRET_KEY_SECRET)


def test_watchlist_added_at_is_stamped(client, configured):
    with session_scope() as s:
        s.add(WatchlistItem(symbol="ZZZ", asset_class="stock",
                            added_at=MOMENT.replace(tzinfo=None)))
    try:
        with (
            patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value={})),
            patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={})),
            patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})),
        ):
            body = client.get("/api/watchlist").json()
        row = next(r for r in body["items"] if r["symbol"] == "ZZZ")
        assert_stamped(row["added_at"], MOMENT)
    finally:
        with session_scope() as s:
            s.query(WatchlistItem).filter(WatchlistItem.symbol == "ZZZ").delete()


# ---------------------------------------------------------------------------
# End to end: status.py, baskets.py, engine.py
# ---------------------------------------------------------------------------


def test_status_stamps_a_legacy_unstamped_heartbeat(client):
    """The engine heartbeat is a STRING in the settings table. Today's writer
    stores an offset, but one written by an older build did not — and /status
    must not hand that bare value to a browser that will call it local."""
    from qt.settings_service import set_setting

    with session_scope() as s:
        set_setting(s, "last_tick_at", "2026-07-15T23:30:05")  # no offset, as a legacy row has
    try:
        assert_stamped(client.get("/api/status").json()["last_tick_at"], MOMENT)
    finally:
        with session_scope() as s:
            set_setting(s, "last_tick_at", None)


def test_basket_created_at_is_stamped(client):
    bid = client.post("/api/baskets", json={"name": "Offset test basket"}).json()["id"]
    try:
        with session_scope() as s:
            s.get(Basket, bid).created_at = MOMENT.replace(tzinfo=None)
        row = next(b for b in client.get("/api/baskets").json() if b["id"] == bid)
        assert_stamped(row["created_at"], MOMENT)
    finally:
        client.delete(f"/api/baskets/{bid}")


def test_open_positions_entry_at_is_stamped(client):
    """/api/engine/positions is the account-wide holdings view — it was the one
    call site in engine.py that skipped the module's own stamping helper."""
    sid = client.post("/api/strategies", json=STRAT | {"name": "Positions offset"}).json()["id"]
    try:
        with session_scope() as s:
            s.add(Trade(strategy_id=sid, mode="shadow", symbol="BBB", asset_class="stock",
                        qty=1, notional=100, status="open", entry_price=100,
                        entry_at=MOMENT.replace(tzinfo=None)))
        rows = client.get("/api/engine/positions").json()["positions"]
        assert_stamped(next(r for r in rows if r["symbol"] == "BBB")["entry_at"], MOMENT)
    finally:
        with session_scope() as s:
            s.query(Trade).filter(Trade.strategy_id == sid).delete()
        client.delete(f"/api/strategies/{sid}")


# ---------------------------------------------------------------------------
# Dates are NOT timestamps
# ---------------------------------------------------------------------------


def test_day_buckets_stay_bare_dates(client):
    """A day bucket is a calendar day, not an instant. Give it a time and an
    offset and it becomes a moment that lands on the day BEFORE for anyone west
    of UTC — which would silently break the fidelity report, whose whole method
    is matching live trades to replayed ones by day."""
    sid = client.post("/api/strategies", json=STRAT | {"name": "Day bucket test"}).json()["id"]
    try:
        with session_scope() as s:
            from qt.settings_service import set_setting

            set_setting(s, "engine_mode", "paper")
            s.add(Trade(strategy_id=sid, mode="paper", symbol="CCC", asset_class="stock",
                        qty=1, notional=100, status="closed", entry_price=100, exit_price=110,
                        pnl=10.0, entry_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        exit_at=datetime.now(timezone.utc).replace(tzinfo=None)))

        days = client.get("/api/engine/strategy-pnl-daily").json()["days"]
        assert days, "the closed trade should have produced a day bucket"
        for day in days:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day), f"{day!r} is not a bare date"
    finally:
        with session_scope() as s:
            from qt.settings_service import set_setting

            s.query(Trade).filter(Trade.strategy_id == sid).delete()
            set_setting(s, "engine_mode", "off")
        client.delete(f"/api/strategies/{sid}")
