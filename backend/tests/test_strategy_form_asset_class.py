"""Three form decisions that all got the asset class wrong by not asking.

Each was written inline in JSX, each looked obviously right, and each was wrong
for exactly one of the two asset classes:

- the entry-window checkbox seeded 09:30–15:30 on a 24/7 book, silently
  discarding about three quarters of the day and undoing the clear that
  `setAssetClass` performs for that very reason;
- two warnings named the "Trading style" control as the remedy on crypto
  strategies, where that control is not rendered at all;
- the fee note branched on `feesPaid === 0`, which is also true of a CRYPTO run
  that took no trades — so it announced that Alpaca charges no commission, about
  the largest single cost a crypto strategy carries.

Like `test_strategy_summary.py` this EXECUTES the shipped module through node's
type-stripping rather than pattern-matching the source, so what is pinned is the
behaviour the browser gets.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FORM_TS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "strategyForm.ts"
)

_DRIVER = """
import { readFileSync } from "node:fs";
import { defaultEntryWindow, stockOnlyWarnings, feeNote } from %(module)s;

const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const out = cases.map((c) => {
  if (c.kind === "window") return defaultEntryWindow(c.assetClass);
  if (c.kind === "warnings") return stockOnlyWarnings(c.inputs);
  return { note: feeNote(c.assetClass, c.feesPaid) };
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def run_form(tmp_path_factory):
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI installs node for this job
        pytest.fail(
            "node is required to test the frontend form helpers "
            "(see .github/workflows/ci.yml, backend-tests job)"
        )
    work = tmp_path_factory.mktemp("form")
    driver = work / "driver.mjs"
    driver.write_text(_DRIVER % {"module": json.dumps(FORM_TS.as_uri())}, encoding="utf8")

    def run(cases):
        payload = work / "cases.json"
        payload.write_text(json.dumps(cases), encoding="utf8")
        proc = subprocess.run(
            [node, str(driver), str(payload)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


# ── the entry window ─────────────────────────────────────────────────────────
def test_a_stock_window_is_the_us_session(run_form):
    (got,) = run_form([{"kind": "window", "assetClass": "stock"}])
    assert got == {"start": "09:30", "end": "15:30"}


def test_a_crypto_window_starts_at_the_whole_day(run_form):
    """THE BUG. 09:30-15:30 on a 24/7 market throws away about three quarters of
    it, and `setAssetClass` clears the pair on switching to crypto for exactly
    that reason — which the checkbox then undid."""
    (got,) = run_form([{"kind": "window", "assetClass": "crypto"}])
    assert got == {"start": "00:00", "end": "23:59"}, (
        "crypto was seeded a US session window")


def test_the_two_classes_do_not_share_a_window(run_form):
    """Stated as its own claim: a single default for both is the defect, so
    equality here is a failure however plausible either value looks."""
    stock, crypto = run_form([{"kind": "window", "assetClass": "stock"},
                              {"kind": "window", "assetClass": "crypto"}])
    assert stock != crypto


# ── the stock-only warnings ──────────────────────────────────────────────────
def _w(**over):
    base = {"assetClass": "stock", "swingMode": True, "stopPct": 2.0,
            "requireAboveVwap": True}
    base.update(over)
    return {"kind": "warnings", "inputs": base}


def test_both_warnings_fire_for_a_stock(run_form):
    """THE CONTROL. Suppress too much and a real misconfiguration goes unflagged,
    which is worse than the bug being fixed."""
    (got,) = run_form([_w()])
    assert got == {"tightSwingStop": True, "vwapOnSwing": True}


def test_neither_warning_fires_for_crypto(run_form):
    """Both name the Trading-style control, which crypto never renders. Identical
    inputs, one field changed."""
    (got,) = run_form([_w(assetClass="crypto")])
    assert got == {"tightSwingStop": False, "vwapOnSwing": False}


@pytest.mark.parametrize("stop,expected", [
    (0.0, False),    # 0 = no stop configured at all, not a tight one
    (2.9, True),
    (3.0, False),    # the boundary is exclusive
    (8.0, False),
])
def test_the_tight_stop_threshold(run_form, stop, expected):
    (got,) = run_form([_w(stopPct=stop)])
    assert got["tightSwingStop"] is expected


def test_an_intraday_stock_gets_neither_warning(run_form):
    """Both are about holding OVERNIGHT. Intraday is the remedy they name, so
    firing them once it is selected would be advice to do what you already did."""
    (got,) = run_form([_w(swingMode=False)])
    assert got == {"tightSwingStop": False, "vwapOnSwing": False}


def test_vwap_is_independent_of_the_stop(run_form):
    """Two rules, not one: a wide stop must not silence the VWAP warning."""
    (got,) = run_form([_w(stopPct=8.0, requireAboveVwap=True)])
    assert got == {"tightSwingStop": False, "vwapOnSwing": True}


# ── the fee note ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("asset,fees,expected", [
    ("crypto", 12.5, "charged"),
    ("stock", 12.5, "charged"),
    ("stock", 0.0, "stock-free"),
    ("crypto", 0.0, "crypto-untraded"),
])
def test_the_fee_note_branches_on_asset_class(run_form, asset, fees, expected):
    """THE BUG is the last row. A crypto run with no trades has `feesPaid == 0`
    and was told Alpaca charges no commission — the opposite of the truth about
    0.25% a side, which is roughly 98% of BTC's entire round trip."""
    (got,) = run_form([{"kind": "fee", "assetClass": asset, "feesPaid": fees}])
    assert got["note"] == expected


def test_a_crypto_run_that_traded_is_not_called_untraded(run_form):
    """The two zero-fee cases are distinguished by asset class; the two
    crypto cases by whether anything traded. Both axes matter."""
    traded, untraded = run_form([
        {"kind": "fee", "assetClass": "crypto", "feesPaid": 0.01},
        {"kind": "fee", "assetClass": "crypto", "feesPaid": 0.0},
    ])
    assert traded["note"] == "charged" and untraded["note"] == "crypto-untraded"
