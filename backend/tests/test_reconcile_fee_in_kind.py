"""Alpaca charges crypto commission IN THE COIN, so the position that lands is
smaller than the order's own filled_qty. QT journals filled_qty, so every crypto
entry left the journal reading ~0.25% above the broker — and the reconciler,
whose tolerance is 0.0001%, reported each one as unexplained drift.

Werner's real alert, and the numbers that identified it:
    "QT's open trades total 2.1322 AAVE/USD but the broker holds 2.12687
     across 2 strategies — not auto-corrected."
    2.1322 -> 2.12687 is 0.249977%, Alpaca's crypto fee to five decimals.
"""

from qt.services.reconcile import (
    CRYPTO_FEE_IN_KIND_MAX_PCT,
    OpenTradeView,
    PositionView,
    reconcile,
)


def _crypto_trade(tid: int, qty: float, symbol: str = "AAVE/USD") -> OpenTradeView:
    return OpenTradeView(
        id=tid, symbol=symbol, qty=qty, entry_order_id=f"o-{tid}",
        entry_confirmed=True, last_price=100.0, asset_class="crypto",
    )


def _kinds(actions) -> list[str]:
    return [a.kind for a in actions]


def test_werners_actual_alert_is_recognised_as_the_fee(monkeypatch):
    """The exact numbers from the Slack message."""
    trades = [_crypto_trade(1, 1.0661), _crypto_trade(2, 1.0661)]  # sums to 2.1322
    actions = reconcile(trades, [PositionView(symbol="AAVEUSD", qty=2.12687)], [])

    assert "alert_qty_mismatch" not in _kinds(actions)
    adjust = [a for a in actions if a.kind == "adjust_qty_fee_in_kind"]
    assert len(adjust) == 2, "both strategies paid the fee, so both are corrected"
    # Scaled in proportion, and the total now matches the broker exactly.
    assert abs(sum(a.qty for a in adjust) - 2.12687) < 1e-9
    assert abs(adjust[0].qty - adjust[1].qty) < 1e-12  # equal entries, equal shares


def test_a_surplus_is_never_treated_as_a_fee():
    """A fee cannot give coins back. More at the broker than QT thinks is real
    drift and must still be reported."""
    actions = reconcile([_crypto_trade(1, 2.0)], [PositionView(symbol="AAVEUSD", qty=2.004)], [])
    assert "alert_qty_mismatch" in _kinds(actions)
    assert "adjust_qty_fee_in_kind" not in _kinds(actions)


def test_a_shortfall_too_large_to_be_a_fee_is_still_reported():
    """The band is the whole safeguard: absorb more than a fee and a genuinely
    mis-journalled position disappears into the noise."""
    over = CRYPTO_FEE_IN_KIND_MAX_PCT * 3
    actions = reconcile(
        [_crypto_trade(1, 2.0)], [PositionView(symbol="AAVEUSD", qty=2.0 * (1 - over / 100))], []
    )
    assert "alert_qty_mismatch" in _kinds(actions)
    assert "adjust_qty_fee_in_kind" not in _kinds(actions)


def test_stocks_are_never_silently_adjusted():
    """US equities are commission-free and pay nothing in kind, so the same
    shortfall on a stock is drift and must keep alerting."""
    stock = OpenTradeView(
        id=1, symbol="NVDA", qty=100.0, entry_order_id="o-1",
        entry_confirmed=True, last_price=100.0, asset_class="stock",
    )
    actions = reconcile([stock], [PositionView(symbol="NVDA", qty=99.75)], [])
    assert "alert_qty_mismatch" in _kinds(actions)
    assert "adjust_qty_fee_in_kind" not in _kinds(actions)


def test_an_exact_match_still_does_nothing():
    """The quiet path has to stay quiet, or this would fire on every symbol."""
    actions = reconcile([_crypto_trade(1, 2.0)], [PositionView(symbol="AAVEUSD", qty=2.0)], [])
    assert _kinds(actions) == []
