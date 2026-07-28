"""Pure MACD math (qt.services.stats.macd) — validated against a hand-computed
reference vector plus directional/edge properties. Look-ahead safety is the
caller's job (pass only completed closes); here we pin the arithmetic."""

from qt.services import stats


def test_macd_matches_hand_computed_reference():
    # closes=[1,2,3,4,5], fast=2 slow=3 signal=2 — worked out by hand:
    #   EMA2 = [-, 1.5, 2.5, 3.5, 4.5]; EMA3 = [-, -, 2, 3, 4]
    #   MACD line = [0.5, 0.5, 0.5]; signal(EMA2) = [-, 0.5, 0.5]
    #   => line 0.5, signal 0.5, histogram 0.0
    assert stats.macd([1, 2, 3, 4, 5], fast=2, slow=3, signal=2) == (0.5, 0.5, 0.0)


def test_macd_constant_series_is_flat():
    line, sig, hist = stats.macd([10.0] * 40)  # default 12/26/9
    assert (line, sig, hist) == (0.0, 0.0, 0.0)
    assert stats.macd_bullish([10.0] * 40) is False  # 0 is not > 0


def test_macd_bullish_on_accelerating_uptrend():
    rising = [round(1.05 ** i, 4) for i in range(40)]  # compounding growth
    line, sig, _ = stats.macd(rising)
    assert line > sig  # line leads its signal
    assert stats.macd_bullish(rising) is True


def test_macd_bearish_on_accelerating_downtrend():
    # Prices fall with GROWING steps (momentum deteriorating), so the MACD line
    # sits below its signal — a decelerating decline would instead be recovering.
    falling = [round(100 - 1.05 ** i, 4) for i in range(40)]
    line, sig, _ = stats.macd(falling)
    assert line < 0 and line < sig
    assert stats.macd_bullish(falling) is False


def test_macd_none_without_enough_history():
    assert stats.macd([1, 2, 3]) is None           # far fewer than slow+signal
    assert stats.macd_bullish([1, 2, 3]) is None
    # Exactly one short of the slow+signal minimum still returns None.
    assert stats.macd([float(i) for i in range(34)]) is None
