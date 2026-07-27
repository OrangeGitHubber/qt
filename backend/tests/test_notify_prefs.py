"""Opt-in Slack message categories: the preference model, the category gate,
the summary jobs' gating, and the prefs API."""

import asyncio
from datetime import datetime, timedelta, timezone

from qt.db import session_scope
from qt.services import notify
from qt.settings_service import set_setting


def _reset_prefs():
    with session_scope() as s:
        set_setting(s, notify.PREFS_KEY, {})
        set_setting(s, "engine_mode", "off")
        s.commit()


def test_notify_prefs_merges_defaults_under_saved_values():
    with session_scope() as s:
        set_setting(s, notify.PREFS_KEY, {"weekly_summary": True, "trade_confirmations": False})
        s.commit()
        prefs = notify.notify_prefs(s)
        assert prefs["weekly_summary"] is True          # saved value wins
        assert prefs["trade_confirmations"] is False     # saved value wins
        assert prefs["daily_summary"] is True            # untouched -> catalog default
    _reset_prefs()


def test_set_notify_prefs_ignores_unknown_keys():
    with session_scope() as s:
        notify.set_notify_prefs(s, {"bogus": True, "reconciliation": False})
        s.commit()
        prefs = notify.notify_prefs(s)
        assert "bogus" not in prefs
        assert prefs["reconciliation"] is False
    _reset_prefs()


def test_slack_cat_only_sends_enabled_categories(monkeypatch):
    sent = []

    async def _fake_slack(session, text):
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "slack", _fake_slack)
    with session_scope() as s:
        set_setting(s, notify.PREFS_KEY, {"trade_confirmations": False, "reconciliation": True})
        s.commit()
        assert asyncio.run(notify.slack_cat(s, "trade_confirmations", "buy")) is False  # muted
        assert asyncio.run(notify.slack_cat(s, "reconciliation", "orphan")) is True     # sent
    assert sent == ["orphan"]
    _reset_prefs()


def test_slack_prefs_api_get_and_put(client):
    body = client.get("/api/engine/slack/prefs").json()
    keys = {c["key"] for c in body["categories"]}
    assert {"trade_confirmations", "daily_summary", "weekly_summary", "reconciliation"} <= keys
    for c in body["categories"]:  # every entry is self-describing for the UI
        assert c["label"] and c["description"] and isinstance(c["enabled"], bool)

    put = client.put("/api/engine/slack/prefs", json={"prefs": {"weekly_summary": True}})
    assert put.status_code == 200 and put.json()["prefs"]["weekly_summary"] is True
    again = {c["key"]: c["enabled"] for c in client.get("/api/engine/slack/prefs").json()["categories"]}
    assert again["weekly_summary"] is True
    assert again["daily_summary"] is True  # unspecified key untouched
    client.put("/api/engine/slack/prefs", json={"prefs": {"weekly_summary": False}})


def test_weekly_summary_gated_by_its_category(monkeypatch):
    from qt.services import jobs

    sent = []

    async def _fake_slack(session, text):
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "slack", _fake_slack)

    # OFF -> nothing sent
    with session_scope() as s:
        set_setting(s, "engine_mode", "paper")
        set_setting(s, notify.PREFS_KEY, {"weekly_summary": False})
        s.commit()
    asyncio.run(jobs.weekly_summary())
    assert sent == []

    # ON -> one recap posted
    with session_scope() as s:
        set_setting(s, notify.PREFS_KEY, {"weekly_summary": True})
        s.commit()
    asyncio.run(jobs.weekly_summary())
    assert len(sent) == 1 and "weekly summary" in sent[0]
    _reset_prefs()


def test_strategy_breakdown_lines_rank_best_first():
    from qt.models import Strategy, Trade
    from qt.services.jobs import _strategy_breakdown_lines

    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        a = Strategy(name="Alpha", asset_class="stock", params="{}")
        b = Strategy(name="Beta", asset_class="stock", params="{}")
        s.add_all([a, b])
        s.flush()
        now = datetime.now(timezone.utc)
        s.add_all([
            Trade(strategy_id=a.id, mode="paper", symbol="X", asset_class="stock",
                  status="closed", qty=1, notional=1, pnl=10.0, exit_at=now),
            Trade(strategy_id=b.id, mode="paper", symbol="Y", asset_class="stock",
                  status="closed", qty=1, notional=1, pnl=-4.0, exit_at=now),
        ])
        s.commit()
        lines = _strategy_breakdown_lines(s, "paper", now - timedelta(days=1))
        assert "Alpha" in lines and "Beta" in lines
        assert lines.index("Alpha") < lines.index("Beta")  # positive P&L ranked first
        assert _strategy_breakdown_lines(s, "paper", now + timedelta(days=1)) == ""  # window with no trades
        s.query(Trade).delete()
        s.query(Strategy).delete()
        s.commit()
