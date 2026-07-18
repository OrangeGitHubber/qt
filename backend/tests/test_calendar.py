"""Pure market-calendar helpers: trading day / half day / holiday."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from qt.broker.alpaca import AlpacaClient
from qt.services import calendar
from qt.services.calendar import (
    find_day,
    is_half_day,
    is_trading_day,
    market_closed_on,
)

# July 3 2026 is an early close (half day); July 4 is a Saturday holiday and is
# simply absent; July 6 is a normal session.
CAL = [
    {"date": "2026-07-02", "open": "09:30", "close": "16:00"},
    {"date": "2026-07-03", "open": "09:30", "close": "13:00"},  # half day
    {"date": "2026-07-06", "open": "09:30", "close": "16:00"},
]


def test_find_day():
    assert find_day(CAL, "2026-07-03")["close"] == "13:00"
    assert find_day(CAL, "2026-07-04") is None


def test_is_trading_day():
    assert is_trading_day(CAL, "2026-07-02") is True
    assert is_trading_day(CAL, "2026-07-04") is False  # holiday absent


def test_market_closed_on():
    assert market_closed_on(CAL, "2026-07-04") is True
    assert market_closed_on(CAL, "2026-07-06") is False


def test_is_half_day():
    assert is_half_day(find_day(CAL, "2026-07-03")) is True
    assert is_half_day(find_day(CAL, "2026-07-02")) is False
    assert is_half_day(None) is False


def test_is_half_day_missing_close_is_false():
    assert is_half_day({"date": "x"}) is False


async def test_is_trading_today_uses_et_date():
    calendar.invalidate_cache()
    # 2026-07-04 03:00 UTC is still 2026-07-03 in ET (a trading half day).
    fake_now = datetime(2026, 7, 4, 3, 0, tzinfo=timezone.utc)
    with patch.object(AlpacaClient, "calendar", new=AsyncMock(return_value=CAL)) as m:
        client = AlpacaClient(key_id="k", key_secret="s")
        assert await calendar.is_trading_today(client, today=fake_now) is True
    # Queried the ET date, not the UTC date.
    assert m.call_args.args[0] == "2026-07-03"
    calendar.invalidate_cache()


async def test_is_trading_today_false_on_holiday():
    calendar.invalidate_cache()
    fake_now = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)  # July 4 ET, absent
    with patch.object(AlpacaClient, "calendar", new=AsyncMock(return_value=CAL)):
        client = AlpacaClient(key_id="k", key_secret="s")
        assert await calendar.is_trading_today(client, today=fake_now) is False
    calendar.invalidate_cache()


async def test_get_calendar_caches():
    calendar.invalidate_cache()
    with patch.object(AlpacaClient, "calendar", new=AsyncMock(return_value=CAL)) as m:
        client = AlpacaClient(key_id="k", key_secret="s")
        await calendar.get_calendar(client, "2026-07-01", "2026-07-07")
        await calendar.get_calendar(client, "2026-07-01", "2026-07-07")
    assert m.call_count == 1  # second call served from cache
    calendar.invalidate_cache()
