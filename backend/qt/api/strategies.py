"""Strategy CRUD with config versioning: every save snapshots the full
config, and trades reference the snapshot that produced them."""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from qt.db import get_session
from qt.models import AuditLog, Strategy, StrategyConfigVersion, Trade
from qt.services.presets import PRESETS

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class EntryRules(BaseModel):
    min_day_gain_pct: float = Field(default=3.0, ge=0, le=100)
    max_day_gain_pct: float = Field(default=0, ge=0, le=1000)  # 0 = no ceiling; skip over-extended movers
    min_price: float = Field(default=0, ge=0)  # $/share floor for this strategy; 0 = any
    max_price: float = Field(default=0, ge=0)  # $/share cap; 0 = none (e.g. only movers under $10)
    require_above_vwap: bool = True
    entry_window_start: str | None = None  # "HH:MM" US/Eastern; None = any time
    entry_window_end: str | None = None
    # Advanced execution: how far THROUGH the market to price the marketable buy
    # limit (0.5% = default). Higher = fills more reliably, worse price; 0 = a
    # passive limit AT the quote (may not fill). Paper/live only — the backtest
    # uses its own spread-cost input.
    entry_slippage_pct: float = Field(default=0.5, ge=0, le=5)


class ExitRules(BaseModel):
    # Lower bounds allow 0 so a buy-and-hold DCA sleeve can turn every exit off.
    # A hard stop stays MANDATORY for normal strategies — enforced in
    # StrategyParams below, which can see whether this is a DCA sleeve.
    trailing_stop_pct: float = Field(default=5.0, ge=0, le=50)
    stop_loss_pct: float = Field(default=4.0, ge=0, le=50)
    take_profit_pct: float = Field(default=0, ge=0, le=500)  # 0 = disabled
    max_holding_hours: float = Field(default=0, ge=0, le=2400)  # 0 = disabled
    flatten_before_close: bool = False
    exit_below_vwap: bool = False
    # Advanced execution: how far BELOW the market to price the marketable sell
    # limit (1% = default). exit_slippage_max_pct >= exit_slippage_pct enables an
    # escalating chase — each time an exit misses the fill, the buffer widens by
    # one base step up to the max, so a fast drop still gets out (still a LIMIT,
    # never a naked market order). Paper/live only — the backtest assumes fills.
    exit_slippage_pct: float = Field(default=1.0, ge=0, le=10)
    exit_slippage_max_pct: float = Field(default=1.0, ge=0, le=20)

    @model_validator(mode="after")
    def _exit_slip(self) -> "ExitRules":
        if self.exit_slippage_max_pct < self.exit_slippage_pct:
            raise ValueError("Max exit slippage can't be less than the base exit slippage.")
        return self


class DCAConfig(BaseModel):
    """Dollar-cost-averaging sleeve config. A strategy carrying this with
    interval_days > 0 buys its fixed symbol list every N days as independent
    lots, regardless of momentum — the steady baseline the momentum strategies
    must beat. Absent (None) or interval_days <= 0 = not a DCA strategy."""

    interval_days: int = Field(default=0, ge=0, le=365)  # 0 = disabled


class ATRConfig(BaseModel):
    """ATR-based stops & position sizing. Two independent switches, off by
    default. period is the ATR lookback in daily bars. stop_mult > 0 enables the
    ATR stop (the hard stop becomes stop_mult × ATR% below entry, adapting to the
    symbol's volatility). risk_usd > 0 (which also needs stop_mult > 0) enables
    ATR sizing (each position sized so a stop-out loses ~risk_usd). Both zero =
    off, and the fixed stop_loss_pct / sizing_usd are unchanged.

    Declared as an explicit nested model — like DCAConfig — because pydantic drops
    keys it doesn't know about, so without this field the atr block would not
    survive a save."""

    period: int = Field(default=14, ge=2, le=100)
    stop_mult: float = Field(default=0, ge=0, le=20)  # 0 = off (use fixed stop_loss_pct)
    risk_usd: float = Field(default=0, ge=0, le=100_000)  # 0 = off (use fixed sizing_usd)


class StrategyParams(BaseModel):
    entry: EntryRules = EntryRules()
    exit: ExitRules = ExitRules()
    dca: DCAConfig | None = None  # present only for DCA sleeve strategies
    atr: ATRConfig | None = None  # present only when ATR stops/sizing are configured

    @model_validator(mode="after")
    def _stop_required_unless_dca(self) -> "StrategyParams":
        # A hard stop-loss is mandatory for every normal strategy. The lone
        # exception is a buy-and-hold DCA sleeve, which is allowed to run with
        # all exits off (the user may still add a stop if they want one).
        is_dca = self.dca is not None and self.dca.interval_days > 0
        if not is_dca and self.exit.stop_loss_pct <= 0:
            raise ValueError("A hard stop-loss is mandatory (stop_loss_pct must be > 0).")
        return self


class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    asset_class: str = Field(pattern="^(stock|crypto)$")
    universe: str = Field(default="scanner", pattern="^(scanner|watchlist|both|basket|custom)$")
    basket_id: int | None = None
    symbols: list[str] = []  # for universe="custom": the hand-picked symbol list
    rank_by: str = Field(
        default="momentum_today",
        pattern="^(momentum_today|return_30d|relative_strength|rs_vs_spy)$",
    )
    top_n: int = Field(default=10, ge=1, le=50)
    preset: str = "custom"
    params: StrategyParams = StrategyParams()
    sizing_usd: float = Field(default=200, ge=10, le=100_000)
    sleeve_usd: float = Field(default=1000, ge=10, le=1_000_000)
    max_positions: int = Field(default=3, ge=1, le=25)
    swing_mode: bool = True
    ignore_regime: bool = False

    @model_validator(mode="after")
    def _sanity(self) -> "StrategyBody":
        if self.sizing_usd > self.sleeve_usd:
            raise ValueError("Per-trade size cannot exceed the strategy's sleeve budget.")
        if self.universe == "basket" and self.basket_id is None:
            raise ValueError("A basket universe needs a basket selected.")
        if self.universe == "custom" and not [s for s in self.symbols if s.strip()]:
            raise ValueError("A custom universe needs at least one symbol.")
        # rs_vs_spy ranks members by out-performance of SPY — a stock benchmark,
        # meaningless for a crypto basket. Block the combination rather than
        # silently ranking nothing.
        if self.rank_by == "rs_vs_spy" and self.asset_class != "stock":
            raise ValueError("Relative strength vs S&P 500 is a stock-only ranking.")
        return self

    def clean_symbols(self) -> list[str]:
        """De-duped, upper-cased, non-empty symbols (kept only for custom)."""
        if self.universe != "custom":
            return []
        return sorted({s.strip().upper() for s in self.symbols if s.strip()})


def _snapshot(session: Session, strategy: Strategy) -> StrategyConfigVersion:
    latest = (
        session.query(func.max(StrategyConfigVersion.version_no))
        .filter(StrategyConfigVersion.strategy_id == strategy.id)
        .scalar()
        or 0
    )
    version = StrategyConfigVersion(
        strategy_id=strategy.id,
        version_no=latest + 1,
        snapshot=json.dumps(_serialize(strategy)),
    )
    session.add(version)
    session.flush()
    return version


def _serialize(s: Strategy) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "enabled": s.enabled,
        "asset_class": s.asset_class,
        "universe": s.universe,
        "basket_id": s.basket_id,
        "symbols": json.loads(s.symbols) if s.symbols else [],
        "rank_by": s.rank_by,
        "top_n": s.top_n,
        "preset": s.preset,
        "params": json.loads(s.params),
        "sizing_usd": s.sizing_usd,
        "sleeve_usd": s.sleeve_usd,
        "max_positions": s.max_positions,
        "swing_mode": s.swing_mode,
        "ignore_regime": s.ignore_regime,
    }


@router.get("/presets")
def presets() -> dict:
    return PRESETS


@router.get("")
def list_strategies(session: Session = Depends(get_session)) -> list[dict]:
    out = []
    for s in session.query(Strategy).order_by(Strategy.id).all():
        row = _serialize(s)
        row["open_trades"] = (
            session.query(func.count(Trade.id))
            .filter(Trade.strategy_id == s.id, Trade.status == "open")
            .scalar()
        )
        row["version"] = (
            session.query(func.max(StrategyConfigVersion.version_no))
            .filter(StrategyConfigVersion.strategy_id == s.id)
            .scalar()
            or 0
        )
        out.append(row)
    return out


def _validate_basket(session: Session, body: StrategyBody) -> None:
    if body.universe == "basket":
        from qt.models import Basket

        if not session.get(Basket, body.basket_id):
            raise HTTPException(status_code=422, detail="Selected basket does not exist.")


@router.post("")
def create_strategy(body: StrategyBody, session: Session = Depends(get_session)) -> dict:
    _validate_basket(session, body)
    strategy = Strategy(
        name=body.name,
        enabled=False,  # always born disabled; enabling is a deliberate act
        asset_class=body.asset_class,
        universe=body.universe,
        basket_id=body.basket_id if body.universe == "basket" else None,
        symbols=json.dumps(body.clean_symbols()),
        rank_by=body.rank_by,
        top_n=body.top_n,
        preset=body.preset,
        params=body.params.model_dump_json(),
        sizing_usd=body.sizing_usd,
        sleeve_usd=body.sleeve_usd,
        max_positions=body.max_positions,
        swing_mode=body.swing_mode,
        ignore_regime=body.ignore_regime,
    )
    session.add(strategy)
    session.flush()
    _snapshot(session, strategy)
    session.add(AuditLog(category="strategy", message=f"Created strategy '{body.name}'"))
    return _serialize(strategy)


@router.put("/{strategy_id}")
def update_strategy(
    strategy_id: int, body: StrategyBody, session: Session = Depends(get_session)
) -> dict:
    strategy = session.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    _validate_basket(session, body)
    strategy.name = body.name
    strategy.asset_class = body.asset_class
    strategy.universe = body.universe
    strategy.basket_id = body.basket_id if body.universe == "basket" else None
    strategy.symbols = json.dumps(body.clean_symbols())
    strategy.rank_by = body.rank_by
    strategy.top_n = body.top_n
    strategy.preset = body.preset
    strategy.params = body.params.model_dump_json()
    strategy.sizing_usd = body.sizing_usd
    strategy.sleeve_usd = body.sleeve_usd
    strategy.max_positions = body.max_positions
    strategy.swing_mode = body.swing_mode
    strategy.ignore_regime = body.ignore_regime
    _snapshot(session, strategy)
    session.add(AuditLog(category="strategy", message=f"Updated strategy '{body.name}' (new config version)"))
    return _serialize(strategy)


@router.post("/{strategy_id}/toggle")
def toggle_strategy(strategy_id: int, session: Session = Depends(get_session)) -> dict:
    strategy = session.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    strategy.enabled = not strategy.enabled
    state = "ENABLED" if strategy.enabled else "paused"
    session.add(AuditLog(category="strategy", message=f"Strategy '{strategy.name}' {state}"))
    return _serialize(strategy)


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: int, session: Session = Depends(get_session)) -> dict:
    strategy = session.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    any_trades = (
        session.query(func.count(Trade.id)).filter(Trade.strategy_id == strategy_id).scalar()
    )
    if any_trades:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Strategy has {any_trades} trade(s) in the journal — history is never deleted. "
                "Pause the strategy instead."
            ),
        )
    session.query(StrategyConfigVersion).filter(
        StrategyConfigVersion.strategy_id == strategy_id
    ).delete()
    session.add(AuditLog(category="strategy", message=f"Deleted strategy '{strategy.name}' (no trades)"))
    session.delete(strategy)
    return {"ok": True}
