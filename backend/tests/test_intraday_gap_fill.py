"""A held position must stay visible after its symbol stops being a riser.

The intraday cache is filled per MOVER-DAY: a symbol gets 15-minute bars for the
days it made a top-N list (plus a short baseline), and for nothing else. But a
position opened on the day a symbol rose can still be open months later, long
after that symbol dropped off every list.

For those days the replay was blind. No bar means no mark, so the position kept
its last seen price; no bar means no exit check, so its stop-loss and trailing
stop could not fire. The position ran unmanaged until the symbol happened to rise
again, and weeks of price movement then landed in a single step — the
flat-line-then-cliff shape on the equity chart.

The daily series is already loaded (it covers the whole window for every symbol,
because the daily sweep is universe-wide) and costs nothing extra, so it fills
those days. Daily resolution, not blind.
"""

from qt.api.backtest import _fill_intraday_gaps


def _ibar(ts: str, c: float) -> dict:
    return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1000, "vw": c}


def _dbar(day: str, c: float, high: float | None = None, low: float | None = None) -> dict:
    return {"t": f"{day}T12:00:00Z", "o": c, "h": high or c, "l": low or c,
            "c": c, "v": 10000, "vw": c}


def test_days_without_intraday_bars_get_the_daily_bar():
    """DOGE rises on the 5th, gets intraday bars, then is never a riser again —
    but the position opened on the 5th is still open."""
    intraday = {"DOGE/USD": [_ibar("2026-05-05T12:00:00Z", 10.0)]}
    daily = {"DOGE/USD": [_dbar("2026-05-05", 10.0), _dbar("2026-05-06", 9.0),
                          _dbar("2026-05-07", 8.0)]}
    merged, filled = _fill_intraday_gaps(intraday, daily, "2026-05-05")
    assert [b["t"][:10] for b in merged["DOGE/USD"]] == [
        "2026-05-05", "2026-05-06", "2026-05-07"
    ]
    assert filled == 2


def test_a_day_that_has_intraday_bars_is_left_alone():
    """No double-counting, and no downgrade: where the finer data exists it is
    the only thing used for that day."""
    intraday = {"AAA": [_ibar("2026-05-05T12:00:00Z", 10.0), _ibar("2026-05-05T18:00:00Z", 11.0)]}
    daily = {"AAA": [_dbar("2026-05-05", 10.5)]}
    merged, filled = _fill_intraday_gaps(intraday, daily, "2026-05-05")
    assert len(merged["AAA"]) == 2
    assert filled == 0
    assert all(b["t"].endswith(("12:00:00Z", "18:00:00Z")) for b in merged["AAA"])


def test_warmup_bars_before_the_window_never_leak_in():
    """`daily` deliberately reaches back over the indicator warm-up. Letting those
    into the replay timeline would silently extend the tested period — the run
    would cover months nobody asked for and every reported return would describe
    a different window."""
    intraday = {"AAA": [_ibar("2026-05-05T12:00:00Z", 10.0)]}
    daily = {"AAA": [_dbar("2026-01-01", 5.0), _dbar("2026-03-01", 7.0),
                     _dbar("2026-05-05", 10.0), _dbar("2026-05-06", 9.0)]}
    merged, filled = _fill_intraday_gaps(intraday, daily, "2026-05-05")
    assert min(b["t"][:10] for b in merged["AAA"]) == "2026-05-05"
    assert filled == 1


def test_the_merged_series_stays_in_time_order():
    """The replay walks bars in order; an out-of-order series would rewind the
    clock mid-run."""
    intraday = {"AAA": [_ibar("2026-05-07T12:00:00Z", 12.0)]}
    daily = {"AAA": [_dbar("2026-05-05", 10.0), _dbar("2026-05-06", 11.0),
                     _dbar("2026-05-08", 13.0)]}
    merged, _ = _fill_intraday_gaps(intraday, daily, "2026-05-05")
    stamps = [b["t"] for b in merged["AAA"]]
    assert stamps == sorted(stamps)


def test_a_symbol_with_no_daily_bars_is_left_as_it_is():
    """Missing daily data is a cache problem, not something to invent around —
    the gap warning reports it rather than this function papering over it."""
    intraday = {"AAA": [_ibar("2026-05-05T12:00:00Z", 10.0)]}
    merged, filled = _fill_intraday_gaps(intraday, {}, "2026-05-05")
    assert merged["AAA"] == intraday["AAA"]
    assert filled == 0


def test_the_stop_that_could_not_fire_now_fires():
    """The payoff, end to end.

    A position is opened on a riser, the symbol leaves the top-N list, and its
    price then falls 30% over the following days. With intraday bars alone the
    replay never sees those days: the stop cannot trigger and the position is
    still open at the end, marked at a stale price. Filled with daily bars the
    stop fires on the day the price actually breaks it.
    """
    from qt.services.backtest import run_backtest
    from qt.services.engine import RISK_DEFAULTS

    strategy = {
        "asset_class": "crypto",
        "swing_mode": False,
        "sizing_usd": 1000.0,
        "sleeve_usd": 5000.0,
        "max_positions": 3,
        "params": {
            "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 10.0, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
    }
    risk = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)

    # Two mover-days with intraday bars: a +5% rise that triggers the entry.
    intraday = {
        "DOGE/USD": [
            _ibar("2026-05-04T12:00:00Z", 100.0),
            _ibar("2026-05-05T12:00:00Z", 105.0),
        ]
    }
    # ...then it drops away, with daily bars the cache has all along.
    daily = {
        "DOGE/USD": [
            _dbar("2026-05-04", 100.0), _dbar("2026-05-05", 105.0),
            _dbar("2026-05-06", 99.0), _dbar("2026-05-07", 88.0),   # through the 10% stop
            _dbar("2026-05-08", 80.0), _dbar("2026-05-09", 74.0),
        ]
    }
    eligible = {"2026-05-05": {"DOGE/USD"}}

    blind = run_backtest(strategy, intraday, risk, starting_cash=5000, spread_pct=0,
                         market="crypto", eligible_by_day=eligible)
    assert blind["trades"] == 0, "no bars after entry means the stop never gets a chance"
    assert len(blind["open_positions"]) == 1, "it just sits there, marked at a stale price"
    # The damning part: the last price it ever saw IS the entry price, so the
    # frozen mark shows no loss at all. The blind run doesn't merely mis-time the
    # exit — it reports a position that fell 30% as costing nothing.
    assert blind["net_pnl"] == 0

    merged, filled = _fill_intraday_gaps(intraday, daily, "2026-05-04")
    assert filled == 4
    seeing = run_backtest(strategy, merged, risk, starting_cash=5000, spread_pct=0,
                          market="crypto", eligible_by_day=eligible)
    assert seeing["trades"] == 1, "the stop should fire once the days are visible"
    assert not seeing["open_positions"]
    # Filling the gap does not flatter the result — it makes it true. Here that
    # means booking a real loss the blind run hid completely.
    assert seeing["net_pnl"] < -100


def test_the_dataset_loader_actually_applies_the_fill(monkeypatch):
    """Through the real cache read, not the helper in isolation — the fill is
    only worth anything if the loader uses it."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from qt.api.backtest import load_scanner_replay_dataset
    from qt.services import barcache

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=n)).strftime("%Y-%m-%d") for n in (6, 5, 4, 3)]
    riser_day = days[1]

    with Sess() as s:
        # Intraday bars ONLY around the day it was a riser — the cache's real shape.
        barcache.save_intraday_bars(s, "BTC/USD", [
            _ibar(f"{days[0]}T12:00:00Z", 100.0),
            _ibar(f"{riser_day}T12:00:00Z", 106.0),
        ], model=barcache.CryptoIntradayBar)
        # Daily bars for the whole window, as the universe sweep leaves them.
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": f"{d}T00:00:00Z", "o": c, "h": c, "l": c, "c": c, "v": 1e6, "vw": c}
            for d, c in zip(days, (100.0, 106.0, 95.0, 88.0))
        ], model=barcache.CryptoDailyBar)
        barcache.store_movers(s, riser_day, [("BTC/USD", 6.0, 106.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    ds = load_scanner_replay_dataset("crypto", 30, 10)
    assert ds.used_intraday is True
    assert ds.daily_filled_days == 2  # the two days after it stopped being a riser
    covered = {b["t"][:10] for b in ds.bars["BTC/USD"]}
    assert days[2] in covered and days[3] in covered, "the held days must be visible"


def test_the_stock_side_gets_the_same_treatment(monkeypatch):
    """Everything above was exercised on the crypto tables. The stock side is a
    separate set of tables with a different day stamp (14:00Z vs 12:00Z) and a
    different sweep, so "it works for crypto" proves nothing about it."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from qt.api.backtest import load_scanner_replay_dataset
    from qt.services import barcache

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=n)).strftime("%Y-%m-%d") for n in (6, 5, 4, 3)]
    riser = days[1]

    with Sess() as s:
        # Stock tables (the defaults), and a real 15-minute bar stamped at exactly
        # 14:00Z — the same stamp cached_daily_bars gives a DAILY stock bar. If the
        # fill were detected by timestamp this bar would be mistaken for one.
        barcache.save_intraday_bars(s, "NVDA", [
            _ibar(f"{days[0]}T14:00:00Z", 100.0),
            _ibar(f"{riser}T14:00:00Z", 106.0),
        ])
        barcache.save_daily_bars(s, "NVDA", [
            {"t": f"{d}T00:00:00Z", "o": c, "h": c, "l": c, "c": c, "v": 1e6, "vw": c}
            for d, c in zip(days, (100.0, 106.0, 105.0, 104.0))
        ])
        barcache.store_movers(s, riser, [("NVDA", 6.0, 106.0, 1e8)])
        s.commit()

    ds = load_scanner_replay_dataset("stock", 30, 10)
    assert ds.used_intraday is True
    assert ds.market == "stock" and ds.benchmark_symbol == "SPY"
    # The held days after it stopped being a riser are filled from the daily bars.
    assert ds.daily_filled_days == 2
    covered = {b["t"][:10] for b in ds.bars["NVDA"]}
    assert days[2] in covered and days[3] in covered
    # The real 14:00Z intraday bars are NOT tagged as fills…
    real = [b for b in ds.bars["NVDA"] if b["t"].endswith("T14:00:00Z") and b["t"][:10] == riser]
    assert real and not any(b.get("daily_fill") for b in real)
    # …and the stand-ins are.
    stand_ins = [b for b in ds.bars["NVDA"] if b["t"][:10] == days[2]]
    assert stand_ins and all(b.get("daily_fill") for b in stand_ins)


def test_real_bars_at_the_daily_stamp_are_not_mistaken_for_fills():
    """A stock DAILY bar is stamped 14:00Z, and 14:00Z is also an ordinary
    15-minute bar time during the session. Spotting fills by their timestamp
    therefore misreads genuine intraday bars as stand-ins, concludes the held
    days are uncovered, and re-downloads them on EVERY run — for a symbol whose
    coverage was already complete."""
    import asyncio

    from qt.api.backtest import ScannerReplayDataset, fetch_held_position_bars

    real_bars = [
        _ibar("2026-05-05T14:00:00Z", 100.0),
        _ibar("2026-05-05T18:00:00Z", 101.0),
        # 05-06's ONLY bar sits on the daily stamp. With any timestamp-based test
        # this day looks uncovered and gets re-fetched; it is a real 15-minute bar.
        _ibar("2026-05-06T14:00:00Z", 102.0),
    ]
    ds = ScannerReplayDataset(
        bars={"NVDA": real_bars}, eligible_by_day={}, timeframe="15Min",
        used_intraday=True, union=["NVDA"], market="stock", benchmark_class="stock",
        benchmark_symbol="SPY", start_day="2026-05-05", days_replayed=2,
        replayed=["NVDA"], dropped=[], intraday_covered=1, daily={}, daily_filled_days=0,
    )
    result = {
        "trade_list": [{"symbol": "NVDA", "entry_day": "2026-05-05", "exit_day": "2026-05-06"}],
        "open_positions": [],
    }

    # Records rather than raises: the fetch path retries and then swallows
    # exceptions by design, so a raising stub would be silently absorbed and the
    # test would pass no matter what.
    calls: list = []

    class Spy:
        async def historical_bars(self, symbols, *a, **kw):
            calls.append(tuple(symbols))
            return {}

    filled = asyncio.run(fetch_held_position_bars(Spy(), result, ds, asset_class="stock"))
    assert calls == [], f"re-downloaded days that were already covered: {calls}"
    assert filled == 0
