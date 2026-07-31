import os

from qt.db import session_scope
from qt.settings_service import set_setting


def _reset(client):
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
        set_setting(s, "risk_config", {})


def test_engine_state_defaults(client):
    _reset(client)
    body = client.get("/api/engine").json()
    assert body["mode"] == "off"
    assert body["risk"]["leverage_enabled"] is False
    assert body["leverage"]["unlockable"] is False


def test_mode_validation(client):
    _reset(client)
    assert client.post("/api/engine/mode", json={"mode": "warp"}).status_code == 422
    # paper requires confirmation
    assert client.post("/api/engine/mode", json={"mode": "paper"}).status_code == 428
    # paper requires an enabled strategy
    assert client.post("/api/engine/mode", json={"mode": "paper", "confirm": True}).status_code == 409
    # shadow is allowed freely
    assert client.post("/api/engine/mode", json={"mode": "shadow"}).status_code == 200
    assert client.get("/api/engine").json()["mode"] == "shadow"
    _reset(client)


def _risk_payload(**overrides):
    payload = {
        "max_daily_loss_usd": 150,
        "max_daily_loss_pct": 4,
        "max_total_positions": 5,
        "max_total_exposure_usd": 2500,
        "max_trades_per_day": 8,
        "cooldown_hours_after_loss": 12,
        "wash_sale_guard": "block",
        "leverage_enabled": False,
        "leverage_confirm": "",
    }
    payload.update(overrides)
    return payload


def test_risk_update_roundtrip(client):
    _reset(client)
    resp = client.put("/api/engine/risk", json=_risk_payload())
    assert resp.status_code == 200
    assert resp.json()["max_trades_per_day"] == 8


def test_leverage_blocked_without_env_var(client):
    _reset(client)
    os.environ.pop("QT_ALLOW_LEVERAGE", None)
    resp = client.put("/api/engine/risk", json=_risk_payload(leverage_enabled=True))
    assert resp.status_code == 403
    assert "locked at the server level" in resp.json()["detail"]


def test_leverage_with_env_var_needs_typed_confirmation(client):
    _reset(client)
    os.environ["QT_ALLOW_LEVERAGE"] = "true"
    try:
        resp = client.put("/api/engine/risk", json=_risk_payload(leverage_enabled=True))
        assert resp.status_code == 428  # visible but still needs the phrase

        resp = client.put(
            "/api/engine/risk",
            json=_risk_payload(leverage_enabled=True, leverage_confirm="I ACCEPT AMPLIFIED LOSSES"),
        )
        assert resp.status_code == 200
        assert resp.json()["leverage_enabled"] is True

        # engine state reflects it while unlocked
        assert client.get("/api/engine").json()["leverage"]["enabled"] is True
    finally:
        os.environ.pop("QT_ALLOW_LEVERAGE", None)
        _reset(client)


def test_leverage_setting_ignored_once_env_removed(client):
    """Even if leverage was enabled while unlocked, removing the env var re-locks it."""
    _reset(client)
    os.environ["QT_ALLOW_LEVERAGE"] = "true"
    client.put(
        "/api/engine/risk",
        json=_risk_payload(leverage_enabled=True, leverage_confirm="I ACCEPT AMPLIFIED LOSSES"),
    )
    os.environ.pop("QT_ALLOW_LEVERAGE", None)
    body = client.get("/api/engine").json()
    assert body["risk"]["leverage_enabled"] is False
    assert body["leverage"]["unlockable"] is False
    _reset(client)


def test_slack_url_validation(client):
    assert client.put("/api/engine/slack", json={"url": "https://evil.example.com/x"}).status_code == 422
    assert client.put("/api/engine/slack", json={"url": "https://hooks.slack.com/services/T00/B00/xyz"}).status_code == 200
    assert client.put("/api/engine/slack", json={"url": ""}).status_code == 200


def test_strategy_pnl_breaks_down_realized_by_strategy(client):
    from datetime import datetime, timezone

    from qt.models import Strategy, Trade

    with session_scope() as s:
        set_setting(s, "engine_mode", "paper")
        s.query(Trade).delete()
        s.query(Strategy).delete()
        a = Strategy(name="Alpha", asset_class="stock", params="{}")
        b = Strategy(name="Beta", asset_class="stock", params="{}")
        s.add_all([a, b])
        s.flush()
        now = datetime.now(timezone.utc)

        def closed(sid, sym, pnl):
            return Trade(strategy_id=sid, mode="paper", symbol=sym, asset_class="stock",
                         status="closed", qty=1, notional=100, pnl=pnl, exit_at=now)

        s.add_all([
            closed(a.id, "X", 30), closed(a.id, "Y", 20), closed(a.id, "Z", -10),  # Alpha: +40, 2/3 win
            closed(b.id, "W", -25),                                                  # Beta: -25
            Trade(strategy_id=b.id, mode="paper", symbol="V", asset_class="stock", status="open", qty=1, notional=100),
            closed(a.id, "S", 999),  # will be overwritten to shadow below
        ])
        s.flush()
        # A shadow-mode trade must be ignored (different mode than active "paper").
        s.query(Trade).filter(Trade.symbol == "S").update({"mode": "shadow"})
        s.commit()

    body = client.get("/api/engine/strategy-pnl").json()
    by = {r["name"]: r for r in body["strategies"]}
    assert body["mode"] == "paper"
    assert by["Alpha"]["realized_pnl"] == 40.0 and by["Alpha"]["trades"] == 3 and by["Alpha"]["wins"] == 2
    assert by["Alpha"]["win_rate"] == round(2 / 3, 4)
    assert by["Beta"]["realized_pnl"] == -25.0 and by["Beta"]["open_positions"] == 1
    assert body["realized_total"] == 15.0            # 40 + (-25); the shadow +999 is excluded
    assert body["strategies"][0]["name"] == "Alpha"  # sorted best-first

    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        set_setting(s, "engine_mode", "off")


def test_strategy_pnl_daily_buckets_realized_by_exit_date(client):
    from datetime import datetime

    from qt.models import Strategy, Trade

    with session_scope() as s:
        set_setting(s, "engine_mode", "paper")
        s.query(Trade).delete()
        s.query(Strategy).delete()
        a = Strategy(name="Alpha", asset_class="stock", params="{}")
        b = Strategy(name="Beta", asset_class="stock", params="{}")
        s.add_all([a, b])
        s.flush()

        def closed(sid, sym, pnl, day):
            return Trade(strategy_id=sid, mode="paper", symbol=sym, asset_class="stock",
                         status="closed", qty=1, notional=100, pnl=pnl,
                         exit_at=datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 15, 0))

        s.add_all([
            closed(a.id, "X", 10, "2026-07-20"),
            closed(a.id, "Y", 5, "2026-07-21"),
            closed(b.id, "Z", -8, "2026-07-20"),
            Trade(strategy_id=a.id, mode="paper", symbol="O", asset_class="stock", status="open", qty=1, notional=100),
        ])
        s.commit()

    body = client.get("/api/engine/strategy-pnl-daily?days=3650").json()
    assert body["days"] == ["2026-07-20", "2026-07-21"]  # only days with closed trades
    by = {r["name"]: r for r in body["strategies"]}
    assert by["Alpha"]["values"] == [10.0, 5.0] and by["Alpha"]["total"] == 15.0
    assert by["Beta"]["values"] == [-8.0, 0.0] and by["Beta"]["total"] == -8.0
    assert body["strategies"][0]["name"] == "Alpha"  # sorted by total, best first

    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        set_setting(s, "engine_mode", "off")


def test_strategy_pnl_daily_window_cutoff_and_all_time(client):
    from datetime import datetime, timedelta, timezone

    from qt.models import Strategy, Trade

    now = datetime.now(timezone.utc)
    old_day = (now - timedelta(days=100)).strftime("%Y-%m-%d")   # outside a 30-day window
    recent_day = (now - timedelta(days=2)).strftime("%Y-%m-%d")  # inside it

    with session_scope() as s:
        set_setting(s, "engine_mode", "paper")
        s.query(Trade).delete()
        s.query(Strategy).delete()
        a = Strategy(name="Alpha", asset_class="stock", params="{}")
        s.add(a)
        s.flush()

        def closed(pnl, day):
            return Trade(strategy_id=a.id, mode="paper", symbol="X", asset_class="stock",
                         status="closed", qty=1, notional=100, pnl=pnl,
                         exit_at=datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 15, 0))

        s.add_all([closed(10, old_day), closed(4, recent_day)])
        s.commit()

    # 30-day window: only the recent day; the older trade is excluded by the cutoff.
    win = client.get("/api/engine/strategy-pnl-daily?days=30").json()
    assert win["days"] == [recent_day]
    assert win["strategies"][0]["total"] == 4.0

    # All time (days=0): no cutoff — both days appear.
    allt = client.get("/api/engine/strategy-pnl-daily?days=0").json()
    assert allt["days"] == sorted([old_day, recent_day])
    assert allt["strategies"][0]["total"] == 14.0

    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        set_setting(s, "engine_mode", "off")


# ---------------------------------------------------------------------------
# Unrealized P&L per strategy: the open positions marked to live quotes.
#
# The rule these pin: a missing mark is NOT a zero mark. A quote hiccup that
# silently reports $0.00 looks exactly like a position sitting at break-even,
# and that is a wrong number presented as a right one.
# ---------------------------------------------------------------------------


def _seed_open_and_closed(client):
    from datetime import datetime, timezone

    from qt import security
    from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
    from qt.models import Strategy, Trade

    with session_scope() as s:
        # Without keys get_client() returns None and NOTHING can be priced — the
        # marking path under test would never run.
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "paper")
        s.query(Trade).delete()
        s.query(Strategy).delete()
        a = Strategy(name="Alpha", asset_class="stock", params="{}")
        b = Strategy(name="Beta", asset_class="stock", params="{}")
        s.add_all([a, b])
        s.flush()
        now = datetime.now(timezone.utc)
        s.add_all([
            Trade(strategy_id=a.id, mode="paper", symbol="X", asset_class="stock",
                  status="closed", qty=1, notional=100, pnl=40.0, exit_at=now),
            # Alpha holds 2 NVDA bought at 100 → +$40 if NVDA marks at 120.
            Trade(strategy_id=a.id, mode="paper", symbol="NVDA", asset_class="stock",
                  status="open", qty=2, notional=200, entry_price=100.0, entry_at=now),
            # Beta holds 1 MSFT bought at 400 → −$50 if MSFT marks at 350.
            Trade(strategy_id=b.id, mode="paper", symbol="MSFT", asset_class="stock",
                  status="open", qty=1, notional=400, entry_price=400.0, entry_at=now),
        ])
        s.commit()
        return a.id, b.id


def _cleanup():
    from qt import security
    from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
    from qt.models import Strategy, Trade

    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        set_setting(s, "engine_mode", "off")
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def test_unrealized_marks_each_strategys_open_positions(client):
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    _seed_open_and_closed(client)
    snaps = {
        "NVDA": {"latestTrade": {"p": 120.0}},
        "MSFT": {"latestTrade": {"p": 350.0}},
    }
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=snaps)):
        body = client.get("/api/engine/strategy-pnl").json()
    by = {r["name"]: r for r in body["strategies"]}
    assert by["Alpha"]["unrealized_pnl"] == 40.0    # (120-100) x 2
    assert by["Beta"]["unrealized_pnl"] == -50.0    # (350-400) x 1
    assert body["unrealized_total"] == -10.0
    assert body["unpriced_positions"] == 0
    # Realized is untouched and still separate — never folded into one number.
    assert by["Alpha"]["realized_pnl"] == 40.0 and body["realized_total"] == 40.0
    _cleanup()


def test_a_position_with_no_price_is_unknown_not_zero(client):
    """The one that matters. If the quote call fails, unrealized must read as
    UNKNOWN — reporting $0.00 would be indistinguishable from break-even."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    _seed_open_and_closed(client)
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(side_effect=RuntimeError("quotes down"))):
        body = client.get("/api/engine/strategy-pnl").json()
    by = {r["name"]: r for r in body["strategies"]}
    assert by["Alpha"]["unrealized_pnl"] is None
    assert by["Beta"]["unrealized_pnl"] is None
    assert by["Alpha"]["unpriced_positions"] == 1
    assert body["unpriced_positions"] == 2
    _cleanup()


def test_a_strategy_holding_nothing_is_flat_not_unknown(client):
    """The flip side: no open positions genuinely IS $0.00 unrealized, and must
    not be dressed up as missing data."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    _seed_open_and_closed(client)
    with session_scope() as s:
        from qt.models import Trade

        s.query(Trade).filter(Trade.status == "open").delete()
        s.commit()
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value={})):
        body = client.get("/api/engine/strategy-pnl").json()
    by = {r["name"]: r for r in body["strategies"]}
    assert by["Alpha"]["unrealized_pnl"] == 0.0
    assert by["Alpha"]["open_positions"] == 0
    assert body["unrealized_total"] == 0.0
    _cleanup()


def test_a_partial_mark_is_reported_as_partial(client):
    """One symbol priced, one not: the total is a floor and says so, rather than
    quietly under-reporting."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    _seed_open_and_closed(client)
    with patch.object(AlpacaClient, "stock_snapshots",
                      new=AsyncMock(return_value={"NVDA": {"latestTrade": {"p": 120.0}}})):
        body = client.get("/api/engine/strategy-pnl").json()
    by = {r["name"]: r for r in body["strategies"]}
    assert by["Alpha"]["unrealized_pnl"] == 40.0
    assert by["Beta"]["unrealized_pnl"] is None and by["Beta"]["unpriced_positions"] == 1
    assert body["unpriced_positions"] == 1
    _cleanup()


def test_shadow_positions_are_not_marked_into_paper_totals(client):
    """Unrealized follows the active mode, exactly like realized does."""
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from qt import security
    from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
    from qt.models import Strategy, Trade

    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "paper")
        s.query(Trade).delete()
        s.query(Strategy).delete()
        a = Strategy(name="Alpha", asset_class="stock", params="{}")
        s.add(a)
        s.flush()
        s.add(Trade(strategy_id=a.id, mode="shadow", symbol="NVDA", asset_class="stock",
                    status="open", qty=2, notional=200, entry_price=100.0,
                    entry_at=datetime.now(timezone.utc)))
        s.commit()
    with patch.object(AlpacaClient, "stock_snapshots",
                      new=AsyncMock(return_value={"NVDA": {"latestTrade": {"p": 120.0}}})):
        body = client.get("/api/engine/strategy-pnl").json()
    assert body["unrealized_total"] == 0.0
    _cleanup()
