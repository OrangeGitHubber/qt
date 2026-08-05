"""The basket sweep tests YOUR strategy, not an internal template.

MEASURED complaint: Werner swept "the trend setter template", was told Health
Care looked good, clicked Create draft — and got a strategy with no ATR settings
and a stop-loss he did not recognise.

He had found a real gap. `SweepBody` carried no strategy id at all; the sweep ran
a hardcoded plain-momentum config (min day gain 2%, 5% trail, 4% stop, no `atr`
block) against every basket, and the draft saved THAT. So the answer to "which
basket suits my trend follower" was actually "which basket suits a momentum
template you never configured", and the two are not the same question — different
entries, different ranking, different stops.

His framing of the feature is the right one: an easy test of one strategy against
every basket, saving you from editing the universe fifteen times.
"""

import json

import pytest

from qt.models import Strategy
from qt.services import sweep


def _fake_optimize(strategy, bars, risk, **kw):
    """Records the config it was handed and returns a minimal result shape."""
    _fake_optimize.seen.append(strategy)
    return {
        "best": {"params": strategy["params"], "in_sample": {"net_pnl_pct": 1.0},
                 "out_of_sample": {"net_pnl_pct": 2.0, "trades": 3, "entries": 3,
                                   "win_rate": 60.0, "max_drawdown_pct": 5.0}},
        "best_draft_params": strategy["params"],
        "out_of_sample_window": {"start": "2025-01-01T00:00:00Z", "end": "2025-02-01T00:00:00Z"},
        "tested_combinations": 1, "search_space_size": 1,
    }


@pytest.fixture(autouse=True)
def _reset():
    """Both the recorder AND the module-level run flags.

    `_sweep_progress` / `_progress` are singletons shared by every test in the
    session, so a run left flagged by another file makes the endpoint answer 409
    ("already running") instead of the validation error under test — which reads
    as a broken endpoint and is contamination."""
    from qt.api import optimizer as opt_api

    _fake_optimize.seen = []
    opt_api._sweep_progress.running = False
    opt_api._progress.running = False


def _bars(n=120):
    return [{"t": f"2025-01-{(i % 28) + 1:02d}T20:00:00Z", "o": 100.0 + i, "h": 101.0 + i,
             "l": 99.0 + i, "c": 100.0 + i, "v": 1e6, "vw": 100.0 + i} for i in range(n)]


BASKETS = [{"id": 1, "name": "Health Care", "symbols": ["AAA", "BBB"]}]
BARS = {"AAA": _bars(), "BBB": _bars(), "SPY": _bars()}

MINE = {
    "name": "My trend follower",
    "asset_class": "stock", "universe": "custom",
    "rank_enabled": True, "rank_by": "macd_slope", "top_n": 8,
    "swing_mode": True, "sizing_usd": 800.0, "sleeve_usd": 4000.0, "max_positions": 5,
    "params": {
        "entry": {"min_day_gain_pct": 1.0, "require_macd_bullish": True},
        "exit": {"stop_loss_pct": 6.0, "trailing_stop_pct": 12.0},
        "atr": {"period": 14, "stop_mult": 3.0, "trail_mult": 3.5},
    },
}


def test_the_sweep_searches_the_strategy_it_was_given():
    """THE fault. Every basket must be scored on the user's own config."""
    sweep.sweep_baskets(BASKETS, BARS, {}, strategy=MINE, optimize_fn=_fake_optimize)

    assert _fake_optimize.seen, "the search never ran"
    used = _fake_optimize.seen[0]
    assert used["params"]["atr"]["stop_mult"] == 3.0
    assert used["params"]["atr"]["trail_mult"] == 3.5
    assert used["params"]["entry"]["require_macd_bullish"] is True
    assert used["params"]["exit"]["stop_loss_pct"] == 6.0


def test_the_ranking_cut_is_part_of_what_gets_tested():
    """rank_enabled / rank_by / top_n change which names are even eligible, so
    dropping them would score every basket against a more generous world than
    the strategy actually trades in."""
    sweep.sweep_baskets(BASKETS, BARS, {}, strategy=MINE, optimize_fn=_fake_optimize)
    used = _fake_optimize.seen[0]
    assert used["rank_enabled"] is True
    assert used["rank_by"] == "macd_slope"
    assert used["top_n"] == 8


def test_the_strategys_own_universe_is_not_carried_into_the_sweep():
    """Each iteration is scored on ONE basket's bars. A basket universe pointing
    at some other basket would be a stale label on every row."""
    basket_strategy = {**MINE, "universe": "basket", "basket_id": 99}
    sweep.sweep_baskets(BASKETS, BARS, {}, strategy=basket_strategy,
                        optimize_fn=_fake_optimize)
    assert _fake_optimize.seen[0]["universe"] == "custom"


def test_the_draft_params_carry_the_strategys_blocks():
    """best_draft_params is what Create draft saves. If the ATR block is missing
    here, the draft loses it — which is exactly what was reported."""
    res = sweep.sweep_baskets(BASKETS, BARS, {}, strategy=MINE,
                              optimize_fn=_fake_optimize)
    draft = res["rows"][0]["best_draft_params"]
    assert draft["atr"]["stop_mult"] == 3.0
    assert draft["exit"]["stop_loss_pct"] == 6.0


def test_the_reported_sizing_is_the_strategys_own():
    """The UI prints this as a caveat under the table. Printing the old fixed
    template's $1,000/$5,000 beside rows scored at the strategy's real size would
    be a caption describing a different run."""
    res = sweep.sweep_baskets(BASKETS, BARS, {}, strategy=MINE,
                              optimize_fn=_fake_optimize)
    assert res["template_sizing"]["sizing_usd"] == 800.0
    assert res["template_sizing"]["sleeve_usd"] == 4000.0
    assert res["template_sizing"]["max_positions"] == 5
    assert res["swept_strategy"] == "My trend follower"


def test_no_strategy_falls_back_to_the_plain_template():
    """Kept deliberately: "do any of these themes have momentum at all" is still
    a fair question, and the fallback is what answers it."""
    res = sweep.sweep_baskets(BASKETS, BARS, {}, optimize_fn=_fake_optimize)
    used = _fake_optimize.seen[0]
    assert "atr" not in used["params"]
    assert res["template_sizing"]["sizing_usd"] == sweep.TEMPLATE_SIZING["sizing_usd"]


def test_the_request_body_requires_a_strategy():
    """Without one the sweep silently answers a different question.

    Asserted on the SCHEMA rather than through the endpoint: `require_client`
    resolves before body validation, so on a box with no broker configured the
    route answers 409 ("Alpaca is not configured") and never reaches the field
    check — which would make this test pass or fail on setup state rather than on
    the thing it is about."""
    from pydantic import ValidationError

    from qt.api.optimizer import SweepBody

    with pytest.raises(ValidationError) as exc:
        SweepBody(days=365, iterations=5)
    assert "strategy_id" in str(exc.value)

    assert SweepBody(strategy_id=7).strategy_id == 7
