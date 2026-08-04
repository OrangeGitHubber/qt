"""The fidelity replay starts with the rail state the live engine really had.

Measured on the running instance: a comparison of strategy 26 reported that the
replay had bought ADA/USD and that "the replay invented it — it believes
something was tradable that wasn't". The truth was the exact opposite. Live could
not buy ADA: another strategy had closed a loss on it about twelve hours earlier
and the account-wide 24h after-loss rail was still in force. The replay began the
window with an empty `last_loss_at`, so it had no way to know, and the report
blamed the backtester for the difference.

The rail itself was always simulated correctly against sim time. It was merely
unseeded — which is a different fault, and a much quieter one.

Seeded for the COMPARISON only. An ordinary 180-day backtest has no meaningful
"before the window" and inventing one would be arbitrary, so `prior_loss_at`
defaults to None and every existing caller is untouched.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import backtest as bt
from qt.services.backtest import run_backtest, run_portfolio_backtest
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc
NOW = datetime.now(UTC)

RISK = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)
# The wash-sale guard reads the SAME loss history over 31 days, so leaving it on
# would mask which rail a stock test is actually exercising. Each test below says
# which of the two it means.
RISK_NO_WASH = dict(RISK, wash_sale_guard="off")

STRATEGY = {
    "asset_class": "stock",
    "swing_mode": False,
    "sizing_usd": 1000.0,
    "sleeve_usd": 5000.0,
    "max_positions": 3,
    "params": {
        "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 5.0, "stop_loss_pct": 4.0, "take_profit_pct": 0,
                 "max_holding_hours": 0, "flatten_before_close": False,
                 "exit_below_vwap": False},
    },
}


def _bars(closes: list[float], start: str) -> list[dict]:
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        {"t": (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "o": c, "h": c, "l": c, "c": c, "v": 1000, "vw": c}
        for i, c in enumerate(closes)
    ]


def _two_days(day1: list[float], day2: list[float]) -> list[dict]:
    """Day one establishes the previous close; day two is where entries happen."""
    return _bars(day1, "2026-05-04T14:00:00Z") + _bars(day2, "2026-05-05T14:00:00Z")


def _entries(result: dict) -> int:
    return result["trades"] + len(result["open_positions"])


# --- the simulator -----------------------------------------------------------


def test_an_unseeded_replay_takes_the_trade_the_live_engine_could_not():
    """The control and the fix, side by side. Same bars, same rules: the only
    difference is whether the replay is told what the account already lost."""
    series = _two_days([100, 100, 100], [104, 104, 104])

    clean = run_backtest(STRATEGY, {"ADA": series}, RISK, starting_cash=5000, spread_pct=0)
    assert _entries(clean) == 1  # the ADA/USD buy the report called "invented"

    # Twelve hours before the entry bar, exactly as measured: a loss taken by
    # ANOTHER strategy, which is why the replay could never have known about it.
    seeded = run_backtest(
        STRATEGY, {"ADA": series}, RISK, starting_cash=5000, spread_pct=0,
        prior_loss_at={"ADA": datetime(2026, 5, 5, 2, 0, tzinfo=UTC)},
    )
    assert _entries(seeded) == 0


def test_a_seed_older_than_the_cooldown_blocks_nothing():
    """The seed is a starting state, not a veto. A loss two days back cannot
    refuse anything (wash-sale off here — that is the next test's claim), and a
    seed that blocked regardless would trade the old false verdict for a new one."""
    series = _two_days([100, 100, 100], [104, 104, 104])
    result = run_backtest(
        STRATEGY, {"ADA": series}, RISK_NO_WASH, starting_cash=5000, spread_pct=0,
        prior_loss_at={"ADA": datetime(2026, 5, 3, 14, 0, tzinfo=UTC)},
    )
    assert _entries(result) == 1


def test_a_stock_loss_weeks_old_still_trips_the_seeded_wash_sale_guard():
    """Same 48-hour-old loss as above, with the wash-sale guard left ON. It reads
    the same history over 31 days, so seeding the cooldown seeds it too — and a
    stock comparison would otherwise keep the identical blind spot for a month."""
    series = _two_days([100, 100, 100], [104, 104, 104])
    result = run_backtest(
        STRATEGY, {"ADA": series}, RISK, starting_cash=5000, spread_pct=0,
        prior_loss_at={"ADA": datetime(2026, 5, 3, 14, 0, tzinfo=UTC)},
    )
    assert _entries(result) == 0


def test_a_loss_inside_the_window_overrides_the_seed():
    """The seed is where the rail STARTS, not a floor under it.

    The seed here is two days old and blocks nothing. The replay buys at 104,
    stops out at 99 an hour later — a real loss, inside the window — and must be
    held off the 110 bar an hour after that. Keep the seed instead of replacing
    it and the second entry sails through on a 48-hour-old timestamp."""
    series = _two_days([100, 100, 100], [104, 99, 110])
    result = run_backtest(
        STRATEGY, {"ADA": series}, RISK_NO_WASH, starting_cash=5000, spread_pct=0,
        prior_loss_at={"ADA": datetime(2026, 5, 3, 14, 0, tzinfo=UTC)},
    )
    assert result["trades"] == 1  # the stop-out
    assert result["trade_list"][0]["pnl"] < 0
    assert _entries(result) == 1  # and no re-entry on the 110 bar


def test_a_naive_seed_is_read_as_utc_and_the_caller_s_dict_is_left_alone():
    """Two housekeeping claims that only bite in production. A seed read straight
    out of SQLite — or off a JSON request — is routinely naive, and subtracting
    it from an aware bar timestamp raises rather than blocks. And the simulation
    WRITES to this dict as it runs, so a caller replaying several segments off
    one seed must not find its own input rewritten by the first of them."""
    seed = {"ADA": datetime(2026, 5, 5, 2, 0)}  # naive
    series = _two_days([100, 100, 100], [104, 99, 110])
    result = run_backtest(
        STRATEGY, {"ADA": series}, RISK, starting_cash=5000, spread_pct=0,
        prior_loss_at=seed,
    )
    assert _entries(result) == 0  # read as UTC, so the cooldown applied
    assert seed == {"ADA": datetime(2026, 5, 5, 2, 0)}  # untouched by the run


def test_the_portfolio_backtester_takes_the_same_seed():
    """The rail is account-wide in the portfolio simulation too, so one dict
    keyed by symbol covers the whole book."""
    strategies = [{**STRATEGY, "id": 1, "name": "one"}]
    series = _two_days([100, 100, 100], [104, 104, 104])
    bars = {1: {"ADA": series}}

    clean = run_portfolio_backtest(strategies, bars, RISK, starting_cash=5000, spread_pct=0)
    assert _entries(clean) == 1

    seeded = run_portfolio_backtest(
        strategies, bars, RISK, starting_cash=5000, spread_pct=0,
        prior_loss_at={"ADA": datetime(2026, 5, 5, 2, 0, tzinfo=UTC)},
    )
    assert _entries(seeded) == 0


# --- gathering the seed from the journal -------------------------------------


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


def _strategy_body(symbols: list[str], name: str, min_gain: float = 3) -> dict:
    return {
        "name": name, "asset_class": "stock", "universe": "custom",
        "symbols": symbols, "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": min_gain, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }


def _loss(sid: int, symbol: str, exit_at: datetime, *, mode="paper", pnl=-40.0,
          status="closed", asset_class="stock") -> None:
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode=mode, symbol=symbol, asset_class=asset_class,
            status=status, qty=10, notional=1000, entry_price=100.0,
            exit_price=96.0, pnl=pnl, entry_reason="gain", exit_reason="stop-loss",
            entry_at=exit_at - timedelta(hours=2),
            exit_at=exit_at if status == "closed" else None,
        ))


def _prior(strategy_id: int, before: datetime, mode="paper", risk=None) -> dict:
    from qt.api.fidelity import _prior_losses

    with session_scope() as s:
        return _prior_losses(
            s, s.get(Strategy, strategy_id), before, mode, risk or RISK_DEFAULTS
        )


def test_the_seed_is_the_engine_s_own_definition_of_a_prior_loss(client, configured):
    """Copied from _build_rail_context rather than re-invented: the most recent
    losing CLOSED exit for that symbol, account-wide (ANY strategy), within this
    mode, strictly before the window. Seed it with anything else and the
    discrepancy has merely been moved somewhere harder to find."""
    mine = client.post("/api/strategies", json=_strategy_body(["RSA"], "seed mine")).json()["id"]
    other = client.post("/api/strategies", json=_strategy_body(["RSA"], "seed other")).json()["id"]
    start = NOW - timedelta(days=1)

    # ANOTHER strategy's loss, six hours before the window — the ADA case.
    _loss(other, "RSA", start - timedelta(hours=6))
    # An older loss on the same symbol: the seed must be the LATEST, not the first.
    _loss(other, "RSA", start - timedelta(hours=20))
    # A win, an open position, another mode, and a loss INSIDE the window: none
    # of these is a rail the engine was carrying when the window opened.
    _loss(mine, "RSWIN", start - timedelta(hours=3), pnl=12.0)
    _loss(mine, "RSOPEN", start - timedelta(hours=3), status="open")
    _loss(mine, "RSLIVE", start - timedelta(hours=3), mode="live")
    _loss(mine, "RSAFTER", start + timedelta(hours=1))

    seed = _prior(mine, start)
    assert set(seed) == {"RSA"}
    assert abs((seed["RSA"] - (start - timedelta(hours=6))).total_seconds()) < 2


def test_a_loss_too_old_to_block_anything_is_not_sent(client, configured):
    """A crypto strategy has one horizon: the after-loss cooldown. A loss older
    than that cannot refuse a single entry, and sending it would inflate what the
    report claims to have seeded."""
    sid = client.post(
        "/api/strategies",
        json={**_strategy_body(["RSC/USD"], "seed crypto"), "asset_class": "crypto"},
    ).json()["id"]
    start = NOW - timedelta(days=1)
    _loss(sid, "RSC/USD", start - timedelta(hours=30), asset_class="crypto")

    assert _prior(sid, start) == {}
    # The same loss inside a longer cooldown IS still in force, and is sent.
    seed = _prior(sid, start, risk=dict(RISK_DEFAULTS, cooldown_hours_after_loss=48))
    assert set(seed) == {"RSC/USD"}


def test_a_stock_seed_reaches_back_over_the_wash_sale_window(client, configured):
    """The wash-sale guard reads the same loss history over 31 days, so a stock
    strategy's horizon is the longer of the two — and shrinks back to the
    cooldown when the guard is switched off, because then nothing older matters."""
    sid = client.post("/api/strategies", json=_strategy_body(["RSW"], "seed wash")).json()["id"]
    start = NOW - timedelta(days=1)
    _loss(sid, "RSW", start - timedelta(days=10))

    assert set(_prior(sid, start)) == {"RSW"}
    assert _prior(sid, start, risk=dict(RISK_DEFAULTS, wash_sale_guard="off")) == {}
    # Beyond 31 days nothing survives either way.
    assert _prior(sid, start - timedelta(days=25)) == {}


# --- both replay paths -------------------------------------------------------


def _daily_bars(symbol_rise_days_ago: int, to_price: float) -> list[dict]:
    out = []
    for n in range(40, 0, -1):
        c = 100.0 if n > symbol_rise_days_ago else to_price
        ts = (NOW - timedelta(days=n)).replace(hour=14, minute=0, second=0, microsecond=0)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c, "h": c, "l": c,
                    "c": c, "v": 1000, "vw": c})
    return out


def _add_trade(sid: int, symbol: str, entry_days_ago: int) -> None:
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol=symbol, asset_class="stock",
            status="open", qty=10, notional=1000, entry_price=100.0,
            entry_reason="gain",
            entry_at=(NOW - timedelta(days=entry_days_ago)).replace(hour=14, minute=5),
        ))


def _backdate_versions(sid: int, ages: list[int]) -> None:
    with session_scope() as s:
        rows = (
            s.query(StrategyConfigVersion)
            .filter_by(strategy_id=sid)
            .order_by(StrategyConfigVersion.version_no)
            .all()
        )
        for row, age in zip(rows, ages):
            row.created_at = NOW - timedelta(days=age)


def _compare(client, sid: int, bars: dict[str, list[dict]], **extra) -> tuple[dict, list]:
    """The comparison, plus every `prior_loss_at` the simulator was actually
    handed. Spying on the simulator rather than trusting the report is the point:
    the report is built from what the REPLAY echoed back, and this checks the
    seed reached the thing that has to act on it."""
    seen: list = []
    real = bt.run_backtest

    def spy(*args, **kwargs):
        seen.append(kwargs.get("prior_loss_at"))
        return real(*args, **kwargs)

    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)), \
            patch.object(bt, "run_backtest", side_effect=spy):
        response = client.post(
            "/api/fidelity/compare", json={"strategy_id": sid, **extra}
        )
    assert response.status_code == 200, response.text
    return response.json(), seen


def test_an_unsegmented_comparison_seeds_the_rail_and_says_so(client, configured):
    """One replay, one seed. RSU rose eight days ago and this strategy bought it;
    another strategy had closed a loss on RSU two hours before the window opened,
    so the live engine was still cooling off on it and the replay must start
    knowing that."""
    sid = client.post("/api/strategies", json=_strategy_body(["RSU"], "seed unseg")).json()["id"]
    other = client.post("/api/strategies", json=_strategy_body(["RSU"], "seed unseg other")).json()["id"]
    _add_trade(sid, "RSU", entry_days_ago=8)
    window_opens = (NOW - timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    _loss(other, "RSU", window_opens - timedelta(hours=2))

    body, seen = _compare(client, sid, {"RSU": _daily_bars(8, 108.0)})

    assert body["config"]["segmented"] is False
    assert [s["symbol"] for s in body["rails"]["seeded_losses"]] == ["RSU"]
    assert body["rails"]["cooldown_hours_after_loss"] == RISK_DEFAULTS["cooldown_hours_after_loss"]
    assert seen and all("RSU" in (s or {}) for s in seen)


def test_a_segmented_comparison_seeds_every_stretch_from_its_own_start(client, configured):
    """Two replay paths, and missing one is the classic bug here.

    A segmented comparison replays each stretch separately, so each needs the
    rail state at ITS OWN start — not the whole window's. The loss here lands
    INSIDE the window and before the second stretch: a seed gathered once at the
    window's opening would not contain it at all."""
    sid = client.post("/api/strategies", json=_strategy_body(["RSS"], "seed seg")).json()["id"]
    client.put(f"/api/strategies/{sid}", json=_strategy_body(["RSS"], "seed seg", min_gain=6))
    _backdate_versions(sid, [25, 12])
    _add_trade(sid, "RSS", entry_days_ago=20)
    _add_trade(sid, "RSS2", entry_days_ago=6)
    other = client.post("/api/strategies", json=_strategy_body(["RSS"], "seed seg other")).json()["id"]
    lost_at = NOW - timedelta(days=12, hours=3)   # inside the window, before the cut
    _loss(other, "RSS", lost_at)

    body, seen = _compare(client, sid, {"RSS": _daily_bars(20, 108.0), "RSS2": _daily_bars(6, 108.0)})

    assert body["config"]["segmented"] is True
    seeded = body["rails"]["seeded_losses"]
    assert [s["symbol"] for s in seeded] == ["RSS"]
    assert seeded[0]["last_loss_at"][:13] == lost_at.isoformat()[:13]
    # The FIRST stretch opened before that loss and must not carry it; the second
    # must. A single window-wide seed cannot produce both.
    # Chunk-agnostic: a long window is now cut into day-sized pieces so it can
    # keep minute bars (_chunk_for_minute_replay), so the number of replays is
    # not the claim. The claim is that the FIRST stretch carries no seed and
    # every stretch after the loss carries it.
    got = [sorted(s or {}) for s in seen]
    assert got[0] == []
    assert got[-1] == ["RSS"]
    assert any(g == ["RSS"] for g in got), got


def test_the_report_only_claims_a_seed_the_replay_confirms(client, configured):
    """The honesty of this report has been fought for repeatedly, and a section
    that says "I seeded the rails" is worth nothing if it is built from what this
    function MEANT to send. It is read back off the replay's own echo instead, so
    a replay path that quietly drops the seed empties the claim rather than
    leaving it standing.

    Simulated here by a replay that returns no echo at all — which is exactly
    what an unwired path would do."""
    from qt.api import fidelity as fidelity_api

    sid = client.post("/api/strategies", json=_strategy_body(["RSH"], "seed honest")).json()["id"]
    other = client.post("/api/strategies", json=_strategy_body(["RSH"], "seed honest other")).json()["id"]
    _add_trade(sid, "RSH", entry_days_ago=8)
    opens = (NOW - timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    _loss(other, "RSH", opens - timedelta(hours=2))

    real = fidelity_api.run

    async def forgetful(*args, **kwargs):
        result = await real(*args, **kwargs)
        result.pop("rails_seeded", None)
        return result

    with patch.object(fidelity_api, "run", side_effect=forgetful):
        body, seen = _compare(client, sid, {"RSH": _daily_bars(8, 108.0)})

    assert seen and all("RSH" in (s or {}) for s in seen)  # it really was sent
    assert body["rails"]["seeded_losses"] == []            # and not claimed anyway


@pytest.fixture()
def seeded_cache(monkeypatch):
    """A bar cache holding two recent days and one mover, so a scanner replay has
    something to replay. Same recipe as test_backtest_api's."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from qt.services import barcache

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (NOW - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (NOW - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        barcache.save_daily_bars(s, "RSMOVER", [
            {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
            {"t": f"{d1}T14:00:00Z", "o": 105, "h": 105, "l": 105, "c": 105, "v": 1e6, "vw": 105},
        ])
        barcache.store_movers(s, d1, [("RSMOVER", 5.0, 105.0, 1e8)])
        s.commit()
    return d1


def test_the_scanner_replay_path_honours_the_seed_too(client, configured, seeded_cache):
    """The other replay path, and the one the reported ADA/USD case actually went
    down — strategy 26 is a scanner strategy. A seed wired into the plain replay
    and not into this one would fix the report for exactly the strategies that
    never needed it."""
    body = {**_strategy_body([], "seed scanner"), "universe": "scanner"}
    body.pop("symbols")
    sid = client.post("/api/strategies", json=body).json()["id"]
    request = {"strategy_id": sid, "scanner_replay": True, "days": 30,
               "starting_cash": 5000, "spread_pct": 0}
    fetch = AsyncMock(side_effect=Exception("no benchmark in test"))  # SPY is best-effort

    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        clean = client.post("/api/backtest", json=request).json()
        seeded = client.post("/api/backtest", json={
            **request, "prior_loss_at": {"RSMOVER": f"{seeded_cache}T06:00:00Z"},
        }).json()

    assert clean["trades"] + len(clean["open_positions"]) == 1
    assert clean["rails_seeded"] == []
    # Eight hours before the bar it would have bought on: still cooling off.
    assert seeded["trades"] + len(seeded["open_positions"]) == 0
    assert seeded["rails_seeded"] == ["RSMOVER"]


def test_imported_trades_are_never_seeded_from_this_machine_s_journal(client, configured):
    """They came from another instance, so this journal is not the history that
    produced them. Seeding from it would manufacture cooldowns the exporting
    account never had — and the report would present the invention as fidelity."""
    sid = client.post("/api/strategies", json=_strategy_body(["RSI"], "seed imported")).json()["id"]
    other = client.post("/api/strategies", json=_strategy_body(["RSI"], "seed imported other")).json()["id"]
    day = (NOW - timedelta(days=8)).strftime("%Y-%m-%d")
    _loss(other, "RSI", NOW - timedelta(days=30))

    body, seen = _compare(
        client, sid, {"RSI": _daily_bars(8, 108.0)},
        window_start=(NOW - timedelta(days=20)).isoformat(),
        window_end=NOW.isoformat(),
        imported_trades=[{
            "symbol": "RSI", "status": "open", "entry_day": day, "exit_day": None,
            "entry_price": 100.0, "exit_price": None, "pnl": None,
            "entry_reason": "gain", "exit_reason": "",
        }],
    )

    assert body["imported"] is True
    assert body["rails"]["seeded_losses"] == []
    assert seen and all(not (s or {}) for s in seen)
