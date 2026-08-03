"""One-minute bars for the fidelity replay.

THE FAULT. The live engine evaluates every 60 seconds; the replay's finest bar
was 15 minutes. Measured on the owner's instance (strategy 26, 13:14–14:02 UTC,
after every other known fault was fixed): 4 matched, 2 "the replay missed it",
2 "the replay invented it", 50%. FIL was bought live at 13:18:18 — BETWEEN the
replay's 13:15 and 13:30 bars — so a signal that appeared and vanished inside
that quarter hour could not be seen at all. And the two "invented" rows were
CONSEQUENCES: the missed entry left the replay a position slot spare, so it
bought something the live engine (at its cap) could not. One blind spot, two
false verdicts pointing opposite ways.

WHAT IS PINNED HERE, claim by claim:

  * a short fidelity window asks for minute bars, a longer one does not, and an
    ordinary backtest cannot ask for them at all;
  * minute bars are cached in their OWN tables, because a 1-minute and a
    15-minute bar collide on (symbol, timestamp) four times an hour;
  * the fetch that fills a window fetches at the resolution asked for, into the
    matching table, under a volume budget the 15-minute caps cannot express;
  * a finer resolution NEVER means less coverage — the replay drops back to
    15-minute bars when minute ones would cover less, and the report says which
    it used;
  * and the whole point: at one minute the replay sees an entry that fifteen
    minutes structurally cannot.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.api.backtest import (
    MAX_HOURS_FOR_MINUTE_REPLAY,
    MINUTE_FILL_MAX_SYMBOL_DAYS,
    BacktestBody,
    ensure_replay_intraday,
    fetch_replay_window_intraday,
    intraday_model_for,
    load_scanner_replay_dataset,
)
from qt.api.fidelity import _bar_floor, _timeframe_for
from qt.broker.alpaca import AlpacaClient
from qt.services import barcache
from qt.services.backtest import run_backtest
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc

PLAIN = {
    "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False},
    "exit": {"stop_loss_pct": 50, "trailing_stop_pct": 0, "take_profit_pct": 0},
}


def _bar(ts: str, close: float, high: float | None = None) -> dict:
    return {
        "t": ts, "o": close, "h": high if high is not None else close,
        "l": close, "c": close, "v": 1e5, "vw": close,
    }


@pytest.fixture()
def cache(monkeypatch):
    """An empty in-memory bar cache wired in as the global one."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    return Sess


# ---------------------------------------------------------------------------
# 1. WHICH RESOLUTION a comparison asks for
# ---------------------------------------------------------------------------


def test_a_window_of_hours_asks_for_minute_bars():
    """The reported window was 48 minutes long. At 15-minute bars the replay had
    three decision points to reproduce a 60-second engine's."""
    assert _timeframe_for(PLAIN, window_hours=0.8) == "1Min"


def test_a_window_past_the_cap_stays_on_fifteen_minute_bars():
    """Minute bars are bought with download volume — 1,440 a day per crypto pair.
    Past a day the comparison is asking about behaviour in general, which
    15-minute bars answer, so the cost stops being worth paying."""
    assert _timeframe_for(PLAIN, window_hours=MAX_HOURS_FOR_MINUTE_REPLAY + 1) == "15Min"
    assert _timeframe_for(PLAIN, window_hours=MAX_HOURS_FOR_MINUTE_REPLAY) == "1Min"


def test_an_ordinary_backtest_cannot_ask_for_minute_bars_over_a_long_window():
    """Forty crypto names over 180 days is ten million minute bars. The endpoint
    accepts 1Min so the fidelity path needs no second implementation of a
    backtest — the length guard is what stops it being a way to download that."""
    start = datetime(2026, 8, 1, tzinfo=UTC)
    ok = BacktestBody(
        strategy_id=1, timeframe="1Min",
        window_start=start, window_end=start + timedelta(hours=MAX_HOURS_FOR_MINUTE_REPLAY),
    )
    assert ok.timeframe == "1Min"
    with pytest.raises(ValueError, match="1-minute replay"):
        BacktestBody(
            strategy_id=1, timeframe="1Min",
            window_start=start,
            window_end=start + timedelta(hours=MAX_HOURS_FOR_MINUTE_REPLAY + 1),
        )


def test_the_hold_back_is_floored_to_the_minute_not_the_quarter_hour():
    """The replay may not start before the engine did. The floor is the start of
    the bar CONTAINING go-live, so at 15-minute bars a strategy switched on at
    13:18 legitimately replays from 13:15 — but doing that while replaying
    1-minute bars hands the backtest three minutes the engine never had, and
    whatever it buys in them is reported as a trade it invented."""
    live = datetime(2026, 8, 3, 13, 18, 18, tzinfo=UTC)
    assert _bar_floor(live, "1Min") == datetime(2026, 8, 3, 13, 18, tzinfo=UTC)
    assert _bar_floor(live, "15Min") == datetime(2026, 8, 3, 13, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 2. THE CACHE — minute bars cannot share the intraday tables
# ---------------------------------------------------------------------------


def test_the_two_resolutions_do_not_collide_in_the_cache(cache):
    """A 15-minute bar and a 1-minute bar share a timestamp four times an hour.
    The intraday tables are keyed (symbol, ts) with no record of the bar's size
    and written insert-or-ignore, so in one table the minute bar would simply be
    dropped — and the survivor served back inside a minute series, carrying the
    high and low of a whole quarter hour. Stops are checked against those
    extremes, so the replay would fire on a range that never happened in that
    minute."""
    fifteen = intraday_model_for("crypto", "15Min")
    minute = intraday_model_for("crypto", "1Min")
    assert fifteen is not minute, "one table cannot hold both — see MinuteBar"

    with cache() as s:
        # Same symbol, same stamp, deliberately different highs.
        barcache.save_intraday_bars(
            s, "ADA/USD", [_bar("2026-08-03T13:15:00Z", 1.0, high=1.4)], model=fifteen
        )
        barcache.save_intraday_bars(
            s, "ADA/USD", [_bar("2026-08-03T13:15:00Z", 1.0, high=1.01)], model=minute
        )
        s.commit()
        coarse = barcache.cached_intraday_bars(s, ["ADA/USD"], "2026-08-03", model=fifteen)
        fine = barcache.cached_intraday_bars(s, ["ADA/USD"], "2026-08-03", model=minute)

    assert coarse["ADA/USD"][0]["h"] == 1.4
    assert fine["ADA/USD"][0]["h"] == 1.01


def test_a_days_worth_of_minute_bars_can_be_saved_in_one_call(cache):
    """Found by this feature rather than invented for it. A bar row binds eight
    parameters and both engines cap the parameters in ONE statement (SQLite
    32,766, Postgres 65,535), so the single unbounded VALUES list the cache had
    always used was fine only because a save was one day of 15-minute bars — 96
    rows. A minute fill covers a contiguous RUN of days in one call: four days of
    one crypto pair is 5,760 rows and 46,080 parameters, which SQLite refuses
    outright and eight days would put past Postgres too."""
    # Four days — the span a minute fill covers in ONE call for a crypto pair,
    # baseline prefix included. 5,760 rows is 46,080 bound parameters, past
    # SQLite's 32,766 limit; the fixture has to be that big or the mutation
    # "stop chunking" survives and this test proves nothing.
    bars = [
        _bar(f"2026-08-{day:02d}T{n // 60:02d}:{n % 60:02d}:00Z", 1.0 + n / 1000)
        for day in (1, 2, 3, 4)
        for n in range(1440)
    ]
    assert len(bars) * 8 > 32_766, "the fixture must exceed SQLite's parameter limit"
    with cache() as s:
        saved = barcache.save_intraday_bars(
            s, "ADA/USD", bars, model=barcache.CryptoMinuteBar
        )
        s.commit()
        assert saved == len(bars)
        assert s.query(barcache.CryptoMinuteBar).count() == len(bars)


def test_the_stock_and_crypto_minute_tables_are_also_separate():
    """Same reason the intraday ones are: a crypto 'day' is the UTC calendar day
    and a stock one the ET session day, so mixing them makes every day-keyed
    query mean two different things at once."""
    assert intraday_model_for("stock", "1Min") is not intraday_model_for("crypto", "1Min")


def test_minute_bars_are_kept_for_far_less_time_than_fifteen_minute_ones(cache, monkeypatch):
    """They are fifteen rows where a 15-minute bar is one, and they are fetched
    for exactly one purpose — grading a window that is hours long and days old.
    Keeping them for the full 730 days would pay 15x the disk on the owner's NAS
    for history no code path reads."""
    monkeypatch.delenv("QT_BAR_CACHE_KEEP_DAYS", raising=False)
    monkeypatch.delenv("QT_BAR_CACHE_MINUTE_KEEP_DAYS", raising=False)
    assert barcache.minute_keep_days() < barcache.intraday_keep_days()

    now = datetime.now(UTC)

    def stamp(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # A day INSIDE the 15-minute window and OUTSIDE the minute one: the whole
    # point of two windows is that this bar goes while its 15-minute twin stays.
    old = stamp(barcache.MINUTE_KEEP_DAYS + 1)
    with cache() as s:
        s.add(barcache.IntradayBar(symbol="AAA", ts=old, o=1, h=1, l=1, c=1, v=1, vw=1))
        s.add(barcache.MinuteBar(symbol="AAA", ts=old, o=1, h=1, l=1, c=1, v=1, vw=1))
        s.add(barcache.CryptoMinuteBar(symbol="ADA/USD", ts=old, o=1, h=1, l=1, c=1, v=1, vw=1))
        s.commit()
        out = barcache.prune_intraday(s)

        assert out["stock_minute"] == 1
        assert out["crypto_minute"] == 1
        assert out["stock"] == 0, "the 15-minute bar is still well inside its own window"
        assert s.query(barcache.MinuteBar).count() == 0
        assert s.query(barcache.IntradayBar).count() == 1


# ---------------------------------------------------------------------------
# 3. READING and FILLING at the finer resolution
# ---------------------------------------------------------------------------


def _seed_days(sess, symbol: str, days: list[str], *, minute: bool, fifteen: bool) -> None:
    """Daily bars for `days`, plus intraday coverage at the chosen resolutions.
    The prices differ per resolution so a test can tell which one it was served."""
    barcache.save_daily_bars(
        sess, symbol, [_bar(f"{d}T00:00:00Z", 1.0) for d in days],
        model=barcache.CryptoDailyBar,
    )
    if fifteen:
        barcache.save_intraday_bars(
            sess, symbol,
            [_bar(f"{d}T{(n * 15) // 60:02d}:{(n * 15) % 60:02d}:00Z", 15.0)
             for d in days for n in range(96)],
            model=barcache.CryptoIntradayBar,
        )
    if minute:
        barcache.save_intraday_bars(
            sess, symbol,
            [_bar(f"{d}T{n // 60:02d}:{n % 60:02d}:00Z", 1.0)
             for d in days for n in range(1440)],
            model=barcache.CryptoMinuteBar,
        )


def test_the_dataset_reads_the_minute_table_when_asked_for_minute_bars(cache):
    """And only that table. Both resolutions are cached here with deliberately
    different closes, so a dataset serving the wrong one is visible rather than
    merely mislabelled."""
    days = ["2026-07-31", "2026-08-01", "2026-08-02"]
    with cache() as s:
        _seed_days(s, "ADA/USD", days, minute=True, fifteen=True)
        barcache.store_movers(s, "2026-08-02", [("ADA/USD", 5.0, 1.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    end = datetime(2026, 8, 2, 23, 0, tzinfo=UTC)
    fine = load_scanner_replay_dataset(
        "crypto", 1, 10, None, end=end, window_hours=3.0, allow_empty_intraday=True,
        intraday_timeframe="1Min",
    )
    assert fine.timeframe == "1Min"
    assert {b["c"] for b in fine.bars["ADA/USD"]} == {1.0}

    coarse = load_scanner_replay_dataset(
        "crypto", 1, 10, None, end=end, window_hours=3.0, allow_empty_intraday=True,
    )
    assert coarse.timeframe == "15Min"
    assert {b["c"] for b in coarse.bars["ADA/USD"]} == {15.0}


def _fetch(ds, timeframe: str):
    """Run the window fill against a recording fake broker."""
    calls: list[tuple] = []

    async def fake_bars(self, symbols, asset_class, tf, start, end=None):
        calls.append((tuple(symbols), tf, start, end))
        return {s: [_bar(f"{start}T00:01:00Z", 1.0)] for s in symbols}

    with patch.object(AlpacaClient, "historical_bars", new=fake_bars):
        saved = asyncio.run(
            fetch_replay_window_intraday(
                AlpacaClient.__new__(AlpacaClient), ds,
                asset_class="crypto", timeframe=timeframe,
            )
        )
    return calls, saved


def test_the_window_fill_asks_the_broker_for_the_resolution_being_replayed(cache):
    """Fetching 15-minute bars for a minute replay would fill a table the dataset
    is not reading, and the reload would find exactly as little as before."""
    days = ["2026-07-31", "2026-08-01", "2026-08-02"]
    with cache() as s:
        _seed_days(s, "ADA/USD", days, minute=False, fifteen=False)
        barcache.store_movers(s, "2026-08-02", [("ADA/USD", 5.0, 1.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    ds = load_scanner_replay_dataset(
        "crypto", 1, 10, None, end=datetime(2026, 8, 2, 23, 0, tzinfo=UTC),
        window_hours=3.0, allow_empty_intraday=True, intraday_timeframe="1Min",
    )
    calls, saved = _fetch(ds, "1Min")
    assert calls, "nothing was fetched at all"
    assert {c[1] for c in calls} == {"1Min"}
    assert saved > 0
    # …and it landed in the MINUTE table, not the 15-minute one.
    with cache() as s:
        assert s.query(barcache.CryptoMinuteBar).count() == saved
        assert s.query(barcache.CryptoIntradayBar).count() == 0


def test_the_minute_fill_stands_down_over_its_volume_budget(cache):
    """The request caps bound how many CALLS are made and say nothing about how
    much comes back. At one minute a call returns fifteen times as much, so the
    same number of requests is ~170,000 bars rather than ~11,000 — and that is
    the number that lands in the owner's Postgres. The minute pass is therefore
    budgeted in symbol-days as well.

    The fixture must clear the REQUEST cap comfortably while breaking the volume
    budget, or it is the old cap being tested and the budget could be deleted
    with this still green: each name is missing 3 window days in one contiguous
    run, so it costs one request and three symbol-days."""
    from qt.api.backtest import REPLAY_FILL_MAX_REQUESTS

    days = ["2026-07-31", "2026-08-01", "2026-08-02"]
    many = [f"BULK{n}/USD" for n in range(MINUTE_FILL_MAX_SYMBOL_DAYS // len(days) + 1)]
    assert len(many) <= REPLAY_FILL_MAX_REQUESTS, "the request cap must not be what stops this"
    assert len(many) * len(days) > MINUTE_FILL_MAX_SYMBOL_DAYS, "…but the budget must be"
    with cache() as s:
        for symbol in many:
            _seed_days(s, symbol, days, minute=False, fifteen=False)
        barcache.store_movers(
            s, "2026-08-02", [(symbol, 5.0, 1.0, 1e8) for symbol in many],
            model=barcache.CryptoDailyMover,
        )
        s.commit()

    ds = load_scanner_replay_dataset(
        "crypto", 1, len(many), None, end=datetime(2026, 8, 2, 23, 0, tzinfo=UTC),
        window_hours=3.0, allow_empty_intraday=True, intraday_timeframe="1Min",
    )
    calls, saved = _fetch(ds, "1Min")
    assert calls == []
    assert saved == 0


def test_the_mover_sweep_is_not_run_for_a_minute_replay(cache):
    """The sweep fetches 15-minute bars and nothing else. Running it here would
    spend a long download filling a table this dataset does not read."""
    from qt.services import barsweep

    days = ["2026-07-31", "2026-08-01", "2026-08-02"]
    with cache() as s:
        _seed_days(s, "ADA/USD", days, minute=False, fifteen=False)
        barcache.store_movers(s, "2026-08-02", [("ADA/USD", 5.0, 1.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    end = datetime(2026, 8, 2, 23, 0, tzinfo=UTC)

    def run(timeframe: str) -> bool:
        swept: list[str] = []

        async def fake_sweep(client, sess, **kw):
            swept.append(timeframe)
            return {}

        ds = load_scanner_replay_dataset(
            "crypto", 1, 10, None, end=end, window_hours=3.0,
            allow_empty_intraday=True, intraday_timeframe=timeframe,
        )
        with patch.object(barsweep, "sweep_crypto_intraday", new=fake_sweep), \
                patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})):
            asyncio.run(
                ensure_replay_intraday(
                    AlpacaClient.__new__(AlpacaClient), ds, PLAIN,
                    asset_class="crypto", days=1, replay_top_n=10, scanner_cfg=None,
                    end=end, window_hours=3.0, allow_empty_intraday=True,
                    timeframe=timeframe,
                )
            )
        return bool(swept)

    assert run("1Min") is False
    # The guard: at 15 minutes the sweep DOES run, so the assertion above is
    # about the resolution rather than about the fixture having nothing to do.
    assert run("15Min") is True


# ---------------------------------------------------------------------------
# 4. THE POINT — a decision fifteen-minute bars structurally cannot resolve
# ---------------------------------------------------------------------------


def _series(step_minutes: int, spike: set[int]) -> list[dict]:
    """A flat 100 for 25 hours, with a brief spike to 104 at the named minutes of
    2026-08-02. Returned at whatever bar size `step_minutes` asks for, built the
    way a real aggregation works: a coarse bar CLOSES at the price prevailing at
    the end of its slot, and carries the slot's high.

    So the spike is inside the body of the 13:15 quarter-hour bar: its high shows
    it, its close does not — and a day-gain is measured on the close."""
    start = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 8, 2, 14, 2, tzinfo=UTC)
    out: list[dict] = []
    cursor = start
    while cursor < end:
        slot = [
            cursor + timedelta(minutes=n)
            for n in range(step_minutes)
            if cursor + timedelta(minutes=n) < end
        ]

        def price(at: datetime) -> float:
            spiking = at.date() == datetime(2026, 8, 2).date() and (at.hour * 60 + at.minute) in spike
            return 104.0 if spiking else 100.0

        closes = [price(t) for t in slot]
        out.append(
            {
                "t": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": closes[0], "h": max(closes), "l": min(closes), "c": closes[-1],
                "v": 1e5, "vw": closes[-1],
            }
        )
        cursor += timedelta(minutes=step_minutes)
    return out


def _replay(bars: list[dict]) -> dict:
    strategy = {
        "asset_class": "crypto", "swing_mode": True, "sizing_usd": 100.0,
        "sleeve_usd": 1000.0, "max_positions": 5, "params": PLAIN,
    }
    return run_backtest(
        strategy, {"FIL/USD": bars}, dict(RISK_DEFAULTS),
        starting_cash=1000, spread_pct=0, market="crypto",
        sim_start=datetime(2026, 8, 2, 13, 14, tzinfo=UTC),
        sim_end=datetime(2026, 8, 2, 14, 2, tzinfo=UTC),
    )


# 13:16, 13:17, 13:18 — the reported FIL buy was at 13:18:18, inside the
# replay's 13:15 bar and gone before its 13:30 one.
SPIKE = {13 * 60 + 16, 13 * 60 + 17, 13 * 60 + 18}


def test_fifteen_minute_bars_cannot_see_a_signal_that_lived_three_minutes():
    """Not a bug in the backtester — a property of the resolution. This is the
    control: if it ever starts finding the trade, the test below proves nothing."""
    result = _replay(_series(15, SPIKE))
    assert result["diagnosis"]["bars_evaluated"] > 0, "the window must be evaluated at all"
    assert result["trades"] + len(result["open_positions"]) == 0


def test_minute_bars_find_the_entry_the_live_engine_made():
    """The whole feature. Same prices, same rules, same window — only the bar
    size differs, and at 60 seconds the replay reproduces the buy that at 15
    minutes came back as "the backtest missed this trade"."""
    result = _replay(_series(1, SPIKE))
    entries = (result["trade_list"] or []) + (result["open_positions"] or [])
    assert len(entries) == 1, result["diagnosis"]["summary"]
    assert entries[0]["symbol"] == "FIL/USD"


# ---------------------------------------------------------------------------
# 5. END TO END, through /api/fidelity/compare — and the report says which
#    resolution graded the trades
# ---------------------------------------------------------------------------


@pytest.fixture()
def configured(client):
    """Defined here rather than imported: CI runs pytest from the repo root,
    where `tests` is not an importable package."""
    from qt import security
    from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
    from qt.db import session_scope
    from qt.models import Strategy, StrategyConfigVersion, Trade

    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def _compare(client, cache, *, minute: tuple[str, ...] = (), fifteen: tuple[str, ...] = (),
             calls: list | None = None) -> dict:
    """A three-hour crypto comparison over a window whose cache holds minute bars
    for the names in `minute` and 15-minute bars for the names in `fifteen` (a
    name may be in both, or in neither). Every name is a cached mover with daily
    bars, so what varies between tests is intraday COVERAGE and nothing else.

    Returns the report. `calls`, if given, collects every bar request the replay
    made — which is how "it did not even look at the coarser cache" is testable.
    """
    from qt.db import session_scope
    from qt.models import Strategy, Trade

    names = sorted(set(minute) | set(fifteen)) or ["TICK/USD"]
    now = datetime.now(UTC)
    # Pinned to YESTERDAY so every day the window touches has a completed daily
    # bar; the still-forming day is somebody else's test.
    end = (now - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=3)
    days = [(end - timedelta(days=n)).strftime("%Y-%m-%d") for n in (3, 2, 1, 0)]

    with cache() as s:
        for name in names:
            _seed_days(s, name, days, minute=name in minute, fifteen=name in fifteen)
        barcache.store_movers(
            s, days[-1], [(name, 9.0, 1.0, 1e8) for name in names],
            model=barcache.CryptoDailyMover,
        )
        s.commit()

    sid = client.post("/api/strategies", json={
        "name": "minute crypto", "asset_class": "crypto", "universe": "scanner",
        "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 0.2, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 100, "sleeve_usd": 1000, "max_positions": 5,
        "swing_mode": True, "ignore_regime": True,
    }).json()["id"]
    with session_scope() as s:
        s.get(Strategy, sid).enabled_at = start - timedelta(hours=1)
        for name in names:
            s.add(Trade(
                strategy_id=sid, mode="paper", symbol=name, asset_class="crypto",
                status="open", qty=10, notional=100, entry_price=1.0,
                entry_reason="gain", entry_at=start + timedelta(minutes=30),
            ))

    async def record(self, syms, asset_class, tf, first, last=None):
        if calls is not None:
            calls.append((tuple(syms), tf))
        return {}

    with patch.object(AlpacaClient, "historical_bars", new=record):
        response = client.post("/api/fidelity/compare", json={
            "strategy_id": sid, "mode": "paper",
            "window_start": start.isoformat(), "window_end": end.isoformat(),
        })
    assert response.status_code == 200, response.text
    return response.json()


ONE = ("TICK/USD",)
TWO = ("TICK/USD", "TOCK/USD")


def test_the_report_names_minute_bars_when_it_used_them(client, configured, cache):
    """The user has to be able to see what graded their trades — a 50% match rate
    means something quite different at 15 minutes than at 60 seconds."""
    assert _compare(client, cache, minute=ONE)["timeframe"] == "1Min"


def test_a_cache_with_no_minute_bars_falls_back_rather_than_refusing(client, configured, cache):
    """A FINER RESOLUTION MUST NEVER MEAN LESS COVERAGE. The minute tables start
    empty on every instance, and for stocks the free IEX feed is thin and a mover
    union is large — so the minute pass can quite legitimately come back with
    nothing while the 15-minute cache is complete. Refusing there would turn a
    comparison that worked yesterday into a 422 today, which is the opposite of
    the improvement."""
    assert _compare(client, cache, fifteen=ONE)["timeframe"] == "15Min"


def test_a_complete_minute_cache_is_not_second_guessed(client, configured, cache):
    """The fallback costs a second read of the whole dataset and a second fetch
    pass. Full minute coverage cannot be beaten, so the case this feature exists
    for must not pay for the case it does not: no 15-minute request is made at
    all."""
    calls: list[tuple] = []
    report = _compare(client, cache, minute=ONE, calls=calls)
    assert report["timeframe"] == "1Min"
    assert [c for c in calls if c[1] == "15Min"] == [], calls


def test_a_tie_on_coverage_goes_to_the_finer_bars(client, configured, cache):
    """Partial coverage each way — minute bars for one name, 15-minute bars for
    the other — so both datasets replay exactly one symbol of two. Neither is
    better, and the tie has to go to the finer bars: they resolve decisions the
    coarser ones cannot, and preferring the coarse one on a draw would give back
    the whole point of the feature for nothing."""
    report = _compare(client, cache, minute=ONE, fifteen=("TOCK/USD",))
    assert report["timeframe"] == "1Min"
