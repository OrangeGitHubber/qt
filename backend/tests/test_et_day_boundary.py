"""Everything QT reports about its own trading rolls over at midnight NEW YORK.

The engine's daily counters always did (_trading_day_start). The reporting side
didn't: the scoreboard's rows, its trade list and the daily-contribution chart
all bucketed by the UTC date. During market hours those agree, which is why it
went unnoticed — but crypto trades round the clock, so a 21:00 ET fill is
already "tomorrow" in UTC and was filed a day late while the engine's own
counters said otherwise.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from qt.db import session_scope
from qt.models import BenchmarkSnapshot, Strategy, Trade
from qt.services import scoreboard
from qt.services.calendar import et_day
from qt.services.engine import _trading_day_start
from qt.settings_service import set_setting

ET = ZoneInfo("America/New_York")


def test_an_evening_trade_belongs_to_that_day_not_the_next():
    """21:00 ET on 31 July is 01:00 UTC on 1 August. It is a 31 July trade."""
    evening = datetime(2026, 7, 31, 21, 0, tzinfo=ET)
    assert evening.astimezone(timezone.utc).strftime("%Y-%m-%d") == "2026-08-01"  # the old answer
    assert et_day(evening) == "2026-07-31"                                        # the right one


def test_dst_is_handled_by_the_zone_not_an_offset():
    """A fixed -5 would be wrong in summer and a fixed -4 wrong in winter, which
    is exactly why this can't be done in SQLite's date()."""
    summer = datetime(2026, 7, 31, 23, 30, tzinfo=timezone.utc)  # EDT, UTC-4 → 19:30 ET
    winter = datetime(2026, 1, 31, 23, 30, tzinfo=timezone.utc)  # EST, UTC-5 → 18:30 ET
    assert et_day(summer) == "2026-07-31"
    assert et_day(winter) == "2026-01-31"


def test_a_naive_timestamp_is_read_as_utc():
    """SQLite hands back naive datetimes. Treating them as LOCAL would shift the
    day by however many hours the container happens to be offset."""
    assert et_day(datetime(2026, 8, 1, 1, 0)) == "2026-07-31"


def test_the_reporting_boundary_matches_the_engines_counters():
    """The rule this whole change exists to enforce: the day the journal reports
    and the day the risk counters reset must be the same day."""
    start = _trading_day_start(datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc))  # 22:00 ET Jul 31
    assert et_day(start) == "2026-07-31"
    assert et_day(start + timedelta(hours=23)) == "2026-07-31"   # still inside that ET day
    assert et_day(start + timedelta(hours=25)) == "2026-08-01"   # rolled over


def test_the_scoreboard_files_an_evening_trade_under_the_right_day(client):
    """End to end: the equity rows and the trades on them agree."""
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(BenchmarkSnapshot).delete()
        set_setting(s, "current_account_id", "A")
        s.add_all([
            BenchmarkSnapshot(day="2026-07-31", bot_equity=10_000.0, spy_close=500.0, account_id="A"),
            BenchmarkSnapshot(day="2026-08-01", bot_equity=10_100.0, spy_close=505.0, account_id="A"),
        ])
        strat = Strategy(name="C", asset_class="crypto", universe="custom", preset="custom",
                         params="{}", symbols="[]")
        s.add(strat)
        s.flush()
        # 21:00 ET on the 31st — 01:00 UTC on the 1st.
        s.add(Trade(strategy_id=strat.id, asset_class="crypto", mode="paper", status="open",
                    account_id="A", symbol="BTC/USD", qty=1, entry_price=60_000.0,
                    entry_at=datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)))
    with session_scope() as s:
        out = scoreboard.series(s)
    assert "2026-07-31" in out["trades"], "evening trade was filed under the wrong day"
    assert "2026-08-01" not in out["trades"]
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(BenchmarkSnapshot).delete()
        s.query(Strategy).delete()
