"""A replay with an explicit END as well as a start.

Every backtest until now was "the last N days from now". That is fine for asking
"how would this do?", and useless for asking "what happened between the 12th and
the 3rd, under the settings that were live THEN" — which is what a fidelity
comparison of a period whose configuration changed has to ask.

The end of a window is a sharper thing than its start. A bar before `sim_start`
is warm-up: it feeds the indicators and trades nothing. A bar after `sim_end` is
NOT the mirror image of that — it must not even re-mark an open position, or the
result reports an equity value the window could not have known. That distinction
is what most of these tests are about.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import run_backtest, run_portfolio_backtest
from qt.services.engine import RISK_DEFAULTS

RISK = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)

STRATEGY = {
    "asset_class": "stock",
    "swing_mode": True,  # hold across days, so a position is open when the window shuts
    "sizing_usd": 1000.0,
    "sleeve_usd": 5000.0,
    "max_positions": 3,
    "params": {
        "entry": {
            "min_day_gain_pct": 3.0,
            "require_above_vwap": False,
            "entry_window_start": None,
            "entry_window_end": None,
        },
        "exit": {
            "trailing_stop_pct": 5.0,
            "stop_loss_pct": 4.0,
            "take_profit_pct": 0,
            "max_holding_hours": 0,
            "flatten_before_close": False,
            "exit_below_vwap": False,
        },
    },
}

DAYS = ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07"]


def _at(day: str, hour: int) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00")


def _series(closes_by_day: list[list[float]]) -> list[dict]:
    """Three hourly bars a day, stamped 14:00-16:00Z — inside the ET session, so
    each bar buckets into the calendar day it is written under."""
    out = []
    for day, closes in zip(DAYS, closes_by_day):
        for i, c in enumerate(closes):
            out.append(
                {
                    "t": _at(day, 14 + i).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": c, "h": c, "l": c, "c": c, "v": 1000, "vw": c,
                }
            )
    return out


def _run(bars: dict[str, list[dict]], **kw) -> dict:
    return run_backtest(STRATEGY, bars, RISK, starting_cash=5000, spread_pct=0, **kw)


def _entries(result: dict) -> set[str]:
    """Which symbols were bought — closed round-trips plus positions still open
    when the window shut."""
    return {t["symbol"] for t in result["trade_list"]} | {
        p["symbol"] for p in result["open_positions"]
    }


# AAA rises on day 2 and holds; BBB rises on day 4. One symbol per half of the
# window, so what a window includes is visible in the trade list alone.
AAA = _series([[100, 100, 100], [104, 104, 104], [104, 104, 104], [104, 104, 104]])
BBB = _series([[100, 100, 100], [100, 100, 100], [100, 100, 100], [104, 104, 104]])


def test_a_window_covering_every_bar_is_the_same_run_as_no_window_at_all():
    """The compatibility floor. `days` still has hundreds of callers, so a window
    that happens to span everything must produce a byte-identical result — not a
    similar one."""
    plain = _run({"AAA": AAA, "BBB": BBB})
    windowed = _run({"AAA": AAA, "BBB": BBB}, sim_end=_at(DAYS[-1], 16))
    assert windowed == plain


def test_a_window_that_shuts_early_excludes_the_trades_after_it():
    everything = _run({"AAA": AAA, "BBB": BBB})
    assert _entries(everything) == {"AAA", "BBB"}

    early = _run({"AAA": AAA, "BBB": BBB}, sim_end=_at(DAYS[1], 16))
    assert _entries(early) == {"AAA"}
    # …and the window's own days are all that the result describes.
    assert early["equity_days"] == DAYS[:2]


def test_a_position_open_when_the_window_shuts_is_marked_inside_the_window():
    """The one that decides what `sim_end` MEANS.

    AAA is bought at 104 on day 2 and quadruples on day 3. A window shutting at
    the end of day 2 must value that holding at 104 — the last price inside the
    period — not at 200. Carrying the later price in would be look-ahead of the
    plainest kind: the equity curve, the unrealized P&L and the drawdown would
    all describe a longer period than the one asked for, while the header still
    named the shorter one.

    This is also why the guard is a break rather than a warm-up-style continue:
    a continue that kept refreshing prices would pass every other test here and
    fail only this one."""
    spiking = _series([[100, 100, 100], [104, 104, 104], [200, 200, 200], [200, 200, 200]])

    inside = _run({"AAA": spiking}, sim_end=_at(DAYS[1], 16))
    held = inside["open_positions"][0]
    assert held["mark_price"] == 104.0
    assert held["unrealized_pnl"] == 0.0
    assert inside["final_equity"] == 5000.0  # nothing gained; nothing was known yet

    # Without the window the very same bars are worth four times as much, so the
    # assertion above is measuring the window and not the price path.
    after = _run({"AAA": spiking})
    assert after["open_positions"][0]["mark_price"] == 200.0
    assert after["final_equity"] > 5000.0


def test_the_end_of_the_window_is_inclusive():
    """A bar stamped exactly at `sim_end` is inside it. Off by one here and every
    segment of a split period would silently drop its final bar — the bar where a
    strategy that flattens before the close does all its selling."""
    on_the_boundary = _run({"BBB": BBB}, sim_end=_at(DAYS[3], 14))
    assert _entries(on_the_boundary) == {"BBB"}

    a_second_earlier = _run(
        {"BBB": BBB}, sim_end=_at(DAYS[3], 14) - timedelta(seconds=1)
    )
    assert _entries(a_second_earlier) == set()


def test_a_window_can_have_both_ends():
    """sim_start and sim_end together — the shape a segment of a longer period
    takes. Bars before the start are warm-up (they feed indicators, they never
    trade); bars after the end do not exist at all."""
    middle = _run(
        {"AAA": AAA, "BBB": BBB},
        sim_start=_at(DAYS[2], 14),
        sim_end=_at(DAYS[3], 16),
    )
    # AAA's rise was on day 2, before the window opened, and on day 3 it is flat
    # against day 2's close — so only BBB's day-4 rise is inside.
    assert _entries(middle) == {"BBB"}
    assert middle["equity_days"] == DAYS[2:]


# --- the portfolio replay, which shares one account across strategies ---------


def _portfolio_strategy(sid: int, name: str) -> dict:
    return {**STRATEGY, "id": sid, "name": name}


def test_the_portfolio_replay_stops_at_the_window_too():
    strategies = [_portfolio_strategy(1, "early"), _portfolio_strategy(2, "late")]
    bars = {1: {"AAA": AAA}, 2: {"BBB": BBB}}

    everything = run_portfolio_backtest(
        strategies, bars, RISK, starting_cash=5000, spread_pct=0
    )
    assert _entries(everything) == {"AAA", "BBB"}

    early = run_portfolio_backtest(
        strategies, bars, RISK, starting_cash=5000, spread_pct=0,
        sim_end=_at(DAYS[1], 16),
    )
    assert _entries(early) == {"AAA"}
    assert early["equity_days"] == DAYS[:2]


def test_the_portfolio_replay_marks_holdings_inside_the_window_as_well():
    """Same reasoning as the single-strategy case, and it needs its own test:
    the portfolio loop refreshes prices in a separate pass, so the guard has to
    sit ahead of it rather than beside the sim_start check."""
    spiking = _series([[100, 100, 100], [104, 104, 104], [200, 200, 200], [200, 200, 200]])
    strategies = [_portfolio_strategy(1, "only")]

    inside = run_portfolio_backtest(
        strategies, {1: {"AAA": spiking}}, RISK, starting_cash=5000, spread_pct=0,
        sim_end=_at(DAYS[1], 16),
    )
    assert inside["open_positions"][0]["mark_price"] == 104.0
    assert inside["final_equity"] == 5000.0


# --- the endpoint ------------------------------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402 — the endpoint half starts here

import pytest  # noqa: E402

from qt import security  # noqa: E402
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient  # noqa: E402
from qt.db import session_scope  # noqa: E402
from qt.models import Strategy, StrategyConfigVersion, Trade  # noqa: E402


@pytest.fixture()
def configured(client):
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


def _api_strategy() -> dict:
    return {
        "name": "windowed", "asset_class": "stock", "universe": "custom",
        "symbols": ["AAA", "BBB"], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }


def _daily_from_now(rise_days_ago: int) -> list[dict]:
    """One bar a day for the last 20 days, flat at 100 until `rise_days_ago`,
    then 104 — a single, dateable entry day."""
    now = datetime.now(timezone.utc)
    out = []
    for n in range(20, 0, -1):
        c = 100.0 if n > rise_days_ago else 104.0
        ts = (now - timedelta(days=n)).replace(hour=14, minute=0, second=0, microsecond=0)
        out.append(
            {"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c, "h": c, "l": c, "c": c,
             "v": 1000, "vw": c}
        )
    return out


def _post(client, **extra) -> dict:
    sid = client.post("/api/strategies", json=_api_strategy()).json()["id"]
    bars = {"AAA": _daily_from_now(15), "BBB": _daily_from_now(5)}
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)):
        response = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["AAA", "BBB"], "timeframe": "1Day",
                  "starting_cash": 5000, "spread_pct": 0, **extra},
        )
    return response


def test_an_explicit_window_replays_only_what_happened_inside_it(client, configured):
    """AAA rose 15 days ago, BBB 5 days ago. A window covering only the older
    half must find AAA and nothing else — the bars for BBB's rise are downloaded
    (the fetch has no end) and simply never traded."""
    now = datetime.now(timezone.utc)
    body = _post(
        client,
        window_start=(now - timedelta(days=21)).isoformat(),
        window_end=(now - timedelta(days=10)).isoformat(),
    ).json()

    traded = {t["symbol"] for t in body["trade_list"]} | {
        p["symbol"] for p in body["open_positions"]
    }
    assert traded == {"AAA"}, body.get("diagnosis")
    # The window is echoed back, so a caller splitting a period can prove which
    # slice each result covers.
    assert body["window_start"].startswith((now - timedelta(days=21)).strftime("%Y-%m-%d"))
    assert body["days"] == 11


def test_the_same_run_without_the_window_finds_both(client, configured):
    """The control. Without it the test above could be passing because the bars
    are wrong rather than because the window works."""
    body = _post(client, days=30).json()
    traded = {t["symbol"] for t in body["trade_list"]} | {
        p["symbol"] for p in body["open_positions"]
    }
    assert traded == {"AAA", "BBB"}
    # An open-ended run's end is "whenever you pressed the button", so it is not
    # stamped — printing a precise end for that would be false precision.
    assert "window_end" not in body


def test_a_window_that_ends_before_it_starts_is_refused(client, configured):
    now = datetime.now(timezone.utc)
    response = _post(
        client,
        window_start=(now - timedelta(days=5)).isoformat(),
        window_end=(now - timedelta(days=10)).isoformat(),
    )
    assert response.status_code == 422
    assert "after its start" in response.text


def test_an_end_on_its_own_still_counts_days_back_from_it(client, configured):
    """`window_end` without a start means "the `days` before that moment" — the
    natural reading, and the one that lets a caller move an ordinary request
    backwards in time without restating its length."""
    now = datetime.now(timezone.utc)
    body = _post(client, days=12, window_end=(now - timedelta(days=10)).isoformat()).json()
    traded = {t["symbol"] for t in body["trade_list"]} | {
        p["symbol"] for p in body["open_positions"]
    }
    assert traded == {"AAA"}
    assert body["days"] == 12
