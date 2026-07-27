"""The engine must not open NEW positions when it has confidently detected that
/data is ephemeral — a lost journal blinds the 'already open' rail and the bot
re-buys positions the broker already holds. Exits are unaffected (tested by the
reconcile/shadow suites); here we pin the entry freeze."""

import asyncio
from unittest.mock import MagicMock

from qt.services import engine, persistence


def test_new_entries_freeze_when_data_confidently_not_persistent(monkeypatch):
    monkeypatch.setattr(persistence, "boot_state", lambda: {"data_persistent": False})
    engine._entries_frozen_logged = False

    session = MagicMock()
    # If the gate works, we return BEFORE touching the DB. Make any query blow up
    # so a regression (querying strategies anyway) fails loudly.
    session.query.side_effect = AssertionError("must not query strategies while entries are frozen")

    asyncio.run(engine._consider_entries(session, MagicMock(), "paper", 1000.0, True, False))
    assert session.query.call_count == 0


def test_new_entries_proceed_when_persistence_is_unknown(monkeypatch):
    # None = ambiguous (local dev, no /proc): must NOT freeze — trade normally.
    monkeypatch.setattr(persistence, "boot_state", lambda: {"data_persistent": None})
    engine._entries_frozen_logged = False

    session = MagicMock()
    # Strategy query returns no enabled strategies, so it returns right after the
    # gate — but the point is it got PAST the gate and queried at all.
    session.query.return_value.filter.return_value.all.return_value = []

    asyncio.run(engine._consider_entries(session, MagicMock(), "paper", 1000.0, True, False))
    assert session.query.call_count >= 1  # proceeded past the persistence gate
