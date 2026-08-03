"""The last two faults under "the replay missed six crypto buys".

Measured on the running instance: timeframe 15Min (correct), replayed_symbols
["ADA/USD","BTC/USD","ETH/USD"], live_trades 6, backtest_trades 0, and
"the replay could not evaluate a single bar in this window". The engine had
bought ADA, XRP, SOL, ETH, AAVE and PAXG between 23:18 on 2026-08-02 and 03:22
on 2026-08-03. Two separate things were wrong.

FAULT 1 — the replay's universe is a different set from the live one, by
construction. It comes from the cached DAILY movers (top-N by a whole day's
close-to-close move, re-filtered with TODAY'S scanner settings), while the live
crypto scanner ranks a ROLLING 24-hour figure recomputed every cycle under the
settings of the time. Four of the six names were simply not in it.

FAULT 2 — the two names that WERE in it evaluated zero bars. The cache reads
intraday bars from the window's own first day, so a crypto bar has no close
24 hours behind it to measure a day-gain against, and `_simulate` skips a bar
with change_pct None without a word. And the missing days could never arrive:
the intraday sweep walks `daily_movers` rows, a day only gets a mover row once
its DAILY bar exists, and crypto's daily bar for the current UTC day
deliberately does not exist yet.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.api.backtest import (
    ScannerReplayDataset,
    ensure_replay_intraday,
    fetch_replay_window_intraday,
    load_scanner_replay_dataset,
)
from qt.api.fidelity import _seed_by_day
from qt.broker.alpaca import AlpacaClient
from qt.services import barcache
from qt.services.backtest import run_backtest
from qt.services.engine import RISK_DEFAULTS
from qt.services.fidelity import compare

UTC = timezone.utc

# The reported window, to the minute.
WINDOW_START = datetime(2026, 8, 2, 23, 18, 18, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 3, 3, 22, tzinfo=UTC)
WINDOW_HOURS = (WINDOW_END - WINDOW_START).total_seconds() / 3600
START_DAY = "2026-08-02"
BASELINE_DAY = "2026-07-31"  # start_day minus BASELINE_WARMUP_DAYS["crypto"]

STRATEGY = {
    "asset_class": "crypto",
    "swing_mode": False,
    "sizing_usd": 100.0,
    "sleeve_usd": 1000.0,
    "max_positions": 5,
    # Strategy 18's shape: a low gain gate and a stop, no daily indicators.
    "params": {
        "entry": {"min_day_gain_pct": 0.2, "require_above_vwap": False},
        "exit": {"stop_loss_pct": 4, "trailing_stop_pct": 0, "take_profit_pct": 0},
    },
}


def _bar(ts: str, close: float) -> dict:
    return {"t": ts, "o": close, "h": close, "l": close, "c": close, "v": 1e5, "vw": close}


def _fifteens(day: str, first_close: float, step: float = 0.05) -> list[dict]:
    """A whole UTC day of 15-minute bars, drifting upwards."""
    return [
        _bar(f"{day}T{(n * 15) // 60:02d}:{(n * 15) % 60:02d}:00Z", first_close + n * step)
        for n in range(96)
    ]


@pytest.fixture()
def cache(monkeypatch):
    """The bar cache exactly as the live instance held it.

    Daily bars and movers stop at 2026-08-02 — crypto's 08-03 daily bar is still
    forming, and sweep_crypto_daily_bars refuses to store a partial one. Intraday
    covers 08-01 and 08-02 for the two names that made the movers list, and
    nothing at all for SOL/USD, which the live scanner had and the reconstruction
    did not.
    """
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    daily_days = ["2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02"]
    with Sess() as s:
        for symbol, base in (("ADA/USD", 1.0), ("ETH/USD", 3000.0), ("SOL/USD", 150.0)):
            barcache.save_daily_bars(
                s,
                symbol,
                [_bar(f"{d}T00:00:00Z", base * (1 + 0.01 * n)) for n, d in enumerate(daily_days)],
                model=barcache.CryptoDailyBar,
            )
        for symbol, base in (("ADA/USD", 1.0), ("ETH/USD", 3000.0)):
            barcache.save_intraday_bars(
                s,
                symbol,
                _fifteens("2026-08-01", base, base * 0.0005)
                + _fifteens("2026-08-02", base * 1.05, base * 0.0005),
                model=barcache.CryptoIntradayBar,
            )
        barcache.store_movers(
            s,
            START_DAY,
            [("ADA/USD", 5.0, 1.05, 1e8), ("ETH/USD", 4.0, 3150.0, 1e8)],
            model=barcache.CryptoDailyMover,
        )
        s.commit()
    return Sess


def _dataset(**over) -> ScannerReplayDataset:
    kwargs = dict(
        end=WINDOW_END, window_hours=WINDOW_HOURS, allow_empty_intraday=True
    )
    kwargs.update(over)
    return load_scanner_replay_dataset("crypto", 1, 10, None, **kwargs)


def _replay(ds: ScannerReplayDataset) -> dict:
    """The replay exactly as _scanner_replay drives it for this window."""
    return run_backtest(
        STRATEGY, ds.bars, dict(RISK_DEFAULTS),
        starting_cash=1000, spread_pct=0, market="crypto",
        eligible_by_day=ds.eligible_by_day,
        sim_start=WINDOW_START, sim_end=WINDOW_END,
    )


# ---------------------------------------------------------------------------
# FAULT 2a — the intraday series had no day-gain baseline before the window
# ---------------------------------------------------------------------------


def test_the_intraday_series_reaches_back_before_the_window(cache):
    """A crypto day-gain is measured against the close ~24h back. Reading the
    cache from the window's own first day leaves every bar in its first 24 hours
    with no reference at all."""
    ds = _dataset()
    assert ds.baseline_day == BASELINE_DAY
    assert any(b["t"][:10] < START_DAY for b in ds.bars["ADA/USD"])


def test_the_replay_now_evaluates_the_windows_bars(cache):
    """THE reported symptom: "could not evaluate a single bar". Both in-universe
    names had 15-minute bars sitting inside the window the whole time; without a
    baseline in front of them every one was skipped in silence."""
    result = _replay(_dataset())
    assert result["diagnosis"]["bars_evaluated"] > 0
    assert "could not evaluate a single bar" not in (result["diagnosis"]["summary"] or "")


def test_the_baseline_bars_are_not_tradable(client, configured, monkeypatch):
    """They exist to be a reference price. If they could trade, the tested window
    would silently grow by the baseline days and every reported figure would
    cover a period nobody asked for.

    Driven through the endpoint with a plain `days` request — the one path that
    passes no explicit window, and therefore the only place the trading floor has
    to be derived rather than handed over."""
    from unittest.mock import AsyncMock

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(UTC)
    days = [(now - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(9, 0, -1)]
    start_day = days[2]  # `days: 7` counts back seven days from now
    with Sess() as s:
        barcache.save_daily_bars(
            s, "FLOOR/USD",
            [_bar(f"{d}T00:00:00Z", 1.0 + 0.05 * n) for n, d in enumerate(days)],
            model=barcache.CryptoDailyBar,
        )
        bars: list[dict] = []
        for n, d in enumerate(days):
            bars += _fifteens(d, 1.0 + 0.05 * n, 0.001)
        barcache.save_intraday_bars(s, "FLOOR/USD", bars, model=barcache.CryptoIntradayBar)
        for d in days:
            barcache.store_movers(s, d, [("FLOOR/USD", 5.0, 1.2, 1e8)],
                                  model=barcache.CryptoDailyMover)
        s.commit()

    made = client.post("/api/strategies", json={
        "name": "floor crypto", "asset_class": "crypto", "universe": "scanner",
        "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 0.2, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 50, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 100, "sleeve_usd": 1000, "max_positions": 5,
        "swing_mode": True, "ignore_regime": True,
    })
    assert made.status_code in (200, 201), made.text
    sid = made.json()["id"]

    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})):
        response = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 7,
            "starting_cash": 1000, "spread_pct": 0,
        })
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["replay_intraday"] is True
    assert body["equity_days"], "the replay produced no days at all"
    assert min(body["equity_days"]) >= start_day, body["equity_days"]
    entered = [
        t["entry_day"] for t in body["trade_list"] + body["open_positions"]
    ]
    assert entered, "nothing was bought, so the floor is untested"
    assert all(day >= start_day for day in entered), entered


def test_coverage_still_means_covered_inside_the_window(cache):
    """A symbol whose only intraday bars sit in the baseline prefix has nothing
    to replay. Counting it would flip a short window to intraday on the strength
    of bars that can never trade, and inflate universe_size with a name that was
    never tested."""
    with cache() as s:
        barcache.save_intraday_bars(
            s, "XRP/USD", _fifteens("2026-08-01", 0.5, 0.0002),
            model=barcache.CryptoIntradayBar,
        )
        barcache.save_daily_bars(
            s, "XRP/USD", [_bar("2026-08-02T00:00:00Z", 0.5)],
            model=barcache.CryptoDailyBar,
        )
        barcache.store_movers(
            s, START_DAY,
            [("ADA/USD", 5.0, 1.05, 1e8), ("ETH/USD", 4.0, 3150.0, 1e8),
             ("XRP/USD", 3.0, 0.5, 1e8)],
            model=barcache.CryptoDailyMover,
        )
        s.commit()
    ds = _dataset()
    assert "XRP/USD" in ds.union
    assert "XRP/USD" not in ds.replayed
    assert "XRP/USD" in ds.dropped
    assert ds.intraday_covered == 2


# ---------------------------------------------------------------------------
# FAULT 2b — the missing days could never be fetched
# ---------------------------------------------------------------------------


def _fetch(ds, returns=None):
    """Run the window fill against a recording fake broker."""
    calls: list[tuple] = []

    async def fake_bars(self, symbols, asset_class, timeframe, start, end=None):
        calls.append((tuple(symbols), timeframe, start, end))
        return {s: (returns or []) for s in symbols}

    with patch.object(AlpacaClient, "historical_bars", new=fake_bars):
        saved = asyncio.run(
            fetch_replay_window_intraday(
                AlpacaClient.__new__(AlpacaClient), ds, asset_class="crypto"
            )
        )
    return calls, saved


def test_the_window_fill_asks_for_the_day_the_sweep_cannot_reach(cache):
    """08-03 has no daily bar (crypto's current UTC day is still forming) and so
    has no mover row, and the intraday sweep only ever walks mover rows. The
    replay's own fill has to go and get it, or the window's last four hours are
    permanently invisible."""
    calls, _ = _fetch(_dataset())
    asked = {(c[0][0], c[2]) for c in calls}
    assert ("ADA/USD", "2026-08-03") in asked
    assert ("ETH/USD", "2026-08-03") in asked


def test_it_does_not_re_request_days_the_cache_already_has(cache):
    """08-01 and 08-02 are covered. A single min-to-max span would re-download
    both of them on every run, because the gaps sit either side of them."""
    calls, _ = _fetch(_dataset())
    spans = [(c[2], c[3]) for c in calls]
    assert spans, "nothing was requested at all"
    for start, end in spans:
        assert not (start <= "2026-08-01" and end > "2026-08-01"), spans


def test_a_name_that_was_never_a_mover_gets_its_whole_baseline_span(cache):
    """A seeded symbol has no intraday bars at all — the sweep had no reason to
    fetch it on any day. Filling only the tail would leave it with bars and no
    baseline, which is the same zero-evaluated-bars failure one level down."""
    ds = _dataset(seed_by_day={START_DAY: ["SOL/USD"]})
    calls, _ = _fetch(ds)
    sol = next(c for c in calls if c[0] == ("SOL/USD",))
    assert sol[2] == BASELINE_DAY


def test_a_day_the_symbol_never_traded_is_not_treated_as_a_gap(cache):
    """Weekends, holidays and a pair that had not listed yet have no bars and
    never will. Reading those as gaps would re-request an empty range for that
    symbol on every single run, forever — so a missing day only counts when the
    DAILY cache says the symbol traded then, or when it is newer than anything
    the sweep has reached.

    HOLI/USD has daily bars either side of 2026-08-01 and none on it."""
    with cache() as s:
        for d in ("2026-07-31", "2026-08-02"):
            barcache.save_daily_bars(s, "HOLI/USD", [_bar(f"{d}T00:00:00Z", 7.0)],
                                     model=barcache.CryptoDailyBar)
        barcache.store_movers(
            s, START_DAY,
            [("ADA/USD", 5.0, 1.05, 1e8), ("HOLI/USD", 3.0, 7.0, 1e8)],
            model=barcache.CryptoDailyMover,
        )
        s.commit()
    calls, _ = _fetch(_dataset())
    spans = [(c[2], c[3]) for c in calls if c[0] == ("HOLI/USD",)]
    assert spans, "HOLI/USD must be fetched at all, or this proves nothing"
    for start, end in spans:
        assert not (start <= "2026-08-01" < end), spans


def test_todays_bars_are_re_requested_however_much_of_today_is_cached(monkeypatch):
    """A window that ends NOW is asking about the last few hours, and those bars
    did not exist when an earlier run fetched today. Treating a partly-fetched
    today as done is how a comparison run twice in an afternoon keeps answering
    with the morning's data — which for this feature is the whole answer."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    past = [(now - timedelta(days=n)).strftime("%Y-%m-%d") for n in (2, 1)]
    with Sess() as s:
        barcache.save_daily_bars(
            s, "FRESH/USD", [_bar(f"{d}T00:00:00Z", 1.0) for d in past],
            model=barcache.CryptoDailyBar,
        )
        # Yesterday in full, and today already partly cached — the state an
        # earlier run of this very fetch leaves behind.
        barcache.save_intraday_bars(
            s, "FRESH/USD",
            _fifteens(past[1], 1.0, 0.001) + _fifteens(today, 1.1, 0.001)[:8],
            model=barcache.CryptoIntradayBar,
        )
        barcache.store_movers(s, past[1], [("FRESH/USD", 5.0, 1.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    ds = load_scanner_replay_dataset(
        "crypto", 1, 10, None, end=now, window_hours=4.0, allow_empty_intraday=True
    )
    calls, _ = _fetch(ds)
    assert any(start <= today <= end for _syms, _tf, start, end in calls), calls


def test_the_fill_stands_down_rather_than_making_hundreds_of_requests(cache):
    """This pass exists for the recent EDGE and a handful of never-swept names.
    A universe that is missing wholesale is an unbuilt cache, and fetching it
    name by name would turn one replay into a sweep nobody asked for — the sweep
    already exists and is the right tool for that."""
    from qt.api.backtest import REPLAY_FILL_MAX_REQUESTS

    many = [f"BULK{n}/USD" for n in range(REPLAY_FILL_MAX_REQUESTS + 5)]
    with cache() as s:
        for symbol in many:
            barcache.save_daily_bars(s, symbol, [_bar(f"{START_DAY}T00:00:00Z", 1.0)],
                                     model=barcache.CryptoDailyBar)
        barcache.store_movers(
            s, START_DAY, [(symbol, 5.0, 1.0, 1e8) for symbol in many],
            model=barcache.CryptoDailyMover,
        )
        s.commit()
    ds = load_scanner_replay_dataset(
        "crypto", 1, 100, None, end=WINDOW_END, window_hours=WINDOW_HOURS,
        allow_empty_intraday=True,
    )
    assert len(ds.union) > REPLAY_FILL_MAX_REQUESTS, "the fixture must exceed the cap"
    calls, saved = _fetch(ds)
    assert calls == []
    assert saved == 0


def test_a_symbol_missing_most_of_a_long_window_is_left_to_the_sweep(monkeypatch):
    """The per-symbol budget, which the request cap does not cover: one symbol
    missing a whole quarter is a single contiguous run, so it is ONE request —
    for a quarter of 15-minute bars. That is a sweep, and the sweep already
    exists, is resumable, and shows progress."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(UTC)
    days = [(now - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(60, 0, -1)]
    with Sess() as s:
        barcache.save_daily_bars(
            s, "LONG/USD", [_bar(f"{d}T00:00:00Z", 1.0) for d in days],
            model=barcache.CryptoDailyBar,
        )
        for d in days:
            barcache.store_movers(s, d, [("LONG/USD", 5.0, 1.0, 1e8)],
                                  model=barcache.CryptoDailyMover)
        s.commit()

    ds = load_scanner_replay_dataset("crypto", 55, 10, None, end=now)
    assert ds.union == ["LONG/USD"], "the fixture must not be caught by the request cap instead"
    calls, saved = _fetch(ds)
    assert calls == []
    assert saved == 0


def test_the_top_up_runs_even_when_the_dataset_already_says_intraday(cache):
    """A short window accepts PARTIAL intraday coverage, so "already intraday"
    can mean one symbol with one bar. Returning early there is exactly how a
    four-hour window with the cache a day behind replayed nothing."""
    ds = _dataset()
    assert ds.used_intraday is True, "the fixture must reproduce the reported state"
    calls: list[tuple] = []

    async def fake_bars(self, symbols, asset_class, timeframe, start, end=None):
        calls.append((tuple(symbols), timeframe, start, end))
        return {s: _fifteens("2026-08-03", 1.1, 0.001) for s in symbols}

    with patch.object(AlpacaClient, "historical_bars", new=fake_bars):
        fresh, topped_up = asyncio.run(
            ensure_replay_intraday(
                AlpacaClient.__new__(AlpacaClient), ds, STRATEGY["params"],
                asset_class="crypto", days=1, replay_top_n=10, scanner_cfg=None,
                end=WINDOW_END, window_hours=WINDOW_HOURS, allow_empty_intraday=True,
            )
        )
    assert calls, "the top-up did not fetch anything"
    assert topped_up is True
    assert any(b["t"][:10] == "2026-08-03" for b in fresh.bars["ADA/USD"])


def test_the_reload_after_a_top_up_keeps_the_windows_length(cache):
    """The reload dropped `window_hours`, so the fresh dataset judged itself by
    the full-coverage rule a LONG window uses, came back as a daily replay, was
    rejected as no improvement — and the bars just downloaded were thrown away.

    Needs coverage that stays partial after the fetch, because full coverage
    passes either rule and would prove nothing: DEAD/USD is a mover the broker
    has no bars for, exactly like a pair delisted mid-window."""
    with cache() as s:
        barcache.save_daily_bars(s, "DEAD/USD", [_bar("2026-08-02T00:00:00Z", 7.0)],
                                 model=barcache.CryptoDailyBar)
        barcache.store_movers(
            s, START_DAY,
            [("ADA/USD", 5.0, 1.05, 1e8), ("ETH/USD", 4.0, 3150.0, 1e8),
             ("DEAD/USD", 3.0, 7.0, 1e8)],
            model=barcache.CryptoDailyMover,
        )
        s.commit()

    async def fake_bars(self, symbols, asset_class, timeframe, start, end=None):
        return {
            s: ([] if s == "DEAD/USD" else _fifteens("2026-08-03", 1.1, 0.001))
            for s in symbols
        }

    with patch.object(AlpacaClient, "historical_bars", new=fake_bars):
        fresh, topped_up = asyncio.run(
            ensure_replay_intraday(
                AlpacaClient.__new__(AlpacaClient), _dataset(), STRATEGY["params"],
                asset_class="crypto", days=1, replay_top_n=10, scanner_cfg=None,
                end=WINDOW_END, window_hours=WINDOW_HOURS, allow_empty_intraday=True,
            )
        )
    assert "DEAD/USD" in fresh.dropped, "coverage must still be partial, or this proves nothing"
    assert fresh.used_intraday is True
    assert fresh.timeframe == "15Min"
    assert topped_up is True
    assert any(b["t"][:10] == "2026-08-03" for b in fresh.bars["ADA/USD"])


def test_the_reload_after_a_top_up_keeps_the_seeded_names(cache):
    """The top-up re-reads the dataset from scratch. Forget the seeds there and a
    comparison that needed both — a short window whose last hours were missing,
    and names the movers list never had — loses the seeded names at the moment it
    finally has the bars for them. Which is the reported case exactly."""
    async def fake_bars(self, symbols, asset_class, timeframe, start, end=None):
        return {s: _fifteens("2026-08-03", 1.1, 0.001) for s in symbols}

    ds = _dataset(seed_by_day={START_DAY: ["SOL/USD"]})
    assert ds.seeded == ["SOL/USD"]
    with patch.object(AlpacaClient, "historical_bars", new=fake_bars):
        fresh, topped_up = asyncio.run(
            ensure_replay_intraday(
                AlpacaClient.__new__(AlpacaClient), ds, STRATEGY["params"],
                asset_class="crypto", days=1, replay_top_n=10, scanner_cfg=None,
                end=WINDOW_END, window_hours=WINDOW_HOURS,
                seed_by_day={START_DAY: ["SOL/USD"]}, allow_empty_intraday=True,
            )
        )
    assert topped_up is True, "the fixture must need a top-up, or nothing is reloaded"
    assert fresh.seeded == ["SOL/USD"]
    assert "SOL/USD" in fresh.eligible_by_day[START_DAY]


def test_a_fetch_that_gains_no_bars_is_not_reported_as_a_top_up(cache):
    """`intraday_topped_up` is shown to the user as "bars were downloaded for
    this run", and the reloaded dataset replaces the one already in hand. Doing
    either after a fetch that added nothing — the broker returned bars the cache
    already had — is a claim about where the data came from that isn't true.

    Deliberately NOT the empty-response case: the broker answers with bars the
    cache already holds, so the save reports rows and only comparing the two
    datasets can tell that nothing was gained."""
    already_cached = _fifteens("2026-08-02", 1.05, 0.0005)

    async def fake_bars(self, symbols, asset_class, timeframe, start, end=None):
        return {s: already_cached for s in symbols}

    ds = _dataset()
    with patch.object(AlpacaClient, "historical_bars", new=fake_bars):
        fresh, topped_up = asyncio.run(
            ensure_replay_intraday(
                AlpacaClient.__new__(AlpacaClient), ds, STRATEGY["params"],
                asset_class="crypto", days=1, replay_top_n=10, scanner_cfg=None,
                end=WINDOW_END, window_hours=WINDOW_HOURS, allow_empty_intraday=True,
            )
        )
    assert topped_up is False
    assert fresh is ds


def test_a_short_window_the_fetch_could_not_help_still_refuses_loudly(client, configured, monkeypatch):
    """The refusal from the previous round is HELD BACK until after the fetch,
    not dropped — raising it before would refuse a window the app can perfectly
    well download bars for, and send the user to run a sweep that structurally
    cannot cover the day they asked about. When the fetch does not help, the same
    refusal must still arrive: a silent empty replay presented as a verdict is
    the worst outcome available."""
    from unittest.mock import AsyncMock

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(UTC)
    end = (now - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    day = end.strftime("%Y-%m-%d")
    with Sess() as s:
        barcache.save_daily_bars(s, "NOBARS/USD", [_bar(f"{day}T00:00:00Z", 1.0)],
                                 model=barcache.CryptoDailyBar)
        barcache.store_movers(s, day, [("NOBARS/USD", 5.0, 1.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    sid = client.post("/api/strategies", json={
        "name": "no bars crypto", "asset_class": "crypto", "universe": "scanner",
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

    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})):
        response = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True,
            "window_start": (end - timedelta(hours=4)).isoformat(),
            "window_end": end.isoformat(),
            "starting_cash": 1000, "spread_pct": 0,
        })
    assert response.status_code == 422, response.text
    assert "intraday" in response.json()["detail"]


# ---------------------------------------------------------------------------
# FAULT 1 — the universe, and saying honestly how it was arrived at
# ---------------------------------------------------------------------------


def test_a_seeded_symbol_becomes_eligible_and_gets_its_bars(cache):
    """SOL/USD was in the live universe — the engine bought it. The cached-movers
    reconstruction does not have it, and never will: it ranks a whole day's move
    while the live scanner ranks a rolling 24h one."""
    plain = _dataset()
    assert "SOL/USD" not in plain.union

    seeded = _dataset(seed_by_day={START_DAY: ["SOL/USD"]})
    assert "SOL/USD" in seeded.union
    assert "SOL/USD" in seeded.eligible_by_day[START_DAY]
    assert seeded.seeded == ["SOL/USD"]


def test_seeding_opens_a_day_the_movers_cache_says_nothing_about(cache):
    """This test previously asserted the OPPOSITE — that a day with no movers row
    must stay absent, because absent means UNRESTRICTED in run_backtest and
    naming it would cut that day down to the seeds. Reversed deliberately, on
    evidence from the live instance.

    The movers cache only gains a row for a day once that day's DAILY bar exists,
    and the sweep refuses today's still-forming bar. So "today" never has a row —
    and a comparison of a strategy switched on this morning is entirely about
    today. The old rule therefore dropped every seed on the only day that had
    trades (`universe.seeded` came back empty on a report with six of them), and
    left that day ungated, so the replay bought two names off its own bat and the
    report announced them as inventions. Both halves were artefacts of the gap.

    A day with no reconstruction cannot honestly be judged in either direction.
    Gating it to the seeds at least makes the trades under comparison judgeable,
    and `seeded` says plainly that the universe was handed over rather than
    rebuilt. The cost is real and accepted: on such a day the replay can no
    longer surface a name the live engine missed."""
    ds = _dataset(seed_by_day={"2026-08-03": ["SOL/USD"]})
    assert ds.eligible_by_day["2026-08-03"] == {"SOL/USD"}
    assert "SOL/USD" in ds.seeded


def test_a_symbol_the_movers_already_had_is_not_reported_as_seeded(cache):
    """`seeded` is the honesty flag. Listing a name the reconstruction produced
    on its own would understate the replay's real coverage."""
    ds = _dataset(seed_by_day={START_DAY: ["ADA/USD"]})
    assert ds.seeded == []
    assert "ADA/USD" in ds.eligible_by_day[START_DAY]


def test_the_seed_is_built_from_the_days_the_engine_actually_acted(cache=None):
    """Per (symbol, DAY), not per symbol. Pinning a name for every day of a long
    comparison would hand the replay a universe far wider than the scanner had,
    and every invented trade that followed would be this function's fault."""
    rows = [
        {"symbol": "ada/usd", "entry_day": "2026-08-02"},
        {"symbol": "SOL/USD", "entry_day": "2026-08-02"},
        {"symbol": "XRP/USD", "entry_day": "2026-08-03"},
    ]
    assert _seed_by_day(rows) == {
        "2026-08-02": ["ADA/USD", "SOL/USD"],
        "2026-08-03": ["XRP/USD"],
    }


def test_a_rejected_row_still_seeds_its_symbol():
    """A rail refusing a trade proves the engine WANTED it, which proves the
    scanner had surfaced it. Dropping those would leave exactly the names a
    comparison most needs to explain outside the replay's universe."""
    rows = [{"symbol": "PAXG/USD", "entry_day": "2026-08-02", "status": "rejected"}]
    assert _seed_by_day(rows) == {"2026-08-02": ["PAXG/USD"]}


def test_a_row_with_no_entry_day_seeds_nothing():
    """There is no day to pin it to, and pinning it to all of them is the wide
    universe this deliberately avoids."""
    assert _seed_by_day([{"symbol": "PAXG/USD", "entry_day": None}]) == {}


def test_a_seeded_miss_is_reported_as_a_signal_difference_not_a_coverage_gap():
    """The whole point of seeding. "The replay was watching this and passed" is a
    weak claim when the replay reconstructed the universe itself; it is a strong
    one when we handed it the symbol precisely so coverage could not be the
    excuse."""
    live = [{"symbol": "SOL/USD", "entry_day": "2026-08-02", "status": "closed",
             "entry_price": 150.0, "pnl": 1.0}]
    out = compare(
        live, {"trade_list": [], "open_positions": []},
        replayed_symbols=["ADA/USD", "SOL/USD"], seeded_symbols=["SOL/USD"],
    )
    assert out["decision"]["missed_despite_seeding"] == 1
    detail = next(r["detail"] for r in out["log"] if r["symbol"] == "SOL/USD")
    assert "signal difference" in detail
    assert "points at a real bug" not in detail


def test_an_unseeded_miss_keeps_its_old_wording():
    """Guards the branch above: if this ever changed, the test above would be
    passing for the wrong reason."""
    live = [{"symbol": "ADA/USD", "entry_day": "2026-08-02", "status": "closed",
             "entry_price": 1.0, "pnl": 1.0}]
    out = compare(
        live, {"trade_list": [], "open_positions": []},
        replayed_symbols=["ADA/USD"], seeded_symbols=[],
    )
    assert out["decision"]["missed_despite_seeding"] == 0
    detail = next(r["detail"] for r in out["log"] if r["symbol"] == "ADA/USD")
    assert "points at a real bug" in detail


def test_a_symbol_outside_the_universe_is_still_a_coverage_gap():
    """Seeding is opt-in per comparison. Without it the old three states stand,
    and a name the replay never saw must not be dressed up as a signal
    difference."""
    live = [{"symbol": "PAXG/USD", "entry_day": "2026-08-02", "status": "closed",
             "entry_price": 3000.0, "pnl": 1.0}]
    out = compare(
        live, {"trade_list": [], "open_positions": []},
        replayed_symbols=["ADA/USD"], seeded_symbols=["SOL/USD"],
    )
    assert out["decision"]["missed_outside_universe"] == 1
    assert out["decision"]["missed_despite_seeding"] == 0
    detail = next(r["detail"] for r in out["log"] if r["symbol"] == "PAXG/USD")
    assert "wasn't in the universe" in detail


# ---------------------------------------------------------------------------
# End to end, through /api/fidelity/compare
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


def test_a_scanner_comparison_seeds_the_names_the_engine_traded(client, configured, monkeypatch):
    """The whole reported case, end to end.

    A crypto scanner strategy bought two coins in a four-hour window. The cached
    movers list has one of them; the live rolling-24h scanner had both. Without
    seeding, the second is "a trade the backtest missed" forever — and the report
    must both find it AND say plainly that it only found it because the journal
    told it where to look.
    """
    from unittest.mock import AsyncMock

    from qt.db import session_scope
    from qt.models import Strategy, Trade

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(UTC)
    # A window pinned to YESTERDAY, so every day it touches has a completed daily
    # bar and a movers row — this test is about the universe, not about the
    # still-forming day (which the fetch tests above cover).
    end = (now - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=4)
    day = end.strftime("%Y-%m-%d")
    prev = (end - timedelta(days=1)).strftime("%Y-%m-%d")

    with Sess() as s:
        for symbol, base in (("SEEDA/USD", 1.0), ("SEEDB/USD", 2.0)):
            barcache.save_daily_bars(
                s, symbol,
                [_bar(f"{d}T00:00:00Z", base * (1 + 0.01 * n))
                 for n, d in enumerate(
                     [(end - timedelta(days=k)).strftime("%Y-%m-%d") for k in (3, 2, 1, 0)]
                 )],
                model=barcache.CryptoDailyBar,
            )
            barcache.save_intraday_bars(
                s, symbol,
                _fifteens(prev, base, base * 0.001) + _fifteens(day, base * 1.1, base * 0.001),
                model=barcache.CryptoIntradayBar,
            )
        # Only SEEDA made the reconstructed movers list. SEEDB is the coin the
        # rolling-24h scanner had and the whole-day ranking did not.
        barcache.store_movers(s, day, [("SEEDA/USD", 9.0, 1.1, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    sid = client.post("/api/strategies", json={
        "name": "seeded crypto", "asset_class": "crypto", "universe": "scanner",
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
        for symbol in ("SEEDA/USD", "SEEDB/USD"):
            s.add(Trade(
                strategy_id=sid, mode="paper", symbol=symbol, asset_class="crypto",
                status="open", qty=10, notional=100, entry_price=1.0,
                entry_reason="gain", entry_at=start + timedelta(minutes=30),
            ))

    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})):
        response = client.post("/api/fidelity/compare", json={
            "strategy_id": sid, "mode": "paper",
            "window_start": start.isoformat(), "window_end": end.isoformat(),
        })
    assert response.status_code == 200, response.text
    body = response.json()

    # It found the coin the reconstruction never had…
    assert body["decision"]["missed_outside_universe"] == 0
    assert "SEEDB/USD" in body["replayed_symbols"]
    # …and says so, rather than presenting it as a reconstruction.
    assert body["universe"]["seeded"] == ["SEEDB/USD"]
    assert body["universe"]["from_daily_movers"] is True
    assert body["universe"]["scanner_config_is_current"] is True



def test_a_still_open_live_position_is_not_reported_as_a_sale():
    """The replay exited and the live trade is still open. That fell through to
    the timing-differs branch and rendered "You sold ETH/USD on None ()" — a sale
    that never happened, with no date, about a position the user still holds.

    The row is stamped with the REPLAY's exit, because that is the only moment it
    is about; without it the row also had no time and sorted to the top."""
    from qt.services.fidelity import compare

    live = [{
        "symbol": "ETH/USD", "entry_day": "2026-08-03", "status": "open",
        "entry_price": 1872.0, "entry_at": "2026-08-03T01:03:00Z",
        "exit_day": None, "exit_at": None, "exit_reason": "",
    }]
    result = {
        "trade_list": [{
            "symbol": "ETH/USD", "entry_day": "2026-08-03", "entry_price": 1870.0,
            "exit_day": "2026-08-03", "exit_price": 1847.0,
            "exit_reason": "stop-loss: -1.24% <= -1%",
            "sim_exit_at": "2026-08-03T04:15:00Z",
        }],
        "open_positions": [],
    }
    report = compare(live, result, replayed_symbols=["ETH/USD"])
    sold = [r for r in report["log"] if r["action"] == "sold"]
    assert len(sold) == 1
    row = sold[0]
    assert row["verdict"] == "replay sold, you held"
    assert "still holding it" in row["detail"]
    assert "None" not in row["detail"], "a sale that never happened"
    assert row["day"] == "2026-08-03", "the row must carry the replay's exit day"


def test_two_open_positions_produce_no_exit_row_at_all():
    """Both sides still holding. `exit_day_matches` demands two real days, so
    None-vs-None reads as a mismatch and the row fell through to the timing
    branch: "You sold AAVE/USD on None (); the replay held until None (None)" —
    a sale that happened on neither side, about a position both still hold.

    Three of those appeared in one real report. There is nothing to compare
    until somebody sells, so the correct output is silence."""
    from qt.services.fidelity import compare

    live = [{
        "symbol": "AAVE/USD", "entry_day": "2026-08-03", "status": "open",
        "entry_price": 92.23, "entry_at": "2026-08-03T13:14:19Z",
        "exit_day": None, "exit_at": None, "exit_reason": "",
    }]
    result = {
        "trade_list": [],
        "open_positions": [{
            "symbol": "AAVE/USD", "entry_day": "2026-08-03", "entry_price": 92.10,
            "exit_day": None, "exit_reason": None,
        }],
    }
    report = compare(live, result, replayed_symbols=["AAVE/USD"])
    assert [r for r in report["log"] if r["action"] == "bought"], "the entry still matches"
    assert [r for r in report["log"] if r["action"] == "sold"] == [], (
        "neither side sold, so no exit row belongs in the log"
    )
