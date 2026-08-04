"""Audit (2026-08-03): `rails_seeded` used to be a confirmation that confirmed
nothing.

`qt.api.backtest` set it to `sorted(body.prior_loss_at)` — the REQUEST BODY,
echoed straight back into the result. The fidelity report reads that field to
state that the replay started under the same after-loss cooldown the live account
was carrying, and the existing coverage only proved the key could be ABSENT. So a
replay that accepted the seed and silently dropped it reported it as applied
exactly as one that honoured it, and the instrument would have lied in the
dangerous direction: hiding the very live-vs-replay divergence it exists to find.

It now comes off the rail state `run_backtest` actually loaded, so unwiring the
seed empties the list.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services.backtest import run_backtest, run_portfolio_backtest
from qt.services.engine import RISK_DEFAULTS


@pytest.fixture()
def configured(client):
    """Broker credentials present (require_client) and a clean strategy table
    afterwards — the same recipe the other API-level tests here use."""
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

RISK = dict(
    RISK_DEFAULTS,
    max_total_exposure_usd=1_000_000,
    max_daily_loss_usd=1_000_000,
    cooldown_hours_after_loss=48,
)

DAY0 = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)


def _bars(closes: list[float], start: datetime = DAY0) -> list[dict]:
    """One bar a day, so the day-gain baseline is the previous bar's close."""
    return [
        {"t": (start + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "c": c, "h": c, "l": c, "v": 1000, "vw": c}
        for i, c in enumerate(closes)
    ]


# Flat, then a +5% day that clears the entry bar.
RISER = _bars([100.0, 100.0, 105.0, 105.0])

STRATEGY = {
    "asset_class": "stock",
    "swing_mode": True,
    "sizing_usd": 1000.0,
    "sleeve_usd": 5000.0,
    "max_positions": 3,
    "params": {
        "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0,
                 "max_holding_hours": 0, "flatten_before_close": False,
                 "exit_below_vwap": False},
    },
}

# A loss the account took the day before the window opens — inside the 48h cooldown.
LOST_AT = DAY0 + timedelta(days=1, hours=12)
# THE SEED IS DELIBERATELY NOT WHAT GETS CONSUMED. `_seed_losses` upper-cases the
# symbol and drops a None entry, so the request says {"aaa", "ZZZ"} and the rail
# state ends up {"AAA"}. That gap is the whole discriminator: it is what tells a
# field derived from the SIMULATION apart from one echoing the REQUEST, which is
# the bug being closed. With a seed that survives the round trip unchanged the two
# are indistinguishable and the test proves nothing.
SEED = {"aaa": LOST_AT, "ZZZ": None}


def _run(prior_loss_at=None, strategy=None, bars=None, market="stock") -> dict:
    return run_backtest(
        strategy or STRATEGY, bars or {"AAA": RISER}, RISK,
        starting_cash=5000, spread_pct=0, market=market,
        prior_loss_at=prior_loss_at,
    )


def _entries(r: dict) -> int:
    return r["trades"] + len(r["open_positions"])


def test_the_seed_is_reported_only_because_the_replay_loaded_it():
    """The claim itself: the field names the symbols whose rail state the
    SIMULATION took in, normalised as the simulation normalised them — not the
    keys the caller happened to send."""
    assert _run(SEED)["rails_seeded"] == ["AAA"]


def test_a_run_with_no_seed_claims_none():
    """The control. An ordinary backtest has no "before the window" to carry in,
    and must not report one — otherwise the test above would pass against a field
    that simply always listed something."""
    assert _run()["rails_seeded"] == []


# A crypto book, so the AFTER-LOSS COOLDOWN is the only rail the seed can trip:
# the wash-sale guard is stocks-only, and with a stock fixture both fire, so
# unwiring either one leaves the entry blocked by the other and the test cannot
# tell which (or whether any) is doing the work.
CRYPTO = {**STRATEGY, "asset_class": "crypto"}


def test_the_seed_actually_blocks_the_entry_it_says_it_blocks():
    """What makes the report meaningful. Bookkeeping that names a seed while the
    rails ignore it is the same lie one layer down, so the seeded run must refuse
    the trade the unseeded run takes."""
    free = _run(strategy=CRYPTO, bars={"AAA/USD": RISER}, market="crypto")
    held = _run({"AAA/USD": LOST_AT}, strategy=CRYPTO,
                bars={"AAA/USD": RISER}, market="crypto")
    assert _entries(free) == 1, "the unseeded control never traded — nothing is being proven"
    assert _entries(held) == 0


def test_the_window_s_own_losses_are_not_reported_as_carried_in():
    """The snapshot has to be taken BEFORE the replay runs: `last_loss_at` gains
    the window's own losing exits, and reading it at the end would report a loss
    this run produced as one the account walked in with."""
    losing = _bars([100.0, 100.0, 105.0, 80.0])  # enters, then collapses
    stopping = {**STRATEGY, "params": {
        **STRATEGY["params"],
        "exit": {**STRATEGY["params"]["exit"], "stop_loss_pct": 5.0},
    }}
    result = run_backtest(
        stopping, {"AAA": losing}, RISK, starting_cash=5000, spread_pct=0,
    )
    assert result["trades"] == 1 and result["trade_list"][0]["pnl"] < 0, (
        "the fixture never took a loss inside the window"
    )
    assert result["rails_seeded"] == []


def test_the_portfolio_replay_answers_the_same_question_the_same_way():
    """The portfolio path seeds the identical account-wide dict and had no answer
    at all — a caller could not tell a seeded book from an unseeded one."""
    strat = {"id": 1, "name": "Alpha", **STRATEGY}
    seeded = run_portfolio_backtest(
        [strat], {1: {"AAA": RISER}}, RISK, starting_cash=5000, spread_pct=0,
        prior_loss_at=SEED,
    )
    clean = run_portfolio_backtest(
        [strat], {1: {"AAA": RISER}}, RISK, starting_cash=5000, spread_pct=0,
    )
    assert seeded["rails_seeded"] == ["AAA"]
    assert clean["rails_seeded"] == []


def test_the_endpoint_does_not_overwrite_the_replay_s_answer(client, configured):
    """The last link. Both API replay paths used to assign
    `result["rails_seeded"] = sorted(body.prior_loss_at)` AFTER the replay
    returned, so the truthful value the simulation produced was thrown away and
    replaced by the request. The lower-case key proves which one comes back."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    body = {
        "name": "rails seed echo", "asset_class": "stock", "universe": "custom",
        "symbols": ["AAA"], "preset": "custom",
        # A saved strategy must carry a hard stop (the schema insists); the
        # service-level fixtures above leave it off only to isolate one rail.
        "params": {
            "entry": STRATEGY["params"]["entry"],
            "exit": {**STRATEGY["params"]["exit"], "stop_loss_pct": 4, "trailing_stop_pct": 5},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }
    created = client.post("/api/strategies", json=body)
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    bars = AsyncMock(return_value={"AAA": RISER})
    with patch.object(AlpacaClient, "historical_bars", new=bars):
        result = client.post("/api/backtest", json={
            "strategy_id": sid, "symbols": ["AAA"], "days": 30, "timeframe": "1Day",
            "starting_cash": 5000, "spread_pct": 0,
            # Sent lower-case; the simulation stores it upper-case.
            "prior_loss_at": {"aaa": LOST_AT.isoformat()},
        }).json()
    assert result["rails_seeded"] == ["AAA"]
