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

from qt.api.strategies import (
    ATRConfig,
    DCAConfig,
    MACDConfig,
    EntryRules,
    ExecutionConfig,
    ExitRules,
    StrategyBody,
    StrategyParams,
)

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
import { entrySummary, exitSummary, sizingSummary } from %(module)s;

const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const out = cases.map((c) => {
  // Entry and sizing both read fields from outside `params` — the regime gate,
  // the sleeve, the rails — so both take a row. A case that only asserts on the
  // exits passes bare params and gets a row with nothing else set.
  const row = c.row ?? { params: c.params };
  return {
    entry: entrySummary(row),
    exit: exitSummary(row.params, c.top_n),
    sizing: c.row ? sizingSummary(c.row) : null,
  };
});
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


# --- Entry gates that live outside EntryRules --------------------------------
#
# The test above pins every field of EntryRules, which is what evaluate_entry
# reads. But evaluate_entry is not the only thing that decides an entry, and the
# two gates ABOVE it in _consider_entries were both invisible:
#
#   dca.interval_days  a DCA sleeve is handed to _consider_dca_entries and the
#                      loop `continue`s, so evaluate_entry is NEVER CALLED. Every
#                      EntryRules field is dead, and the row rendered them all.
#                      Not one number wrong — the whole row wrong.
#   ignore_regime      a column on the strategy, checked in the same loop, which
#                      blocks stock buys while SPY is under its 200-day.
#
# A summary built only from `params.entry` could not see either one, which is
# why entrySummary now takes the row.

# Entry-deciding fields that are NOT in EntryRules. StrategyBody is enumerated
# by the sizing property test, so only DCAConfig needs a source here — the
# regime gate is asserted directly below.
_ENTRY_GATE_EXCLUDED: dict[str, str] = {}

_ENTRY_GATE_ON = {"params.dca.interval_days": 7}


def test_a_dca_sleeve_does_not_pretend_to_have_entry_rules(summarise):
    """The sleeve buys on a calendar; the momentum rules never run.

    Two claims: the cadence is named, and the dead rules are gone. The second is
    the one that matters — "+3% day · above VWAP" on a strategy that ignores both
    is the most confident kind of wrong this file exists to prevent.
    """
    # The defaults are +3% day and above-VWAP, so a sleeve built from them is
    # exactly the card the bug produced: two rules named, neither one running.
    plain, dca = summarise(
        [{"row": _row()}, {"row": _row(**{"params.dca.interval_days": 7})}]
    )
    assert plain["entry"] == "+3% day · above VWAP"
    assert dca["entry"] == "every 7 days on schedule — entry rules don't apply"


def test_a_daily_dca_sleeve_reads_as_english(summarise):
    """`every 1 days` is the kind of detail that makes a card look generated."""
    (got,) = summarise([{"row": _row(**{"params.dca.interval_days": 1})}])
    assert got["entry"].startswith("every day on schedule")


def test_a_dca_sleeve_is_not_claimed_to_use_atr_sizing(summarise):
    """_consider_dca_entries calls open_trade WITHOUT the sizing_usd override.

    So a scheduled lot is the fixed dollar amount even with ATR sizing fully
    configured — the same branch that kills the entry rules reaches the Sizing
    row too, and the two dead-ATR reasons are not the same problem.
    """
    row = _row(
        **{
            "sizing_usd": 100,
            "params.atr.stop_mult": 1.5,
            "params.atr.risk_usd": 50,
            "params.dca.interval_days": 7,
        }
    )
    (got,) = summarise([{"row": row}])
    assert "$100 / trade" in got["sizing"]
    assert "ATR risk $50 unused — DCA lots buy the fixed size" in got["sizing"]


def test_the_regime_override_is_named_on_the_entry_row(summarise):
    """It blocks buys, so it belongs beside the other things that block buys.

    Stock-only, matching the engine — crypto has no SPY regime to ignore, and a
    crypto card claiming otherwise would advertise a rule that cannot fire.
    """
    stock, crypto = summarise(
        [
            {"row": _row(ignore_regime=True)},
            {"row": _row(asset_class="crypto", ignore_regime=True)},
        ]
    )
    assert "buys even when SPY is below its 200-day" in stock["entry"]
    assert "SPY" not in crypto["entry"]


def test_the_regime_filter_left_on_says_nothing(summarise):
    """Only the override is named.

    Leaving it off is the default, and whether the filter then runs at all also
    depends on the account-wide `regime_filter_enabled` setting, which no card
    can see. Claiming the rule from the strategy alone would sometimes be false.
    """
    (got,) = summarise([{"row": _row(ignore_regime=False)}])
    assert "SPY" not in got["entry"]


@pytest.mark.parametrize("field", sorted(f"params.dca.{n}" for n in DCAConfig.model_fields))
def test_every_entry_gate_outside_entryrules_is_visible(summarise, field):
    if field in _ENTRY_GATE_EXCLUDED:
        pytest.skip(f"deliberately omitted: {_ENTRY_GATE_EXCLUDED[field]}")
    assert field in _ENTRY_GATE_ON, (
        f"{field} is new. If it can decide whether a buy happens, render it in "
        f"entrySummary(); otherwise add it to _ENTRY_GATE_EXCLUDED with the reason."
    )
    base, changed = summarise(
        [{"row": _row()}, {"row": _row(**{field: _ENTRY_GATE_ON[field]})}]
    )
    assert changed["entry"] != base["entry"], f"{field} can be set without the card saying so"


# --- Sizing -----------------------------------------------------------------
#
# The same bug a third time. The row rendered "$100 / trade" from `sizing_usd`
# while `atr.risk_usd` was what actually decided the size — sizing_usd being, as
# with trailing_stop_pct, only the fallback for when ATR can't be computed. A
# reader of the card could not tell which of the two modes was running.
#
# One thing here has no analogue in the exits: ATR sizing needs BOTH risk_usd
# and stop_mult (the size is derived from the stop distance). So risk_usd alone
# is set, saved, shown in the editor — and inert.

# The knobs that decide how much money goes into a position, and what may be
# exposed at once. Everything else on StrategyBody picks WHICH symbols or WHICH
# rules, and is named by another row of the same card.
_SIZING_MODELS = {
    "": StrategyBody,
    "params.atr.": ATRConfig,
    "params.execution.": ExecutionConfig,
}

_SIZING_EXCLUDED = {
    # Identity and provenance — not settings the engine acts on.
    "name": "the card's title",
    "notes": "shown in full under the card; never read by the engine",
    "preset": "a provenance label; changes no behaviour",
    "optimized_from_id": "the Lineage row",
    "optimized_days": "the Lineage row",
    # WHICH symbols, not how much of them.
    "asset_class": "the Trades row",
    "universe": "the Trades row",
    "basket_id": "the Trades row",
    "symbols": "the Trades row",
    "rank_by": "the Trades row",
    "rank_enabled": "the Trades row",
    "top_n": "the Trades row, and the Exit row's rank rotation",
    "swing_mode": "the Trades row",
    # WHICH rules, not how much.
    "params": "the Entry and Exit rows; its sizing blocks are enumerated above",
    "ignore_regime": "it blocks BUYS — the Entry row shows it",
    # ATR knobs that aren't about size.
    "params.atr.trail_mult": "the trailing stop — the Exit row shows it",
    "params.atr.period": "how ATR is measured, not how much is bought",
}

_SIZING_ON = {
    "sizing_usd": 250.0,  # default 200
    "sleeve_usd": 5000.0,  # default 1000
    "max_positions": 7,  # default 3
    "allow_concurrent_symbol": True,
    "params.atr.risk_usd": 50.0,
    "params.atr.stop_mult": 1.5,
    "params.execution.market_orders": True,
}

# risk_usd and stop_mult are ONE rule wearing two fields, like the entry window:
# a risk budget with no stop multiple sizes nothing, so they switch on together.
_SIZING_TOGETHER = {
    "params.atr.risk_usd": "params.atr.stop_mult",
    "params.atr.stop_mult": "params.atr.risk_usd",
}


def _row(**overrides) -> dict:
    """A default StrategyBody as JSON, with dotted overrides applied at any depth.

    This is what the card hands to sizingSummary — StrategyRow in the frontend,
    StrategyBody here, and the point of building it from the pydantic model is
    that a field added there shows up in these tests without anyone editing them.
    """
    row = StrategyBody(name="s", asset_class="stock").model_dump(mode="json")
    for key, value in overrides.items():
        node = row
        *path, leaf = key.split(".")
        for step in path:
            if node.get(step) is None:
                node[step] = {}
            node = node[step]
        node[leaf] = value
    return row


# A strategy sized by risk, not by a fixed dollar amount: $50 of risk per trade,
# stopped out at 1.5x the symbol's own ATR. The $100 is the fallback.
ATR_SIZED = _row(
    **{
        "sizing_usd": 100,
        "sleeve_usd": 1000,
        "max_positions": 3,
        "params.atr.period": 14,
        "params.atr.stop_mult": 1.5,
        "params.atr.risk_usd": 50,
    }
)


def test_atr_sizing_displaces_the_fixed_dollar_amount(summarise):
    """The reported row, end to end.

    Two claims, because the row made two mistakes at once: it named a number
    that decides nothing, and it never named the one that does.
    """
    (got,) = summarise([{"row": ATR_SIZED}])
    assert got["sizing"] == (
        "risk $50 / trade (1.5×ATR stop) · $1,000 sleeve & per-trade cap · max 3 positions"
    )
    assert "$100 / trade" not in got["sizing"]


def test_the_fixed_dollar_amount_stays_while_atr_sizing_is_off(summarise):
    """The fallback is not a lie when it is the thing in use.

    The obvious over-correction — dropping sizing_usd whenever an `atr` block
    exists — would blank the size for every strategy using only an ATR stop.
    """
    (got,) = summarise([{"row": _row(**{"sizing_usd": 100, "params.atr.stop_mult": 1.5})}])
    assert "$100 / trade" in got["sizing"]
    assert "ATR stop)" not in got["sizing"]


def test_a_risk_budget_with_no_atr_stop_is_named_as_unused(summarise):
    """risk_usd alone sizes nothing — the engine needs stop_mult to divide by.

    Saying nothing would leave the editor showing "Risk $ per trade: 50" beside
    a card implying it is in force, which is the failure this file exists for.
    """
    (got,) = summarise([{"row": _row(**{"sizing_usd": 100, "params.atr.risk_usd": 50})}])
    assert "$100 / trade" in got["sizing"]
    assert "ATR risk $50 unused — needs an ATR stop" in got["sizing"]


def test_market_orders_are_named_because_they_change_the_share_count(summarise):
    """Off, a $100 trade buys ZERO whole shares of a $400 stock and is skipped;
    on, it buys a quarter of one. Same $100 on the card, opposite outcomes.

    Crypto is fractional either way, so only the order type is claimed there —
    promising "fractional shares" would describe nothing.
    """
    on = {"params.execution.market_orders": True}
    stock, crypto = summarise(
        [{"row": _row(**on)}, {"row": _row(asset_class="crypto", **on)}]
    )
    assert "market orders, fractional shares" in stock["sizing"]
    assert "market orders" in crypto["sizing"]
    assert "fractional" not in crypto["sizing"]


@pytest.mark.parametrize(
    "field",
    sorted(
        f"{prefix}{name}"
        for prefix, model in _SIZING_MODELS.items()
        for name in model.model_fields
    ),
)
def test_every_sizing_field_that_is_switched_on_is_visible(summarise, field):
    if field in _SIZING_EXCLUDED:
        pytest.skip(f"deliberately omitted: {_SIZING_EXCLUDED[field]}")
    assert field in _SIZING_ON, (
        f"{field} is a new sizing/risk field. Render it in sizingSummary(), or "
        f"add it to _SIZING_EXCLUDED with the reason it does not size a position."
    )
    overrides = {field: _SIZING_ON[field]}
    partner = _SIZING_TOGETHER.get(field)
    if partner:
        overrides[partner] = _SIZING_ON[partner]
    base, changed = summarise([{"row": _row()}, {"row": _row(**overrides)}])
    assert changed["sizing"] != base["sizing"], f"{field} can be set without the card saying so"


def test_a_sleeve_with_no_exits_says_so(summarise):
    """A DCA sleeve may legally run with every exit off.

    It used to render `trail 0% · stop 0%`, which reads as a configured stop at
    the worst possible level rather than as no stop at all.
    """
    off = _params(**{"exit.trailing_stop_pct": 0, "exit.stop_loss_pct": 0})
    (got,) = summarise([{"params": off}])
    assert got["exit"] == "no exit rules — held"


# --- Indicator PERIODS, which are not rules but change what the rule means ----
#
# `require_macd_bullish` is one EntryRules field and it passed the invariant
# above from the day it was written — the card said "MACD bullish" and that was
# a visible change. What the invariant could not see is that the SIGNAL behind
# those words is configurable: `MACDConfig` allows fast/slow/signal anywhere in
# 1-100 / 2-200 / 1-100, so a 5/13/4 strategy and a 12/26/9 one produced
# identical cards while trading on different indicators.
#
# They stopped being cosmetic on 2026-08-07: `_daily_lookback_days` now sizes the
# daily-bars fetch from `slow + signal`, so the periods decide how much history
# is loaded and how long the warm-up runs.
_MACD_ON = {"fast": 5, "slow": 13, "signal": 4}


@pytest.mark.parametrize("field", sorted(MACDConfig.model_fields))
def test_every_macd_period_reaches_the_card(summarise, field):
    """Each period on its own, not all three at once: changing only `signal`
    must show, and a label built from `fast`/`slow` alone would hide it."""
    assert field in _MACD_ON, (
        f"MACDConfig.{field} is new. Render it in macdLabel(), or decide "
        f"explicitly that it cannot change what the signal means."
    )
    on = {"require_macd_bullish": True}
    base, changed = summarise([
        {"params": _params(**{"entry.require_macd_bullish": True})},
        {"params": _params(**{"entry.require_macd_bullish": True,
                              f"macd.{field}": _MACD_ON[field]})},
    ])
    assert changed["entry"] != base["entry"], (
        f"macd.{field} can be set without the card saying so")


def test_the_default_periods_are_not_spelled_out(summarise):
    """The common case stays short. Printing "(12/26/9)" on every MACD strategy
    would bury the one case worth noticing in noise."""
    (got,) = summarise([{"params": _params(**{"entry.require_macd_bullish": True})}])
    assert "MACD bullish" in got["entry"]
    assert "12/26/9" not in got["entry"]


def test_a_non_default_period_set_is_spelled_out(summarise):
    (got,) = summarise([{"params": _params(**{
        "entry.require_macd_bullish": True,
        "macd.fast": 5, "macd.slow": 13, "macd.signal": 4})}])
    assert "MACD bullish (5/13/4)" in got["entry"], got["entry"]


def test_the_exit_signal_names_its_periods_too(summarise):
    """Entry and exit read the SAME `params.macd`. Labelling one and not the
    other is how the pair drifts apart."""
    (got,) = summarise([{"params": _params(**{
        "exit.exit_on_macd_bearish": True,
        "macd.fast": 5, "macd.slow": 13, "macd.signal": 4})}])
    assert "MACD bearish (5/13/4)" in got["exit"], got["exit"]
