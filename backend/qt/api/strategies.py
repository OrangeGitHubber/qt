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
    require_above_vwap: bool = True
    entry_window_start: str | None = None  # "HH:MM" US/Eastern; None = any time
    entry_window_end: str | None = None


class ExitRules(BaseModel):
    trailing_stop_pct: float = Field(default=5.0, ge=0.5, le=50)
    stop_loss_pct: float = Field(default=4.0, gt=0, le=50)  # a hard stop is mandatory
    take_profit_pct: float = Field(default=0, ge=0, le=500)  # 0 = disabled
    max_holding_hours: float = Field(default=0, ge=0, le=2400)  # 0 = disabled
    flatten_before_close: bool = False
    exit_below_vwap: bool = False


class StrategyParams(BaseModel):
    entry: EntryRules = EntryRules()
    exit: ExitRules = ExitRules()


class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    asset_class: str = Field(pattern="^(stock|crypto)$")
    universe: str = Field(default="scanner", pattern="^(scanner|watchlist|both|basket)$")
    basket_id: int | None = None
    rank_by: str = Field(default="momentum_today", pattern="^(momentum_today|return_30d|relative_strength)$")
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
        return self


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
