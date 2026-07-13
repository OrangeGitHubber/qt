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
                "require_above_vwap": True,
                "entry_window_start": "10:00",
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
                "entry_window_start": "10:00",
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
