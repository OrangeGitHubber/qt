"""Every `InfoTip k="…"` must name a glossary entry that exists.

There was no test here at all, and the failure mode is silent: `InfoTip` with a
key the glossary does not define renders NOTHING. No console error, no missing
box — the little "?" simply isn't there, and the explanation the header was
supposed to carry is gone. A typo, a renamed key, or a deleted entry all look
identical to "we decided this one didn't need a tooltip".

That is exactly the shape of the bug this file was written alongside: the
Today/Day column meant two different things (previous session close for stocks,
rolling 24 hours for crypto) and the only explanation lived in `glossary.ts`
with nothing linking to it. Adding the link is one line; keeping every link
honest is this.

The key set is read by EXECUTING glossary.ts through node rather than by
pattern-matching it, so what is checked is the object the app actually builds.
The usages have to be scanned from source — there is no runtime moment at which
every `InfoTip` in the codebase has been rendered.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
GLOSSARY_TS = FRONTEND / "glossary.ts"

# Three ways a key reaches InfoTip, and the scan has to see all of them or it is
# vacuously green:
#   k="literal"      the direct form
#   tip="literal"    a wrapper (Field/Row/Metric) that forwards its prop as k={tip}
#   tip: "literal"   the same key living in an options array
# The forwarding wrappers are why the first version of this file was wrong: it
# asserted there were no computed keys, and there are four — every one of them
# `k={tip}` fed by a literal elsewhere in the same file.
_LITERAL = re.compile(r'<InfoTip\b[^>]*?\bk="([A-Za-z0-9_]+)"')
_FORWARDED = re.compile(r'\btip[=:]\s*"([A-Za-z0-9_]+)"')
# Any computed form OTHER than forwarding a `tip` prop would be invisible again.
_COMPUTED_OK = re.compile(r'<InfoTip\b[^>]*?\bk=\{\s*(?:tip|[A-Za-z_][A-Za-z0-9_]*\.tip)\s*\}')
_COMPUTED_ANY = re.compile(r'<InfoTip\b[^>]*?\bk=\{([^}]*)\}')

_DRIVER = """
import { GLOSSARY } from %(module)s;
process.stdout.write(JSON.stringify(Object.keys(GLOSSARY)));
"""


@pytest.fixture(scope="module")
def glossary_keys(tmp_path_factory):
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI installs node for this job
        pytest.fail("node is required to read the glossary (see ci.yml)")
    work = tmp_path_factory.mktemp("glossary")
    driver = work / "driver.mjs"
    driver.write_text(_DRIVER % {"module": json.dumps(GLOSSARY_TS.as_uri())}, encoding="utf8")
    proc = subprocess.run([node, str(driver)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return set(json.loads(proc.stdout))


def _sources():
    return [p for p in FRONTEND.rglob("*.tsx")] + [p for p in FRONTEND.rglob("*.ts")]


def _usages():
    found: dict[str, list[str]] = {}
    for path in _sources():
        text = path.read_text(encoding="utf8", errors="replace")
        for key in _LITERAL.findall(text) + _FORWARDED.findall(text):
            found.setdefault(key, []).append(path.name)
    return found


def test_every_infotip_key_exists(glossary_keys):
    """THE INVARIANT. A missing key renders nothing at all, so this cannot be
    caught by looking at the page — only by comparing the two sides."""
    missing = {k: v for k, v in _usages().items() if k not in glossary_keys}
    assert not missing, (
        "InfoTip references a glossary entry that does not exist: "
        + ", ".join(f"{k} (in {', '.join(sorted(set(v)))})" for k, v in sorted(missing.items()))
    )


def test_the_scan_actually_found_tooltips():
    """A regex that matches nothing would make the test above vacuously true —
    and vacuously green is exactly how this gap stayed open."""
    usages = _usages()
    assert len(usages) > 20, f"only found {len(usages)} InfoTip keys; the scan is broken"


def test_the_only_computed_key_is_a_forwarded_prop():
    """`k={expr}` cannot be resolved from source, so the scan can only stay
    complete while every computed call is a wrapper forwarding a literal `tip`
    (which `_FORWARDED` does see). Any other expression makes a key invisible
    again, and this fails rather than letting coverage quietly drop."""
    unknown = []
    for path in _sources():
        text = path.read_text(encoding="utf8", errors="replace")
        for expr in _COMPUTED_ANY.findall(text):
            if not _COMPUTED_OK.search(f"<InfoTip k={{{expr}}}"):
                unknown.append(f"{path.name}: k={{{expr.strip()}}}")
    assert not unknown, (
        "InfoTip is called with a key this scan cannot resolve: "
        + "; ".join(unknown))


def test_the_forwarded_scan_finds_the_wrapper_keys():
    """The wrappers carry ~30 keys between them. If `_FORWARDED` stops matching,
    `test_every_infotip_key_exists` silently stops checking most of the app."""
    forwarded = set()
    for path in _sources():
        forwarded.update(_FORWARDED.findall(path.read_text(encoding="utf8", errors="replace")))
    assert len(forwarded) > 15, f"only {len(forwarded)} forwarded keys found"


def test_every_entry_is_filled_in(glossary_keys, tmp_path_factory):
    """An entry with an empty explanation is the same failure wearing a
    different hat: the "?" appears and teaches nothing."""
    node = shutil.which("node")
    work = tmp_path_factory.mktemp("gloss2")
    driver = work / "d.mjs"
    driver.write_text(
        "import { GLOSSARY } from %s;\n"
        "process.stdout.write(JSON.stringify(Object.entries(GLOSSARY)"
        ".filter(([, v]) => !v.term?.trim() || !v.explain?.trim()).map(([k]) => k)));"
        % json.dumps(GLOSSARY_TS.as_uri()), encoding="utf8")
    proc = subprocess.run([node, str(driver)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_the_day_change_column_is_explained(glossary_keys):
    """The reason this file exists. "Today" on the Scanner and "Day" on a
    strategy's decision table are the SAME number measured two different ways —
    previous session close for a stock, rolling 24 hours for crypto — and the
    entry rule that reads it (Min gain today) inherits the split."""
    assert "day_change" in glossary_keys
    usages = _usages()
    assert "day_change" in usages, "no header links to the explanation"
    assert {"Scanner.tsx", "Strategies.tsx"} <= set(usages["day_change"]), (
        f"only linked from {sorted(set(usages['day_change']))}")
