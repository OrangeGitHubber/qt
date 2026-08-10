"""A live strategy must not look like a paper one in a list.

Step four of live trading. The plan named this as a requirement in its own right:

    "Per-strategy visual distinction in the UI, unmistakable at a glance in every
     list, card and table. A live strategy sitting in the same list as a paper
     one, distinguishable only by a small label, is the failure mode to design
     against."

That is a real hazard rather than a styling preference. Modes now run SIDE BY
SIDE — that was the whole point of stage one — so the strategies list is where a
live row and a paper row sit next to each other, and scanning past the difference
is how you edit, pause or promote the wrong one.

FOUR CHANNELS, because any single one can be lost. Colour goes for a colour-blind
reader; colour AND emphasis go in a greyscale screenshot pasted into Slack; an
icon can be stripped by a font that lacks the glyph; a label can be truncated in
a narrow column. Live has to remain distinguishable when any one of them is gone,
so this file asserts each channel separately rather than asserting "the badge
looks right".

EXECUTES THE SHIPPED TypeScript, via node's type-stripping — the same technique
test_strategy_summary.py uses, and for the same reason: a regex approximation of
the frontend would pass while the frontend was wrong.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FORM_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "strategyForm.ts"

_DRIVER = """
import { readFileSync } from "node:fs";
import { modeBadge } from %(module)s;

const modes = JSON.parse(readFileSync(process.argv[2], "utf8"));
process.stdout.write(JSON.stringify(modes.map((m) => modeBadge(m))));
"""


@pytest.fixture(scope="module")
def badge(tmp_path_factory):
    node = shutil.which("node")
    if node is None:  # pragma: no cover — CI installs node for this job
        pytest.fail("node is required to test the frontend mode badge")
    work = tmp_path_factory.mktemp("badge")
    driver = work / "driver.mjs"
    driver.write_text(_DRIVER % {"module": json.dumps(FORM_TS.as_uri())}, encoding="utf8")

    def run(modes: list) -> list[dict]:
        payload = work / "modes.json"
        payload.write_text(json.dumps(modes), encoding="utf8")
        proc = subprocess.run(
            [node, "--experimental-strip-types", str(driver), str(payload)],
            capture_output=True, text=True, encoding="utf8",
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


def test_the_three_modes_differ_on_every_channel(badge):
    """The core claim. Two modes sharing ANY channel means that channel does no
    work, and the whole point is redundancy."""
    shadow, paper, live = badge(["shadow", "paper", "live"])
    for field in ("label", "tone", "icon"):
        values = [shadow[field], paper[field], live[field]]
        assert len(set(values)) == 3, f"{field} does not distinguish the modes: {values}"
        # DISTINCT IS NOT ENOUGH — an EMPTY value is distinct from two real ones,
        # so blanking live's icon passed this test while deleting the very
        # channel live most needs. Each mode must actually carry each channel.
        assert all(str(v).strip() for v in values), f"{field} is blank on some mode: {values}"


def test_live_is_the_only_emphasised_one(badge):
    """Emphasis is the channel that survives greyscale. If paper carried it too,
    the flag would stop meaning "real money"."""
    shadow, paper, live = badge(["shadow", "paper", "live"])
    assert live["emphasise"] is True
    assert paper["emphasise"] is False
    assert shadow["emphasise"] is False


def test_live_reads_as_live_without_colour(badge):
    """The greyscale / colour-blind case, stated on its own: with `tone`
    discarded, the LABEL alone must still say which one is real."""
    shadow, paper, live = badge(["shadow", "paper", "live"])
    assert live["label"] == "LIVE"
    assert live["label"] != paper["label"] and live["label"] != shadow["label"]
    # Upper case as well as different text — the one that survives a narrow
    # column truncating everything after four characters.
    assert live["label"].isupper()
    assert not paper["label"].isupper()


def test_live_is_the_only_red_one(badge):
    """Colour is the channel most people actually read first."""
    tones = {m: b["tone"] for m, b in zip(["shadow", "paper", "live"],
                                          badge(["shadow", "paper", "live"]))}
    assert tones["live"] == "red"
    assert "red" not in (tones["paper"], tones["shadow"])


def test_every_mode_explains_itself_on_hover(badge):
    """The list is where someone decides what to change. "Paper" and "Shadow"
    are not self-explanatory to a novice, and the difference between them is
    whether an order was ever placed."""
    shadow, paper, live = badge(["shadow", "paper", "live"])
    assert "REAL MONEY" in live["title"]
    assert "No real money" in paper["title"]
    assert "no orders" in shadow["title"].lower()
    assert len({shadow["title"], paper["title"], live["title"]}) == 3


@pytest.mark.parametrize("junk", [None, "", "  ", "nonsense", "LIVE!", "off", "prod"])
def test_an_unrecognised_mode_reads_as_shadow(badge, junk):
    """Never as live. A row whose mode is missing, misspelled or from a later
    version must imply the SAFEST thing — and it matches the engine, where an
    unknown mode resolves to off and trades nothing. Reading junk as live would
    be alarming but harmless; reading it as paper when it is live is the
    dangerous direction, and defaulting to shadow avoids both."""
    got = badge([junk])[0]
    assert got["label"] == "Shadow", junk
    assert got["emphasise"] is False


def test_case_and_whitespace_do_not_change_the_badge(badge):
    """The value survives a JSON round trip and hand editing. ' LIVE ' rendering
    as Shadow would be the worst possible failure of this function."""
    for variant in [" live ", "LIVE", "Live"]:
        assert badge([variant])[0]["label"] == "LIVE", variant


def test_the_backend_and_the_badge_agree_on_the_vocabulary():
    """The two sides are edited independently. A mode the engine accepts but the
    badge has never heard of renders as Shadow — a live strategy displayed as the
    safest thing there is."""
    from qt.services.engine import STRATEGY_MODES

    rendered = {"shadow", "paper", "live"}
    assert set(STRATEGY_MODES) == rendered, (
        f"engine modes {set(STRATEGY_MODES)} do not match what modeBadge renders")


# ── alerts name the mode they are about ──────────────────────────────────────
_MODE_LABELLED_MODULES = [
    ("qt.services.execution", "every entry, exit and failed-exit alert"),
    ("qt.api.engine", "the force-close alert"),
]


@pytest.mark.parametrize("module,what", _MODE_LABELLED_MODULES)
def test_no_alert_hardcodes_a_mode_name(module, what):
    """Slack messages must name the mode of the TRADE they describe.

    Three alerts in execution.py said `*PAPER*` as a literal, and all three sit
    on failure paths. The worst is "cannot exit — the position needs closing by
    hand": on a live trade that is a real position you have to go and close
    yourself, announced under a label that says it is not real. A missing mode
    is a gap; a WRONG one is worse than silence, because it is believed.

    Asserted against the source because the alternative is driving every failure
    path through a broker. That is a weaker test than exercising them — it would
    not catch a message that named the wrong VARIABLE — so it is paired with the
    mutation runs recorded in the commit rather than trusted alone."""
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(module))
    for literal in ('*PAPER*', '*LIVE*', '*SHADOW*', '[paper]', '[live]', '[shadow]'):
        assert literal not in src, (
            f"{module} hardcodes {literal} — {what} must read the trade's own mode")


def test_every_execution_slack_message_names_a_mode():
    """The control for the above: deleting every mode reference would also pass a
    "no hardcoded literal" check while leaving the alerts unlabelled.

    Written as "every slack_cat call in this module mentions a mode" rather than
    as a COUNT. The count version asserted `>= 3` against four labelled alerts,
    so dropping one still passed — a threshold that cannot fail for the case it
    was written to catch. This one grows with the file instead."""
    import inspect
    import re

    from qt.services import execution

    src = inspect.getsource(execution)
    calls = [m.start() for m in re.finditer(r"notify\.slack_cat\(", src)]
    assert calls, "no Slack alerts found — this test would be vacuous"
    for start in calls:
        # The call spans a few lines; take enough to cover the message argument.
        block = src[start:start + 600].split(")\n")[0]
        assert "mode" in block, (
            f"a Slack alert does not name its mode:\n{block[:220]}")


def test_the_force_close_alert_names_the_mode():
    import inspect

    from qt.api import engine as engine_api

    src = inspect.getsource(engine_api.force_close_position)
    assert "trade.mode.upper()" in src
