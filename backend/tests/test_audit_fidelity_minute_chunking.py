"""Cutting a long comparison into pieces a MINUTE replay can cover.

The live engine decides every 60 seconds. `_timeframe_for` only asks for minute
bars while a stretch fits inside MAX_HOURS_FOR_MINUTE_REPLAY — and a comparison's
window grows for as long as the strategy stays switched on. So a strategy live
for a day and a bit silently dropped to 15-minute bars and began sampling
fifteen times less often than the engine it was grading.

Measured: strategy 25 compared clean at 1Min over 12 hours; overnight the same
stretch reached 26 hours, fell to 15Min, and three real trades came back as "the
replay missed it — this is the kind that points at a real bug".
"""

from datetime import datetime, timedelta, timezone

from qt.api.backtest import MAX_HOURS_FOR_MINUTE_REPLAY
from qt.api.fidelity import _chunk_for_minute_replay, _config_stretches, _Segment

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
CAP = timedelta(hours=MAX_HOURS_FOR_MINUTE_REPLAY)


def _seg(start_h: float, end_h: float, version: int = 1) -> _Segment:
    return _Segment(
        start=T0 + timedelta(hours=start_h), end=T0 + timedelta(hours=end_h),
        config={"name": f"v{version}"}, version_no=version,
        symbols=["AAA"], scanner_replay=False,
    )


def _trade(hours: float) -> dict:
    return {"entry_at": (T0 + timedelta(hours=hours)).isoformat()}


def test_a_short_stretch_is_left_exactly_as_it_was():
    """Anti-vacuity: chunking must not fire on the common case."""
    segs = [_seg(0, 5)]
    assert _chunk_for_minute_replay(segs, [_trade(1)]) == segs


def test_a_long_stretch_with_a_trade_is_cut_to_something_minute_bars_can_cover():
    out = _chunk_for_minute_replay([_seg(0, 40)], [_trade(1), _trade(30)])
    assert len(out) > 1
    assert all(s.end - s.start <= CAP for s in out), \
        "a piece longer than the cap cannot get minute bars — the whole point"


def test_trade_free_stretches_are_left_long_so_nobody_pays_for_minute_bars():
    """Minute bars run 1,440 per symbol per day. A strategy live for months must
    not ask for millions of them to compare a handful of trades."""
    out = _chunk_for_minute_replay([_seg(0, 24 * 30)], [_trade(1)])
    assert len(out) == 2, [(s.start, s.end) for s in out]
    assert out[0].end - out[0].start <= CAP        # the piece holding the trade
    assert out[1].end - out[1].start > CAP         # the empty month, left whole


def test_the_piece_holding_the_trade_is_the_one_kept_small():
    out = _chunk_for_minute_replay([_seg(0, 24 * 5)], [_trade(80)])
    holding = [s for s in out if s.start <= T0 + timedelta(hours=80) < s.end]
    assert len(holding) == 1
    assert holding[0].end - holding[0].start <= CAP


def test_each_piece_keeps_its_own_configuration():
    """A stretch replayed under version 2 must stay version 2 — chunking is about
    resolution, and must never quietly re-date a configuration."""
    out = _chunk_for_minute_replay([_seg(0, 40, version=2)], [_trade(1), _trade(30)])
    assert {s.version_no for s in out} == {2}
    assert all(s.symbols == ["AAA"] for s in out)


def test_the_pieces_tile_the_original_span_without_gap_or_overlap():
    out = _chunk_for_minute_replay([_seg(0, 100)], [_trade(5), _trade(60)])
    assert out[0].start == T0
    assert out[-1].end == T0 + timedelta(hours=100)
    for a, b in zip(out, out[1:]):
        assert a.end == b.start, "a gap or overlap would lose or double-count trades"


def test_only_the_first_piece_counts_as_a_configuration_stretch():
    """`segmented` means "your strategy changed mid-window". Cutting for
    resolution must not start claiming edits the user never made."""
    out = _chunk_for_minute_replay([_seg(0, 40)], [_trade(1), _trade(30)])
    assert out[0].resolution_chunk is False
    assert all(s.resolution_chunk for s in out[1:])
    assert len(_config_stretches(out)) == 1


def test_two_real_configurations_stay_two_after_chunking():
    out = _chunk_for_minute_replay(
        [_seg(0, 40, version=1), _seg(40, 80, version=2)],
        [_trade(1), _trade(30), _trade(50)],
    )
    stretches = _config_stretches(out)
    assert [s.version_no for s in stretches] == [1, 2]
    assert stretches[0].start == T0
    assert stretches[-1].end == T0 + timedelta(hours=80)
