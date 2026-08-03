"""How precise a percentage is allowed to be — settled in ONE place.

The optimizer used to hand back a draft its own editor refused to save. The
search proposed a stop-loss of 1.23%, the draft saved fine, and then the
strategy form — where `step="0.1"` on the number input quietly means "one
decimal place only" to the browser — blocked the whole form until the tuned
value was rounded down to 1.2. Two parts of the app disagreed about the same
number, and the user was stuck between them with a native browser message the
app never wrote and cannot reword.

The disagreement is settled in favour of PRECISION, because the maths uses it:
evaluate_exit compares entry_price * (1 - pct/100) against raw bar lows with no
rounding anywhere in between, so 1.23% and 1.20% really are different stops that
fire on different bars (proved below). The editor's step was never a deliberate
precision policy — it was a spinner increment that HTML5 promotes into a
validation rule.

So the rule these tests pin down is:

  1. a hundredth of a percent changes a real trading decision;
  2. the editor imposes no precision the backend model doesn't have — int fields
     step by 1, float fields declare step="any";
  3. every value the search can propose sits inside the editor's OWN min/max;
  4. a tuned value survives create -> read -> save-unchanged byte-identical.

Two of these read the frontend source from a backend test, which is unusual and
deliberate: the contradiction existed precisely BECAUSE nothing could see both
sides at once. Every previous test of "the search can only propose savable
values" checked the pydantic model alone — the looser of the two gates — and so
passed all the way through the bug.
"""

import re
from pathlib import Path

import pytest

from qt.api.strategies import ATRConfig, DCAConfig, EntryRules, ExitRules, MACDConfig, StrategyBody
from qt.services.optimizer import KNOB_BOUNDS, _geometric_grid

EDITOR = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "Strategies.tsx"

# Which pydantic model each JSX value-expression namespace binds to.
_MODELS = {
    "p.entry": EntryRules,
    "p.exit": ExitRules,
    "p.dca": DCAConfig,
    "atr": ATRConfig,
    "macd": MACDConfig,
    "s": StrategyBody,
}

# The editor field behind each searchable knob, as it is written in the JSX.
_KNOB_FIELDS = {
    "min_day_gain_pct": "p.entry.min_day_gain_pct",
    "trailing_stop_pct": "p.exit.trailing_stop_pct",
    "stop_loss_pct": "p.exit.stop_loss_pct",
    "take_profit_pct": "p.exit.take_profit_pct",
    "rsi_max": "p.entry.rsi_max",
    "rsi_min": "p.entry.rsi_min",
    "exit_rsi_above": "p.exit.exit_rsi_above",
    "atr_stop_mult": "atr.stop_mult",
    "macd_slow": "macd.slow",
}


def _number_fields() -> dict[str, dict[str, str]]:
    """Every <NumberField> in the strategy editor, keyed by the state path it is
    bound to, with its declared step/min/max.

    Non-greedy to `/>` rather than `>`, because the onChange handlers contain an
    arrow function and `[^>]*` would stop at its `=>`."""
    src = EDITOR.read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for tag in re.findall(r"<NumberField(.*?)/>", src, re.S):
        value = re.search(r"value=\{([^}]*)\}", tag)
        if not value:
            continue
        # "p.entry.rsi_min ?? 0" / "s.top_n!" -> "p.entry.rsi_min" / "s.top_n"
        path = value.group(1).split("??")[0].strip().rstrip("!")
        attrs = {}
        for name in ("step", "min", "max"):
            found = re.search(rf'\b{name}=(?:"([^"]*)"|\{{([^}}]*)\}})', tag)
            if found:
                attrs[name] = found.group(1) if found.group(1) is not None else found.group(2)
        out[path] = attrs
    return out


def test_the_editor_asks_for_no_more_precision_than_the_model_defines():
    """The bug's root. `step` looks like it only sizes the up/down arrows, but the
    browser ALSO refuses to submit a form whose value is off the step grid — so
    step="0.1" on a percentage is a validation rule saying "tenths only", written
    nowhere in the schema and enforced with a message the app cannot reword.

    A field the backend declares `float` is continuous everywhere that matters
    (model, backtester, live engine), so the editor must declare step="any". A
    field it declares `int` genuinely is whole numbers, and step="1" states that
    honestly. Anything else is the editor inventing a limit the server does not
    have — which is how a saved strategy became unsavable."""
    fields = _number_fields()
    assert fields, "no <NumberField> found — the parser has drifted from the JSX"

    checked: dict[str, int] = {ns: 0 for ns in _MODELS}
    for path, attrs in fields.items():
        namespace, _, name = path.rpartition(".")
        model = _MODELS.get(namespace)
        if model is None or name not in model.model_fields:
            continue  # a local/UI-only number (history days, iterations…)
        checked[namespace] += 1
        annotation = model.model_fields[name].annotation
        expected = "1" if annotation is int else "any"
        assert attrs.get("step") == expected, (
            f"{path} is a {annotation.__name__} in the model but the editor "
            f'declares step="{attrs.get("step")}" — expected "{expected}"'
        )

    # Guards the loop against silently checking nothing. Per NAMESPACE, not a
    # total: a total is satisfied by the entry fields alone, so dropping every
    # exit rule — where the reported bug actually lived — sailed straight past a
    # count-based guard when this was mutation-tested.
    for namespace, n in checked.items():
        assert n, f"matched no editor fields for {namespace} — the parser has drifted"


def test_a_hundredth_of_a_percent_decides_a_real_trade():
    """Why precision won rather than the editor. The same bar — low 98.78 on a
    100.00 entry — stops out a 1.20% stop and leaves a 1.23% stop untouched. If
    the hundredth were rounded away downstream this would be a distinction
    without a difference and the honest fix would have been the opposite one:
    stop the search producing false precision."""
    from datetime import datetime, timezone

    from qt.services.engine import evaluate_exit

    def run(stop_pct: float) -> tuple[bool, str, dict]:
        params = {
            "exit": {
                "stop_loss_pct": stop_pct,
                "trailing_stop_pct": 0,
                "take_profit_pct": 0,
                "max_holding_hours": 0,
                "flatten_before_close": False,
                "exit_below_vwap": False,
            }
        }
        out: dict = {}
        now = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)
        hit, reason = evaluate_exit(
            params, False, 100.0, now, 100.0, 99.0, None, now, False,
            bar_high=100.0, bar_low=98.78, out=out,
        )
        return hit, reason, out

    tighter_hit, reason, out = run(1.20)
    assert tighter_hit is True and "stop-loss" in reason
    # And it exits AT the stop, which is itself a function of the hundredth.
    assert out["exit_price"] == pytest.approx(98.80)

    looser_hit, _, _ = run(1.23)
    assert looser_hit is False, "1.23% and 1.20% must not be the same stop"


def test_every_value_the_search_can_propose_fits_the_editors_own_limits():
    """The optimizer's bounds comment claims they are "the same limits the
    strategy schema enforces" — which was true of pydantic and false of the
    editor, the gate the user actually meets. The editor's trailing-stop floor is
    0.5% and its stop-loss floor 0.1%, both stricter than the schema's ge=0, so a
    search floored at 0.05 could hand back a draft that opens and then cannot be
    saved. Read the mins out of the JSX rather than restating them here, so the
    two cannot drift apart again."""
    fields = _number_fields()
    for knob, (lo, hi, _dec) in KNOB_BOUNDS.items():
        attrs = fields[_KNOB_FIELDS[knob]]
        editor_min = float(attrs["min"])
        editor_max = float(attrs["max"]) if "max" in attrs else float("inf")
        assert lo >= editor_min, f"{knob}: search floor {lo} is below the editor's min {editor_min}"
        assert hi <= editor_max, f"{knob}: search ceiling {hi} is above the editor's max {editor_max}"
        # Not just the endpoints — every value a grid can actually produce.
        for anchor in (lo, hi, (lo + hi) / 2):
            for step in (0.05, 0.15, 0.25, 0.5):
                for v in _geometric_grid(anchor, step, KNOB_BOUNDS[knob]):
                    assert editor_min <= v <= editor_max, f"{knob} proposed {v}"


def test_the_search_still_explores_hundredths():
    """The other half of the settlement, and the reason this is a test rather
    than a comment: rounding the grid to tenths would look like a tidy fix and
    would quietly damage the search. The grid is GEOMETRIC, so near the bottom of
    a range consecutive steps are a few hundredths apart — round them to tenths
    and neighbouring steps collapse onto the same value, shrinking the space the
    user was told was explored."""
    from qt.services.optimizer import RELATIVE_STEP_DEFAULT

    # "Only buy something already up 0.5% today" — an ordinary setting, not a
    # contrived one, and at the default step its grid is spaced in hundredths.
    grid = _geometric_grid(0.5, RELATIVE_STEP_DEFAULT, KNOB_BOUNDS["min_day_gain_pct"])
    assert any(round(v, 1) != round(v, 2) for v in grid), f"no hundredths in {grid}"
    tenths = {round(v, 1) for v in grid}
    assert len(tenths) < len(grid), "rounding to tenths would collapse distinct steps"


def _tuned_strategy() -> dict:
    """A strategy carrying exactly the kind of values a search produces — the
    hundredths that used to be unsavable, plus a tenth on an RSI knob."""
    return {
        "name": "tuned",
        "asset_class": "stock",
        "universe": "watchlist",
        "params": {
            "entry": {
                "min_day_gain_pct": 2.47,
                "require_above_vwap": False,
                "rsi_min": 40.5,
                "rsi_max": 71.5,
            },
            "exit": {
                "trailing_stop_pct": 1.32,
                "stop_loss_pct": 1.23,
                "take_profit_pct": 12.56,
            },
            "atr": {"period": 14, "stop_mult": 1.37, "risk_usd": 0},
        },
    }


def test_a_tuned_value_survives_being_saved_opened_and_saved_again(client):
    """The quiet half of the bug. Even once the form submits, a save must not
    change what the search found — otherwise renaming a strategy would silently
    downgrade the result of a long search, and nothing would say so."""
    created = client.post("/api/strategies", json=_tuned_strategy())
    assert created.status_code == 200, created.text
    sid = created.json()["id"]

    def read() -> dict:
        row = next(r for r in client.get("/api/strategies").json() if r["id"] == sid)
        return row["params"]

    after_create = read()
    assert after_create["exit"]["stop_loss_pct"] == 1.23
    assert after_create["exit"]["take_profit_pct"] == 12.56
    assert after_create["entry"]["rsi_min"] == 40.5
    assert after_create["atr"]["stop_mult"] == 1.37

    # Open the editor, change ONLY the name, save — the round-trip that used to be
    # blocked, and that must never rewrite a number.
    saved = client.put(f"/api/strategies/{sid}", json={**_tuned_strategy(), "name": "renamed"})
    assert saved.status_code == 200, saved.text
    assert read() == after_create
