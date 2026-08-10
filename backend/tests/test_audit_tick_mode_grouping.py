"""The tick runs ONE PASS PER MODE, and the passes must not leak into each other.

Per-strategy mode is only real if the engine acts on it. `effective_mode` is
pure and tested next door; this file tests that `tick` actually groups by it.

WHY SEPARATE PASSES RATHER THAN ONE LOOP WITH A MODE ON EACH STRATEGY. Every
account-wide rail — the daily-loss kill switch, the exposure cap, the trade-rate
limiter, the after-loss cooldown — is a query filtered on `Trade.mode`. Those
queries take the pass's mode, not the individual strategy's. Run the modes
together and a paper strategy's losses eat a live strategy's daily-loss budget,
or the reverse, and the rails would be quietly wrong in a way no single test of
any one rail would catch. Separate passes are what makes the partition true
rather than merely likely.

EXITS ARE KEYED ON THE TRADE'S OWN MODE, not on any strategy's. A strategy
demoted from paper to shadow still has open paper positions with real stops
against them; if exits only ran for modes that currently have enabled
strategies, those positions would be abandoned unwatched. That asymmetry between
the entry pass and the exit pass is deliberate and is asserted below.

NOTE ON COVERAGE: the pre-existing suite sets `engine_mode = "paper"` in eight
places but builds its Trade rows directly, so none of it reaches the grouping.
A green suite was not evidence that any of this worked.
"""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services import engine
from qt.services.engine import tick
from qt.settings_service import set_setting

ACCOUNT = {"equity": "100000", "cash": "100000", "account_number": "PA_TEST"}
CLOCK_OPEN = {"is_open": True, "next_close": "2099-01-01T21:00:00Z"}

_PARAMS = '{"entry":{"min_day_gain_pct":3.0},"exit":{"stop_loss_pct":4}}'


MINE = "modegroup:"


@pytest.fixture()
def world():
    """Keys stored (or `get_client` returns None and tick returns before it ever
    groups anything), a master switch, and an isolated set of strategies.

    DISABLES the other strategies rather than deleting them. Deleting hit a
    FOREIGN KEY constraint from `strategy_config_versions`, and — worse — took
    the seeded templates with it, breaking a sibling file that asserts they
    exist. The grouping only ever looks at ENABLED rows, so switching them off
    is both sufficient and reversible."""
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "live")   # ceiling wide open unless a test lowers it
        parked = [r.id for r in s.query(Strategy).filter(Strategy.enabled.is_(True)).all()]
        for sid in parked:
            s.get(Strategy, sid).enabled = False
        s.query(Trade).delete()
    yield
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
        s.query(Trade).delete()
        for row in s.query(Strategy).filter(Strategy.name.like(f"{MINE}%")).all():
            s.delete(row)
        s.flush()
        for sid in parked:
            row = s.get(Strategy, sid)
            if row is not None:
                row.enabled = True
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def _strategy(mode: str, name: str, enabled: bool = True) -> int:
    with session_scope() as s:
        row = Strategy(
            name=MINE + name, enabled=enabled, mode=mode, asset_class="stock",
            universe="custom", symbols='["AAA"]', preset="custom", params=_PARAMS,
            sizing_usd=100, sleeve_usd=1000, max_positions=3, swing_mode=True,
            ignore_regime=True,
        )
        s.add(row)
        s.flush()
        return row.id


def _open_trade(mode: str, symbol: str = "AAA") -> None:
    with session_scope() as s:
        sid = s.query(Strategy.id).first()
        s.add(Trade(
            strategy_id=sid[0] if sid else None, mode=mode, symbol=symbol,
            asset_class="stock", qty=1, notional=100, status="open",
            entry_price=100.0, entry_order_id="o-1",
        ))


async def _run_tick():
    """Runs a tick with the two workers replaced, and returns what each saw."""
    entries = AsyncMock()
    exits = AsyncMock()
    with patch.multiple(
        AlpacaClient,
        account=AsyncMock(return_value=ACCOUNT),
        clock=AsyncMock(return_value=CLOCK_OPEN),
    ), patch.object(engine, "_consider_entries", entries), \
            patch.object(engine, "_manage_exits", exits):
        await tick(leverage_unlocked=False)
    entry_calls = {c.args[2]: c.kwargs.get("strategies") for c in entries.call_args_list}
    exit_modes = [c.args[2] for c in exits.call_args_list]
    return entry_calls, exit_modes


# ── the entry pass ───────────────────────────────────────────────────────────
async def test_each_mode_gets_its_own_pass(world):
    _strategy("shadow", "s-one")
    _strategy("paper", "p-one")
    entry_calls, _ = await _run_tick()
    assert set(entry_calls) == {"shadow", "paper"}, entry_calls


async def test_a_pass_sees_only_its_own_strategies(world):
    """The leak that would matter. If the paper pass received the shadow
    strategy, that strategy would place real broker orders."""
    _strategy("shadow", "s-only")
    _strategy("paper", "p-only")
    entry_calls, _ = await _run_tick()
    assert [s.name for s in entry_calls["shadow"]] == [MINE + "s-only"]
    assert [s.name for s in entry_calls["paper"]] == [MINE + "p-only"]


async def test_the_master_switch_collapses_the_passes(world):
    """One setting stops the whole instance touching the broker. With the master
    at shadow, the paper strategy must run in the SHADOW pass — not merely be
    skipped, and certainly not keep its own mode."""
    _strategy("shadow", "s")
    _strategy("paper", "p")
    with session_scope() as s:
        set_setting(s, "engine_mode", "shadow")
    entry_calls, _ = await _run_tick()
    assert set(entry_calls) == {"shadow"}
    assert sorted(x.name for x in entry_calls["shadow"]) == [MINE + "p", MINE + "s"]


async def test_master_off_runs_nothing(world):
    _strategy("paper", "p")
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
    entry_calls, exit_modes = await _run_tick()
    assert entry_calls == {} and exit_modes == []


async def test_disabled_strategies_are_in_no_pass(world):
    _strategy("paper", "off-one", enabled=False)
    entry_calls, _ = await _run_tick()
    assert entry_calls == {}, entry_calls


# ── the exit pass ────────────────────────────────────────────────────────────
async def test_exits_run_for_an_open_trade_whose_strategy_was_demoted(world):
    """THE ASYMMETRY. A strategy moved down to shadow still has open PAPER
    positions carrying real stops. Keying exits on the enabled strategies would
    abandon them; they are keyed on the trade's own mode instead."""
    _strategy("shadow", "demoted")     # no paper strategy remains
    _open_trade("paper")
    _, exit_modes = await _run_tick()
    assert "paper" in exit_modes, exit_modes


async def test_exits_do_not_run_for_a_book_the_master_has_cooled(world):
    """The master switch has to hold on the exit side too, or "stop touching the
    broker" would still place sell orders."""
    _strategy("shadow", "s")
    _open_trade("paper")
    with session_scope() as s:
        set_setting(s, "engine_mode", "shadow")
    _, exit_modes = await _run_tick()
    assert "paper" not in exit_modes, exit_modes


async def test_each_mode_gets_at_most_one_exit_pass(world):
    """Two strategies in one mode must not mean two exit sweeps — every exit
    would be evaluated twice and a stop could fire two sell orders."""
    _strategy("paper", "p1")
    _strategy("paper", "p2")
    _open_trade("paper")
    _, exit_modes = await _run_tick()
    assert exit_modes.count("paper") == 1, exit_modes


async def test_a_mode_with_strategies_but_no_open_trades_still_sweeps(world):
    """Cheap, and the alternative is worse: a position opened earlier in the
    same tick would wait a full cycle for its first stop check."""
    _strategy("paper", "p")
    _, exit_modes = await _run_tick()
    assert "paper" in exit_modes, exit_modes


# ── the rails partition, verified rather than assumed ────────────────────────
def test_the_daily_loss_rail_counts_only_its_own_mode(world):
    """The memory note that prompted this said to VERIFY rather than trust it.
    This is the one place a mistake spends real money on a paper strategy's
    budget, or stops a live strategy because a paper one lost."""
    from datetime import datetime, timedelta, timezone

    _strategy("paper", "p")
    day_start = datetime.now(timezone.utc) - timedelta(hours=1)
    with session_scope() as s:
        sid = s.query(Strategy.id).first()[0]
        for mode, pnl in (("paper", -50.0), ("shadow", -999.0), ("live", -777.0)):
            s.add(Trade(
                strategy_id=sid, mode=mode, symbol="AAA", asset_class="stock",
                qty=1, notional=100, status="closed", entry_price=100.0,
                exit_price=50.0, pnl=pnl,
                exit_at=datetime.now(timezone.utc),
            ))
    with session_scope() as s:
        assert engine._daily_loss(s, "paper", day_start) == 50.0
        assert engine._daily_loss(s, "live", day_start) == 777.0
        assert engine._daily_loss(s, "shadow", day_start) == 999.0


# ── per-mode clients (stage 2) ───────────────────────────────────────────────
async def _run_tick_capturing_clients():
    """Like `_run_tick`, but returns the CLIENT and equity each entry pass got."""
    entries = AsyncMock()
    exits = AsyncMock()
    with patch.multiple(
        AlpacaClient,
        account=AsyncMock(return_value=ACCOUNT),
        clock=AsyncMock(return_value=CLOCK_OPEN),
    ), patch.object(engine, "_consider_entries", entries), \
            patch.object(engine, "_manage_exits", exits):
        await tick(leverage_unlocked=False)
    return {c.args[2]: (c.args[1], c.args[3]) for c in entries.call_args_list}


@pytest.fixture()
def live_keys():
    from qt.broker.alpaca import LIVE_SECRET_KEY_ID, LIVE_SECRET_KEY_SECRET

    with session_scope() as s:
        security.set_secret(s, LIVE_SECRET_KEY_ID, "live-id")
        security.set_secret(s, LIVE_SECRET_KEY_SECRET, "live-secret")
    yield
    with session_scope() as s:
        security.delete_secret(s, LIVE_SECRET_KEY_ID)
        security.delete_secret(s, LIVE_SECRET_KEY_SECRET)


async def test_the_live_pass_uses_the_live_client(world, live_keys):
    """THE POINT OF STAGE 2. Running a live pass through the paper client is the
    single mistake that makes every 'live' order a silent paper one."""
    from qt.broker.alpaca import LIVE_BASE_URL, PAPER_BASE_URL

    _strategy("paper", "p")
    _strategy("live", "l")
    calls = await _run_tick_capturing_clients()
    assert calls["paper"][0].base_url == PAPER_BASE_URL
    assert calls["live"][0].base_url == LIVE_BASE_URL
    assert calls["live"][0].key_id == "live-id"


async def test_a_live_strategy_does_not_trade_without_live_keys(world):
    """No credentials, no live pass — however the strategy is configured. The
    paper book must keep running regardless."""
    _strategy("paper", "p")
    _strategy("live", "l")
    calls = await _run_tick_capturing_clients()
    assert "paper" in calls
    assert "live" not in calls, "traded live with no credentials stored"


async def test_the_live_pass_is_sized_on_the_live_account(world, live_keys):
    """Position size and the exposure rail are both fractions of equity, so a
    live pass on the paper account's equity sizes real money against imaginary
    money."""
    async def _account(self):
        from qt.broker.alpaca import LIVE_BASE_URL

        return {"equity": "7777" if self.base_url == LIVE_BASE_URL else "100000",
                "cash": "1", "account_number": "X"}

    entries = AsyncMock()
    _strategy("paper", "p")
    _strategy("live", "l")
    with patch.object(AlpacaClient, "account", _account), \
            patch.object(AlpacaClient, "clock", AsyncMock(return_value=CLOCK_OPEN)), \
            patch.object(engine, "_consider_entries", entries), \
            patch.object(engine, "_manage_exits", AsyncMock()):
        await tick(leverage_unlocked=False)
    got = {c.args[2]: c.args[3] for c in entries.call_args_list}
    assert got["paper"] == 100000.0
    assert got["live"] == 7777.0, "live sized against the paper account"


async def test_an_unreadable_live_account_skips_only_the_live_pass(world, live_keys):
    """A broker failure on one book must not stop the others. The paper strategy
    has to keep trading while the live account is unreachable."""
    async def _account(self):
        from qt.broker.alpaca import LIVE_BASE_URL

        if self.base_url == LIVE_BASE_URL:
            raise RuntimeError("live account unreachable")
        return ACCOUNT

    entries = AsyncMock()
    _strategy("paper", "p")
    _strategy("live", "l")
    with patch.object(AlpacaClient, "account", _account), \
            patch.object(AlpacaClient, "clock", AsyncMock(return_value=CLOCK_OPEN)), \
            patch.object(engine, "_consider_entries", entries), \
            patch.object(engine, "_manage_exits", AsyncMock()):
        await tick(leverage_unlocked=False)
    modes = {c.args[2] for c in entries.call_args_list}
    assert modes == {"paper"}, modes


async def test_missing_live_credentials_are_named_as_the_reason(world, caplog):
    """The None-client guard survived its first mutation because the equity
    fetch below it already swallowed the failure — an AttributeError caught by a
    broad `except` and logged as "could not read the live account", which is a
    different and misleading problem. The guard's real contribution is saying
    what is actually wrong, so that is what is asserted.

    A user whose live strategy silently does nothing needs to be told it has no
    credentials, not that the account was unreadable."""
    import logging

    _strategy("live", "l")
    with caplog.at_level(logging.WARNING, logger="qt.engine"):
        await _run_tick_capturing_clients()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "credentials" in text, text
    assert "could not read" not in text, (
        "reported as an unreadable account rather than as missing credentials")
