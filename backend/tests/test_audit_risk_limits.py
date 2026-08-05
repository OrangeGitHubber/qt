"""The bounds on a risk setting live in ONE place, and the page can read them.

Werner typed 1000 into "Max open positions", which the API refused at 50 — a
ceiling that arrived in the Phase 2 scaffolding with no comment and no way for
the person filling in the form to discover it. Two things came out of that:

  * 50 was arbitrary and out of step with its neighbours (200 trades/day, $10M
    exposure). It is now 1000, which against a $15,000 exposure cap is $15 a
    position — the exposure rail binds long before the count ever could.
  * The number a form ENFORCES and the number it SHOWS must be the same number.
    The first version of the hint hardcoded "1-50" in the page, which is exactly
    the drift that produced the original confusion in a different form.

So `RISK_LIMITS` is the single definition: the pydantic validators are built
from it, and `/api/engine/state` publishes it so the page can render both the
`max` attribute and the hint beneath the input from the same source.

`QT_MAX_TOTAL_POSITIONS` raises or lowers the ceiling for anyone who wants
something other than 1000 — a container-level act, like `QT_ALLOW_LEVERAGE`,
because it widens what the account is allowed to do and should not be a click in
the UI.
"""

import importlib
import os

import pytest

from qt.api import engine as engine_api


def _reload(monkeypatch, value: str | None):
    """Re-import the module with the env var set, since a pydantic Field's
    bounds are fixed when the class is created."""
    if value is None:
        monkeypatch.delenv("QT_MAX_TOTAL_POSITIONS", raising=False)
    else:
        monkeypatch.setenv("QT_MAX_TOTAL_POSITIONS", value)
    return importlib.reload(engine_api)


@pytest.fixture(autouse=True)
def _restore():
    yield
    os.environ.pop("QT_MAX_TOTAL_POSITIONS", None)
    importlib.reload(engine_api)


def _body(mod, **over):
    return dict(
        {
            "max_daily_loss_usd": 200, "max_daily_loss_pct": 5,
            "max_total_positions": 6, "max_total_exposure_usd": 3000,
            "max_trades_per_day": 10, "cooldown_hours_after_loss": 24,
            "wash_sale_guard": "block", "leverage_enabled": False,
        },
        **over,
    )


def test_the_default_ceiling_is_a_thousand():
    """The number Werner asked for, and the number the docstring commits to."""
    assert engine_api.RISK_LIMITS["max_total_positions"][1] == 1000
    engine_api.RiskBody(**_body(engine_api, max_total_positions=1000))


def test_past_the_ceiling_is_still_refused():
    """Raising a bound is not removing it — a typo of 10000 must still be caught
    rather than silently accepted."""
    with pytest.raises(ValueError):
        engine_api.RiskBody(**_body(engine_api, max_total_positions=1001))


def test_a_container_variable_moves_the_ceiling(monkeypatch):
    mod = _reload(monkeypatch, "5000")
    assert mod.RISK_LIMITS["max_total_positions"][1] == 5000
    mod.RiskBody(**_body(mod, max_total_positions=5000))
    with pytest.raises(ValueError):
        mod.RiskBody(**_body(mod, max_total_positions=5001))


def test_nonsense_in_the_variable_falls_back_rather_than_crashing(monkeypatch):
    """A container that fails to boot because of a typo in an optional override
    is a worse outcome than ignoring the typo. The default is the safe answer."""
    for junk in ("banana", "", "-1", "0"):
        mod = _reload(monkeypatch, junk)
        assert mod.RISK_LIMITS["max_total_positions"][1] == 1000, junk


def test_the_validators_are_built_from_the_same_table():
    """Not merely equal today — DERIVED, so a change to the table cannot leave a
    validator behind. Checked against pydantic's own recorded metadata."""
    fields = engine_api.RiskBody.model_fields
    for name, (low, high) in engine_api.RISK_LIMITS.items():
        meta = fields[name].metadata
        assert any(getattr(m, "ge", None) == low for m in meta), name
        assert any(getattr(m, "le", None) == high for m in meta), name


def test_the_page_can_read_the_limits(client, monkeypatch):
    """The form has to render `max` and the hint under the input from the same
    numbers the server enforces. Hardcoding them in the page is the drift that
    produced the original confusion."""
    body = client.get("/api/engine").json()

    assert "risk_limits" in body, sorted(body)
    assert body["risk_limits"]["max_total_positions"] == [1, 1000]
    assert set(body["risk_limits"]) == set(engine_api.RISK_LIMITS)
