"""Scoreboard scoping. Equity is only comparable WITHIN a broker account, so the
series is filtered to one account and rebased on that account's first day.

The bug this pins: snapshots had no account column and every point normalised
against the first row ever recorded, so replacing a paper account made the
balance step read as a catastrophic trading loss (a real case showed −80% on the
switch day)."""

from qt.db import session_scope
from qt.models import BenchmarkSnapshot
from qt.services import scoreboard
from qt.settings_service import set_setting


def _seed(rows: list[tuple[str, float, float, str | None]]) -> None:
    """(day, bot_equity, spy_close, account_id) — spy moves 1% a day so the
    benchmark is never the thing under test."""
    with session_scope() as s:
        s.query(BenchmarkSnapshot).delete()
        for day, equity, spy, acct in rows:
            s.add(BenchmarkSnapshot(day=day, bot_equity=equity, spy_close=spy, account_id=acct))


def set_setting_account(acct: str) -> None:
    with session_scope() as s:
        set_setting(s, "current_account_id", acct)


def test_account_switch_no_longer_reads_as_a_crash(client):
    # OLD account sat at $100k. NEW account starts at $20k — a 5x smaller
    # balance, not a loss. Unscoped, the last point would be (20000/100000-1)
    # = −80%: exactly the phantom cliff users saw.
    _seed([
        ("2026-07-26", 100_000.0, 500.0, "OLD"),
        ("2026-07-27", 101_000.0, 505.0, "OLD"),
        ("2026-07-28", 20_000.0, 510.0, "NEW"),
        ("2026-07-29", 20_400.0, 515.0, "NEW"),
    ])
    with session_scope() as s:
        set_setting(s, "current_account_id", "NEW")

    body = client.get("/api/engine/scoreboard").json()
    assert body["days"] == ["2026-07-28", "2026-07-29"]  # only the current account
    assert body["bot"] == [0.0, 2.0]  # rebased on ITS first day: +2%, not −80%
    assert body["account"] == "NEW"
    assert "beating" in (body["verdict"] or "")

    # The benchmark rebases on the SAME first day, so all lines start at 0.
    assert body["spy"][0] == 0.0


def test_all_accounts_view_still_available_and_still_cross_account(client):
    # 'all' reproduces the legacy, misleading behaviour on purpose — it exists
    # for completeness, not as the default.
    _seed([
        ("2026-07-26", 100_000.0, 500.0, "OLD"),
        ("2026-07-28", 20_000.0, 510.0, "NEW"),
    ])
    with session_scope() as s:
        set_setting(s, "current_account_id", "NEW")
    body = client.get("/api/engine/scoreboard?account=all").json()
    assert body["days"] == ["2026-07-26", "2026-07-28"]
    assert body["bot"][-1] == -80.0  # the phantom cliff, visible only when asked for


def test_untagged_legacy_rows_are_reachable(client):
    _seed([
        ("2026-07-20", 50_000.0, 480.0, None),
        ("2026-07-28", 20_000.0, 510.0, "NEW"),
    ])
    with session_scope() as s:
        set_setting(s, "current_account_id", "NEW")
    body = client.get("/api/engine/scoreboard?account=untagged").json()
    assert body["days"] == ["2026-07-20"]


def test_current_account_with_no_snapshots_is_empty_not_wrong(client):
    # Just switched: the new account has no history yet. An empty chart is the
    # honest answer — never fall back to another account's numbers.
    _seed([("2026-07-26", 100_000.0, 500.0, "OLD")])
    with session_scope() as s:
        set_setting(s, "current_account_id", "BRAND_NEW")
    body = client.get("/api/engine/scoreboard").json()
    assert body["days"] == [] and body["bot"] == []
    assert body["verdict"] is None


def test_untagged_history_of_the_same_account_is_kept(client):
    """The real deployment case: snapshots only started carrying an account in
    migration 0008, so an account running for days has ONE tagged row (today) and
    the rest untagged. Strict scoping showed a one-point chart. The untagged rows
    that are CONTINUOUS in equity belong to this account and are kept; the ones
    across the switch step are not."""
    _seed([
        ("2026-07-26", 100_000.0, 500.0, None),  # old account, untagged
        ("2026-07-27", 101_000.0, 505.0, None),  # old account, untagged
        ("2026-07-28", 20_000.0, 510.0, None),   # ← the switch: -80% step
        ("2026-07-29", 20_400.0, 515.0, None),   # new account, untagged
        ("2026-07-30", 20_600.0, 520.0, "NEW"),  # new account, tagged today
    ])
    with session_scope() as s:
        set_setting(s, "current_account_id", "NEW")

    body = client.get("/api/engine/scoreboard").json()
    # Walks back through the contiguous run and stops AT the switch.
    assert body["days"] == ["2026-07-28", "2026-07-29", "2026-07-30"]
    assert body["bot"] == [0.0, 2.0, 3.0]  # rebased on the new account's first day
    assert body["bot"][-1] > 0  # no phantom crash


def test_untagged_history_stops_at_a_different_tagged_account(client):
    # An explicitly OLD-tagged row is a hard stop — never walk past it.
    _seed([
        ("2026-07-27", 20_100.0, 505.0, "OLD"),
        ("2026-07-28", 20_200.0, 510.0, None),
        ("2026-07-29", 20_400.0, 515.0, "NEW"),
    ])
    with session_scope() as s:
        set_setting(s, "current_account_id", "NEW")
    body = client.get("/api/engine/scoreboard").json()
    assert body["days"] == ["2026-07-28", "2026-07-29"]  # stopped before OLD


# ---------------------------------------------------------------------------
# Trades per day.
#
# The scoreboard chart was given NO trade data at all, yet the hover panel still
# printed "no trades this day" — on every day, including days the bot traded.
# The series now carries the day's buys and sells so the claim is answerable.
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from qt.models import Strategy, Trade  # noqa: E402


def _seed_trade(**kw) -> None:
    with session_scope() as s:
        strat = s.query(Strategy).filter(Strategy.name == "T").one_or_none()
        if strat is None:
            strat = Strategy(
                name="T", asset_class="stock", universe="custom", preset="custom",
                params="{}", symbols="[]",
            )
            s.add(strat)
            s.flush()
        s.add(Trade(strategy_id=strat.id, asset_class="stock", **kw))


def _clear_trades() -> None:
    with session_scope() as s:
        s.query(Trade).delete()


def test_a_day_with_trades_reports_them(client):
    _clear_trades()
    _seed([("2026-07-29", 10_000.0, 500.0, "A"), ("2026-07-30", 10_100.0, 505.0, "A")])
    set_setting_account("A")
    _seed_trade(
        mode="paper", status="closed", account_id="A", symbol="AAPL", qty=3,
        entry_price=100.0, exit_price=110.0, pnl=30.0,
        entry_at=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        exit_at=datetime(2026, 7, 30, 18, 30, tzinfo=timezone.utc),
    )
    with session_scope() as s:
        out = scoreboard.series(s)
    assert [t["kind"] for t in out["trades"]["2026-07-29"]] == ["buy"]
    sell = out["trades"]["2026-07-30"][0]
    assert sell["kind"] == "sell" and sell["symbol"] == "AAPL" and sell["pnl"] == 30.0


def test_a_late_evening_trade_lands_on_the_right_day(client):
    """The day key must match BenchmarkSnapshot's UTC day. Computed in the
    browser instead, a 23:30Z trade would slide a day in most timezones and be
    attributed to the wrong equity step."""
    _clear_trades()
    _seed([("2026-07-29", 10_000.0, 500.0, "A"), ("2026-07-30", 10_100.0, 505.0, "A")])
    set_setting_account("A")
    _seed_trade(
        mode="paper", status="open", account_id="A", symbol="MSFT", qty=1, entry_price=400.0,
        entry_at=datetime(2026, 7, 30, 23, 30, tzinfo=timezone.utc),
    )
    with session_scope() as s:
        out = scoreboard.series(s)
    assert "2026-07-30" in out["trades"]
    assert "2026-07-31" not in out["trades"]


def test_another_accounts_trades_stay_off_this_line(client):
    """The equity line is account-scoped; its trades must be too, or the chart
    explains this account's moves with another account's trades."""
    _clear_trades()
    _seed([("2026-07-30", 10_000.0, 500.0, "A")])
    set_setting_account("A")
    _seed_trade(mode="paper", status="open", account_id="B", symbol="NVDA", qty=1,
                entry_price=100.0, entry_at=datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc))
    with session_scope() as s:
        out = scoreboard.series(s)
    assert out["trades"] == {}


def test_shadow_trades_are_not_claimed_as_real(client):
    """Shadow mode places no orders, so it never moved this equity line."""
    _clear_trades()
    _seed([("2026-07-30", 10_000.0, 500.0, "A")])
    set_setting_account("A")
    _seed_trade(mode="shadow", status="open", account_id="A", symbol="TSLA", qty=1,
                entry_price=100.0, entry_at=datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc))
    with session_scope() as s:
        out = scoreboard.series(s)
    assert out["trades"] == {}


def test_a_genuinely_quiet_day_reports_no_trades(client):
    """The flip side: 'no trades' must still be sayable when it's true."""
    _clear_trades()
    _seed([("2026-07-30", 10_000.0, 500.0, "A")])
    set_setting_account("A")
    with session_scope() as s:
        out = scoreboard.series(s)
    assert out["trades"] == {}
