"""Top-N ranking for basket universes — a PURE function so it's unit-tested
without a broker.

The three metrics are all derived from price data QT already computes:
  - momentum_today     : % change vs yesterday's close (from the snapshot)
  - return_30d         : % change over ~30 calendar days (from daily bars)
  - relative_strength  : % above/below the 200-day moving average (daily bars)
                         — the same trend test the regime filter applies to SPY,
                         per symbol. Higher = further above its own long trend.

Ranking is descending (bigger metric = better) with a deterministic tie-break
on symbol, and symbols whose chosen metric is missing (None) are dropped — you
cannot rank on data you don't have.
"""

RANK_METRICS = ("momentum_today", "return_30d", "relative_strength")


def rank_symbols(
    metrics: dict[str, dict[str, float | None]], rank_by: str, top_n: int
) -> list[tuple[str, float]]:
    """Given {symbol: {metric: value|None}}, return the top `top_n` symbols by
    `rank_by` as (symbol, value) pairs, best first.

    - Missing/None values for the chosen metric drop the symbol entirely.
    - Ties break on symbol ascending, so the result is fully deterministic.
    - top_n <= 0 returns nothing.
    """
    if rank_by not in RANK_METRICS:
        raise ValueError(f"unknown rank_by {rank_by!r}; expected one of {RANK_METRICS}")
    if top_n <= 0:
        return []
    scored = [
        (symbol, m.get(rank_by))
        for symbol, m in metrics.items()
        if m.get(rank_by) is not None
    ]
    # sort by value desc, then symbol asc (negate value so both keys ascend)
    scored.sort(key=lambda sv: (-float(sv[1]), sv[0]))  # type: ignore[arg-type]
    return [(symbol, float(value)) for symbol, value in scored[:top_n]]  # type: ignore[arg-type]
