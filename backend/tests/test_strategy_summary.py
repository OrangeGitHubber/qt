"""The strategy card must describe the strategy that is actually running.

Two failures on one line motivated these tests, both found on "max $20 swing
trader", whose card read:

    trail 6% · stop 1.5×ATR · target 100%

while its config said trail = 1.5×ATR (the 6% is only the fallback for when ATR
can't be computed) and give-back = 20% of every gain.

So: one number that was WRONG, and one whole rule that was MISSING. They are the
same bug seen twice — a summary hand-written inline and never revisited when the
rules moved underneath it. `exit_giveback_pct` was added, the engine honoured it,
and the card that exists to explain the strategy said nothing.

Pinning "the string looks right today" would not have prevented either one. What
these tests pin instead is the INVARIANT:

    every field of EntryRules / ExitRules either changes the summary when you
    switch it on, or is named in _EXCLUDED with a reason.

Add a rule to the pydantic model and this file fails until you decide which it
is. That is the whole point.

Like test_optimizer_precision.py, this reaches across into the frontend — and for
the same reason: the contradiction existed precisely because nothing could see
both sides at once. Here it goes one better and EXECUTES the frontend code
(node's TypeScript type-stripping runs the .ts module directly), so what is under
test is the shipped summary, not a regex approximation of it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from qt.api.strategies import EntryRules, ExitRules, StrategyParams

SUMMARY_TS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "strategySummary.ts"
)

# Knobs that price an order once the decision to trade is already made. They
# never decide WHETHER to enter or exit, so they are execution detail and stay
# off a summary of trading rules. The editor shows them under "Advanced".
_EXCLUDED = {
    "entry_slippage_pct": "prices the buy limit; does not gate the entry",
    "exit_slippage_pct": "prices the sell limit; does not trigger the exit",
    "exit_slippage_max_pct": "widens the sell limit on a miss; not an exit rule",
}

# A value for each field that is meaningfully DIFFERENT from its default, so
# "switching it on changes the summary" has something to compare. Kept explicit
# rather than derived: this map is also the checklist that a newly added rule
# has been thought about at all.
_ENTRY_ON = {
    "min_day_gain_pct": -2.0,  # default is +3: a dip-buying entry
    "max_day_gain_pct": 8.0,
    "min_price": 5.0,
    "max_price": 50.0,
    "require_above_vwap": False,  # default True — turning it OFF must show
    "require_macd_bullish": True,
    "rsi_min": 30.0,
    "rsi_max": 70.0,
    "rsi_cross_above": 40.0,
    "entry_window_start": "09:45",
    "entry_window_end": "11:00",
    "entry_slippage_pct": 2.0,
}

_EXIT_ON = {
    "trailing_stop_pct": 7.0,
    "stop_loss_pct": 3.0,  # must stay > 0: the model makes a hard stop mandatory
    "take_profit_pct": 10.0,
    "exit_giveback_pct": 20.0,
    "max_holding_hours": 48.0,
    "flatten_before_close": True,
    "exit_below_vwap": True,
    "exit_on_macd_bearish": True,
    "exit_rsi_above": 70.0,
    "exit_rsi_below": 30.0,
    "exit_rsi_falling": True,
    "exit_on_regime_bear": True,
    "rotate_on_rank_dropout": True,
    "exit_slippage_pct": 2.0,
    "exit_slippage_max_pct": 4.0,
}

# The entry time window is ONE rule wearing two fields — a start with no end
# bounds nothing, so they are switched on together.
_TOGETHER = {"entry_window_start": "entry_window_end", "entry_window_end": "entry_window_start"}

_DRIVER = """
import { readFileSync } from "node:fs";
import { entrySummary, exitSummary } from %(module)s;

const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const out = cases.map((c) => ({
  entry: entrySummary(c.params),
  exit: exitSummary(c.params, c.top_n),
}));
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def summarise(tmp_path_factory):
    """Run the real frontend summary builders over a batch of param sets.

    Batched into a single node process — one interpreter start for the whole
    module rather than one per assertion.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI installs node for this job
        pytest.fail(
            "node is required to test the frontend summary builders "
            "(see .github/workflows/ci.yml, backend-tests job)"
        )
    work = tmp_path_factory.mktemp("summary")
    driver = work / "driver.mjs"
    driver.write_text(_DRIVER % {"module": json.dumps(SUMMARY_TS.as_uri())}, encoding="utf8")

    def run(cases: list[dict]) -> list[dict]:
        payload = work / "cases.json"
        payload.write_text(json.dumps(cases), encoding="utf8")
        proc = subprocess.run(
            [node, "--experimental-strip-types", str(driver), str(payload)],
            capture_output=True,
            text=True,
            encoding="utf8",
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


def _params(**overrides) -> dict:
    """A default StrategyParams as JSON, with dotted overrides applied."""
    p = StrategyParams().model_dump(mode="json")
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        if p.get(section) is None:
            p[section] = {}
        p[section][field] = value
    return p


# The strategy from the bug report, exactly as saved.
MAX20 = _params(
    **{
        "exit.trailing_stop_pct": 6,
        "exit.stop_loss_pct": 2,
        "exit.take_profit_pct": 100,
        "exit.exit_giveback_pct": 20,
        "atr.period": 14,
        "atr.trail_mult": 1.5,
        "atr.stop_mult": 1.5,
    }
)


def test_atr_replaces_the_trail_the_same_way_it_replaces_the_stop(summarise):
    """The reported card, end to end.

    Both halves of the bug in one assertion because both halves are one line:
    the ATR trail must displace the 6% exactly as the ATR stop displaces the 2%,
    and the give-back must be there at all.
    """
    (got,) = summarise([{"params": MAX20}])
    assert got["exit"] == "trail 1.5×ATR · stop 1.5×ATR · target 100% · give back 20%"


def test_the_fixed_percentage_is_hidden_only_while_the_atr_trail_is_on(summarise):
    """The fallback is not a lie when it is the thing in use.

    Guards the obvious over-correction: dropping the percentage always, so a
    strategy with no ATR trail shows no trail at all.
    """
    off = _params(**{"exit.trailing_stop_pct": 6, "atr.trail_mult": 0, "atr.stop_mult": 0})
    (got,) = summarise([{"params": off}])
    assert "trail 6%" in got["exit"]
    assert "ATR" not in got["exit"]


def test_giveback_is_named_as_a_share_of_the_gain(summarise):
    """`give back 20%` — not `20%`, and not merged into the take-profit.

    A take-profit caps the upside and a give-back does not; a summary that let
    the two read alike would be worse than the omission it replaced.
    """
    (got,) = summarise([{"params": _params(**{"exit.exit_giveback_pct": 20})}])
    assert "give back 20%" in got["exit"]


@pytest.mark.parametrize("field", sorted(ExitRules.model_fields))
def test_every_exit_rule_that_is_switched_on_is_visible(summarise, field):
    if field in _EXCLUDED:
        pytest.skip(f"deliberately omitted: {_EXCLUDED[field]}")
    assert field in _EXIT_ON, (
        f"ExitRules.{field} is new. Render it in exitSummary(), or add it to "
        f"_EXCLUDED with the reason it is not a trading rule."
    )
    overrides = {f"exit.{field}": _EXIT_ON[field]}
    base, changed = summarise(
        [{"params": _params(), "top_n": 10}, {"params": _params(**overrides), "top_n": 10}]
    )
    assert changed["exit"] != base["exit"], f"exit.{field} can be set without the card saying so"


@pytest.mark.parametrize("field", sorted(EntryRules.model_fields))
def test_every_entry_rule_that_is_switched_on_is_visible(summarise, field):
    if field in _EXCLUDED:
        pytest.skip(f"deliberately omitted: {_EXCLUDED[field]}")
    assert field in _ENTRY_ON, (
        f"EntryRules.{field} is new. Render it in entrySummary(), or add it to "
        f"_EXCLUDED with the reason it is not a trading rule."
    )
    overrides = {f"entry.{field}": _ENTRY_ON[field]}
    partner = _TOGETHER.get(field)
    if partner:
        overrides[f"entry.{partner}"] = _ENTRY_ON[partner]
    base, changed = summarise([{"params": _params()}, {"params": _params(**overrides)}])
    assert changed["entry"] != base["entry"], f"entry.{field} can be set without the card saying so"


@pytest.mark.parametrize("mult", ["stop_mult", "trail_mult"])
def test_both_atr_multipliers_reach_the_summary(summarise, mult):
    """Symmetry, stated as a property rather than as one expected string."""
    base, changed = summarise(
        [{"params": _params()}, {"params": _params(**{f"atr.{mult}": 2.5})}]
    )
    assert "2.5×ATR" in changed["exit"]
    assert changed["exit"] != base["exit"]


def test_a_sleeve_with_no_exits_says_so(summarise):
    """A DCA sleeve may legally run with every exit off.

    It used to render `trail 0% · stop 0%`, which reads as a configured stop at
    the worst possible level rather than as no stop at all.
    """
    off = _params(**{"exit.trailing_stop_pct": 0, "exit.stop_loss_pct": 0})
    (got,) = summarise([{"params": off}])
    assert got["exit"] == "no exit rules — held"
