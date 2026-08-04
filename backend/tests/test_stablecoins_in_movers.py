"""The stablecoin skip was live-only, so every replay could still trade USDC.

`is_stablecoin` had exactly one caller — `scanner._reject_reason` — which is the
LIVE scanner's filter. The bar-cache movers, and therefore every scanner-replay
backtest, every fidelity comparison and every optimizer run, went on offering
USDC/USD and USDT/USD to a strategy the live engine would never have handed
them to. That is a live-vs-replay divergence sitting inside the very feature
whose job is detecting divergence: the replay buys a dollar with a dollar, live
does not, and the report calls it a trade the backtester invented.

Two spellings are in play and that is what hid it. The live scanner sees
Alpaca's slashed form, 'USDC/USD'; the bar cache stores whatever the bars
endpoint returned, which for crypto is slash-less, 'USDCUSD'. `is_stablecoin`
deliberately requires the slash so a stock ticker can never be caught by the
list, so it matched nothing at all on the cache's own spelling.

The distinction that must SURVIVE: the skip is a SCANNER rule. A watchlist, a
basket or a custom list is a set of names the user typed, and a user who puts a
stablecoin on their own watchlist has made a choice.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.services import barcache, scanner


# ---------------------------------------------------------------------------
# 1. READING A BASE OFF EITHER SPELLING
# ---------------------------------------------------------------------------


def test_the_slashless_cache_spelling_is_recognised():
    """THE bug: 'USDCUSD' is what the cache holds, and it matched nothing."""
    assert scanner.is_stablecoin("USDCUSD") is False   # the old answer
    assert scanner.is_stablecoin_pair("USDCUSD") is True
    assert scanner.is_stablecoin_pair("USDTUSD") is True
    assert scanner.is_stablecoin_pair("DAIUSD") is True


def test_the_slashed_spelling_still_works():
    """The live scanner's form has to keep matching, or fixing the replay would
    unfix the engine."""
    assert scanner.is_stablecoin_pair("USDC/USD") is True
    assert scanner.is_stablecoin_pair("usdt/usd") is True


def test_ordinary_coins_are_untouched_in_both_spellings():
    for ok in ("BTCUSD", "BTC/USD", "ETHUSD", "DOGEUSD"):
        assert scanner.is_stablecoin_pair(ok) is False


def test_gold_pegged_paxg_survives_the_slashless_form_too():
    """PAXG tracks the GOLD price and swings several percent a day — a real
    momentum candidate, and one letter from USDP. Stripping the quote leg must
    not turn 'PAXGUSD' into a stablecoin."""
    assert scanner.is_stablecoin_pair("PAXGUSD") is False
    assert scanner.is_stablecoin_pair("PAXG/USD") is False


def test_a_longer_quote_leg_is_stripped_before_a_shorter_one():
    """'BTCUSDT' must yield BTC, not BTCUS. Checking 'USD' first would leave a
    base nothing matches — silently, which is the failure mode this whole file
    is about."""
    assert scanner.is_stablecoin_pair("BTCUSDT") is False
    assert scanner.is_stablecoin_pair("DAIUSDT") is True


# ---------------------------------------------------------------------------
# 2. THE MOVERS CACHE, AT READ TIME AND AT SWEEP TIME
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    with Sess() as s:
        # A cache swept BEFORE the rule existed — the state every live install
        # is actually in. Slash-less, as Alpaca's bars endpoint returns crypto.
        barcache.store_movers(s, "2026-07-30", [
            ("USDCUSD", 0.04, 1.0, 500_000.0),
            ("USDTUSD", 0.03, 1.0, 900_000.0),
            ("DOTUSD", 6.0, 0.78, 400_000.0),
            ("PAXGUSD", 4.0, 3_050.0, 300_000.0),
        ], model=barcache.CryptoDailyMover)
        s.commit()
    return Sess


def _read(Sess, **kw):
    with Sess() as s:
        return barcache.movers_between(s, "2026-07-01", model=barcache.CryptoDailyMover, **kw)


def test_the_premise_all_four_are_in_the_cache(cache):
    """So the assertions below are about filtering, not about an empty fixture."""
    assert len(_read(cache)["2026-07-30"]) == 4


def test_a_stablecoin_is_filtered_out_of_a_crypto_replays_universe(cache):
    got = _read(cache, crypto=True)["2026-07-30"]
    assert "USDCUSD" not in got and "USDTUSD" not in got


def test_real_coins_are_still_offered(cache):
    """Anti-vacuity: the filter is the stablecoin list, not a blanket refusal."""
    got = _read(cache, crypto=True)["2026-07-30"]
    assert "DOTUSD" in got and "PAXGUSD" in got


def test_read_time_filtering_is_what_fixes_an_existing_cache(cache):
    """The rows above were swept before the rule existed. If the skip only
    applied at sweep time, every install would need a re-sweep to stop replaying
    stablecoins — and nothing would tell the user to run one."""
    assert "USDCUSD" in _read(cache)["2026-07-30"]          # unfiltered read
    assert "USDCUSD" not in _read(cache, crypto=True)["2026-07-30"]


def test_the_stock_path_is_not_subjected_to_a_crypto_rule(cache):
    """`crypto` is told, not guessed: a slash-less string is indistinguishable
    from a stock ticker, and the day a ticker collides with a coin name a
    guessing filter would silently delete a tradable stock."""
    assert "USDCUSD" in _read(cache, crypto=False)["2026-07-30"]


def test_a_sweep_stops_storing_stablecoins(cache):
    """Belt and braces beside the read-time filter, exactly as the exclude list
    has both: read-time fixes an existing cache, this stops a new one filling
    with names that can never be traded."""
    quotes = [
        barcache.DayQuote(symbol="USDCUSD", close=1.0, prev_close=0.9996, high=1.0004,
                          volume=5e5, vwap=1.0),
        barcache.DayQuote(symbol="DOTUSD", close=0.78, prev_close=0.7, high=0.8,
                          volume=1e6, vwap=0.78),
    ]
    assert [r[0] for r in barcache.rank_movers(quotes, top_n=10, crypto=True)] == ["DOTUSD"]
    # …and the stock path is unchanged, so no ticker can be lost to this.
    assert len(barcache.rank_movers(quotes, top_n=10, crypto=False)) == 2


def test_the_filter_applies_before_the_top_n_cut(cache):
    """Order matters: filtering after the cut would let a stablecoin consume one
    of the N slots and silently shrink the day's universe — the same argument
    the exclude list already makes.

    The two stablecoins are stored at ranks 1 and 2, so filtering AFTER the cut
    would leave this day with an empty universe."""
    assert _read(cache, crypto=True, top_n=2)["2026-07-30"] == ["DOTUSD", "PAXGUSD"]


# ---------------------------------------------------------------------------
# 3. THE DISTINCTION THAT MUST SURVIVE
# ---------------------------------------------------------------------------


def _replay_cache(monkeypatch) -> str:
    """A crypto cache holding one stablecoin mover and one real one, with daily
    bars for both — enough for the real dataset loader to run offline."""
    from datetime import datetime, timedelta, timezone

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    earlier = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    with Sess() as s:
        for sym, close in (("USDCUSD", 1.0), ("DOTUSD", 0.78)):
            barcache.save_daily_bars(s, sym, [
                {"t": f"{earlier}T00:00:00Z", "o": close, "h": close, "l": close,
                 "c": close, "v": 1e6, "vw": close},
                {"t": f"{day}T00:00:00Z", "o": close, "h": close, "l": close,
                 "c": close, "v": 1e6, "vw": close},
            ], model=barcache.CryptoDailyBar)
        barcache.store_movers(s, day, [
            ("USDCUSD", 0.04, 1.0, 5e5), ("DOTUSD", 6.0, 0.78, 4e5),
        ], model=barcache.CryptoDailyMover)
        s.commit()
    return day


def test_the_replays_scanner_universe_drops_the_stablecoin_end_to_end(monkeypatch):
    """Not the filter in isolation — the actual dataset a scanner replay trades
    off, which is where the divergence lived."""
    from qt.api.backtest import load_scanner_replay_dataset

    day = _replay_cache(monkeypatch)
    ds = load_scanner_replay_dataset("crypto", 30, 10)
    assert "DOTUSD" in ds.eligible_by_day[day]
    assert "USDCUSD" not in ds.eligible_by_day[day]


def test_a_name_the_user_pinned_is_still_eligible(monkeypatch):
    """THE distinction that must survive. A watchlist, basket or custom list is
    a set of names the user typed, and `load_scanner_replay_dataset` unions
    those ON TOP of the movers. Live draws the same line — `is_stablecoin` is
    consulted by the SCANNER's filter and by nothing else — so flattening it
    would be a second divergence pointing the other way."""
    from qt.api.backtest import load_scanner_replay_dataset

    day = _replay_cache(monkeypatch)
    ds = load_scanner_replay_dataset("crypto", 30, 10, always_eligible=["USDCUSD"])
    assert "USDCUSD" in ds.eligible_by_day[day]


def test_the_live_scanner_still_rejects_stablecoins_under_their_own_reason():
    """The engine side is untouched — the fix reached the replay by ADDING a
    caller, not by moving the rule."""
    f = dict(scanner.CRYPTO_DEFAULTS)
    assert scanner._reject_reason(
        f, [], price=0.9998, change_pct=0.03, dollar_volume=4_930.0, symbol="USDC/USD"
    ) == scanner.STABLECOIN_REASON
