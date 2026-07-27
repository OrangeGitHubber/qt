"""Per-strategy order-fill settings: the escalating exit buffer + schema."""

import pytest
from pydantic import ValidationError

from qt.api.strategies import EntryRules, ExitRules
from qt.services.execution import _escalated_exit_pct


def test_escalated_exit_pct_widens_with_misses_up_to_the_cap():
    assert _escalated_exit_pct(1.0, 1.0, 0) == 1.0   # cap == base → never escalates
    assert _escalated_exit_pct(1.0, 1.0, 5) == 1.0
    assert _escalated_exit_pct(1.0, 3.0, 0) == 1.0   # first attempt = base
    assert _escalated_exit_pct(1.0, 3.0, 1) == 2.0   # +1 base step per prior miss
    assert _escalated_exit_pct(1.0, 3.0, 2) == 3.0   # reaches the cap
    assert _escalated_exit_pct(1.0, 3.0, 9) == 3.0   # stays capped
    assert _escalated_exit_pct(1.0, None, 4) == 1.0  # no cap → no escalation
    assert _escalated_exit_pct(2.0, 1.0, 3) == 2.0   # cap < base → clamped up to base


def test_slippage_defaults_preserve_current_behaviour():
    assert EntryRules().entry_slippage_pct == 0.5      # 0.5% through = old ENTRY_SLIP 1.005
    e = ExitRules()
    assert e.exit_slippage_pct == 1.0 and e.exit_slippage_max_pct == 1.0  # old EXIT_SLIP 0.99, no escalation


def test_exit_slippage_max_cannot_be_below_base():
    ExitRules(exit_slippage_pct=1.0, exit_slippage_max_pct=3.0)  # ok
    with pytest.raises(ValidationError):
        ExitRules(exit_slippage_pct=3.0, exit_slippage_max_pct=1.0)
