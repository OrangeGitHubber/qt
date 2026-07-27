"""Strategy presets: sane, conservative starting points a novice can pick
from a dropdown instead of tuning raw numbers. Values are hypotheses to be
tested in shadow/paper — not promises."""

PRESETS: dict[str, dict] = {
    "momentum_swing_stocks": {
        "label": "Momentum — stocks, swing (recommended for stocks)",
        "description": (
            "Buys strong risers in a healthy market and holds for days, letting a "
            "trailing stop follow the trend. Avoids intraday churn where spreads "
            "and thin free data hurt most."
        ),
        "asset_class": "stock",
        "universe": "scanner",
        "swing_mode": True,
        "params": {
            "entry": {
                "min_day_gain_pct": 3.0,
                "max_day_gain_pct": 10.0,  # skip blow-off moves already too far to chase
                "require_above_vwap": True,
                "entry_window_start": "09:30",
                "entry_window_end": "15:30",
            },
            "exit": {
                "trailing_stop_pct": 5.0,
                "stop_loss_pct": 4.0,
                "take_profit_pct": 12.0,
                "max_holding_hours": 120,
                "flatten_before_close": False,
                "exit_below_vwap": False,
            },
        },
    },
    "momentum_intraday_crypto": {
        "label": "Momentum — crypto, intraday",
        "description": (
            "Rides 24/7 crypto momentum with tighter stops and shorter holds. "
            "Crypto is the intraday lab: no market close, cleaner data, but more "
            "volatile — sizes should stay small."
        ),
        "asset_class": "crypto",
        "universe": "scanner",
        "swing_mode": False,
        "params": {
            "entry": {
                "min_day_gain_pct": 2.0,
                "require_above_vwap": True,
                "entry_window_start": None,
                "entry_window_end": None,
            },
            "exit": {
                "trailing_stop_pct": 3.0,
                "stop_loss_pct": 2.5,
                "take_profit_pct": 6.0,
                "max_holding_hours": 24,
                "flatten_before_close": False,
                "exit_below_vwap": True,
            },
        },
    },
    "sector_rotation": {
        "label": "Sector rotation — relative-strength leaders (basket)",
        "description": (
            "Holds only the few strongest names in a basket and rotates: buys the "
            "top-N ranked by relative strength (price vs its 200-day average) and "
            "sells one the instant it drops out of the top-N. Low turnover, and it "
            "reads daily bars so the thin free intraday feed doesn't matter. After "
            "picking this, choose your Sector-ETFs basket in the editor."
        ),
        "asset_class": "stock",
        "universe": "basket",
        "rank_by": "relative_strength",
        "top_n": 3,
        "swing_mode": True,
        "params": {
            "entry": {
                # Rank-based: any current top-N member is eligible — the ranking,
                # not a daily-gain threshold, is the signal.
                "min_day_gain_pct": 0.0,
                "require_above_vwap": False,
                "entry_window_start": None,
                "entry_window_end": None,
            },
            "exit": {
                "rotate_on_rank_dropout": True,  # THE rotation rule
                "stop_loss_pct": 15.0,           # wide safety net; rotation does the work
                "trailing_stop_pct": 0.0,
                "take_profit_pct": 0.0,
                "max_holding_hours": 0,          # 0 = no time cap (low turnover)
                "flatten_before_close": False,
                "exit_below_vwap": False,
            },
        },
    },
    "watchlist_swing": {
        "label": "Watchlist only — swing",
        "description": (
            "Trades only symbols you pinned yourself, entering on strength and "
            "holding for days. Most predictable behavior; good first strategy to "
            "watch in shadow mode."
        ),
        "asset_class": "stock",
        "universe": "watchlist",
        "swing_mode": True,
        "params": {
            "entry": {
                "min_day_gain_pct": 1.5,
                "require_above_vwap": True,
                "entry_window_start": "09:30",
                "entry_window_end": "15:30",
            },
            "exit": {
                "trailing_stop_pct": 6.0,
                "stop_loss_pct": 5.0,
                "take_profit_pct": 15.0,
                "max_holding_hours": 240,
                "flatten_before_close": False,
                "exit_below_vwap": False,
            },
        },
    },
}
