"""The engine watchdog: when is a stale heartbeat actually a stall?"""

from datetime import datetime, timedelta, timezone

from qt.services import watchdog

NOW = datetime(2026, 8, 2, 3, 0, tzinfo=timezone.utc)  # 3am Sunday: crypto trades, stocks don't


def test_a_crypto_book_is_watched_while_the_stock_market_is_shut():
    """Crypto trades 24/7, so a stale heartbeat at 3am Sunday is a real stall.
    Gating purely on US market hours left the watchdog blind for about three
    quarters of the time crypto actually trades — a crypto engine could die on
    Friday evening and go unreported until Monday."""
    assert watchdog.should_alert(
        mode="paper",
        market_open=False,
        crypto_active=True,
        last_tick_at=NOW - timedelta(minutes=30),
        now=NOW,
        threshold=timedelta(minutes=10),
        already_alerted=False,
    )


def test_a_stocks_only_book_stays_quiet_out_of_hours():
    """The other direction. Without this, silencing the market-hours gate
    entirely would also pass — and every stocks-only user would be paged
    overnight for an engine that is idle on purpose."""
    assert not watchdog.should_alert(
        mode="paper",
        market_open=False,
        crypto_active=False,
        last_tick_at=NOW - timedelta(minutes=30),
        now=NOW,
        threshold=timedelta(minutes=10),
        already_alerted=False,
    )
