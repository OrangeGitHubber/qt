"""Baskets CRUD: curated symbol groups the user builds strategies and
backtests from. Baskets are curated lists, NOT a sector database — the UI says
so. Mutations are audit-logged like everything else that changes state."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from qt.db import get_session
from qt.models import Asset, AuditLog, Basket, BasketItem, Strategy
from qt.services.starter_baskets import annotate_membership

router = APIRouter(prefix="/api/baskets", tags=["baskets"])


class BasketCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class BasketRename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ItemBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    asset_class: str = Field(pattern="^(stock|crypto)$")


def _serialize(session: Session, basket: Basket) -> dict:
    items = (
        session.query(BasketItem)
        .filter(BasketItem.basket_id == basket.id)
        .order_by(BasketItem.symbol)
        .all()
    )
    members = annotate_membership(session, items)
    return {
        "id": basket.id,
        "name": basket.name,
        "builtin": basket.builtin,
        "created_at": basket.created_at.isoformat() if basket.created_at else None,
        "count": len(members),
        "symbols": members,
    }


def _get_or_404(session: Session, basket_id: int) -> Basket:
    basket = session.get(Basket, basket_id)
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found.")
    return basket


@router.get("")
def list_baskets(session: Session = Depends(get_session)) -> list[dict]:
    return [
        _serialize(session, b)
        for b in session.query(Basket).order_by(Basket.builtin.desc(), Basket.name).all()
    ]


@router.post("")
def create_basket(body: BasketCreate, session: Session = Depends(get_session)) -> dict:
    name = body.name.strip()
    if session.query(Basket).filter(func.lower(Basket.name) == name.lower()).first():
        raise HTTPException(status_code=409, detail=f"A basket named '{name}' already exists.")
    basket = Basket(name=name, builtin=False)
    session.add(basket)
    session.flush()
    session.add(AuditLog(category="basket", message=f"Created basket '{name}'"))
    return _serialize(session, basket)


@router.put("/{basket_id}")
def rename_basket(basket_id: int, body: BasketRename, session: Session = Depends(get_session)) -> dict:
    basket = _get_or_404(session, basket_id)
    name = body.name.strip()
    clash = (
        session.query(Basket)
        .filter(func.lower(Basket.name) == name.lower(), Basket.id != basket_id)
        .first()
    )
    if clash:
        raise HTTPException(status_code=409, detail=f"A basket named '{name}' already exists.")
    old = basket.name
    basket.name = name
    session.add(AuditLog(category="basket", message=f"Renamed basket '{old}' → '{name}'"))
    return _serialize(session, basket)


@router.delete("/{basket_id}")
def delete_basket(basket_id: int, session: Session = Depends(get_session)) -> dict:
    basket = _get_or_404(session, basket_id)
    using = (
        session.query(func.count(Strategy.id))
        .filter(Strategy.basket_id == basket_id)
        .scalar()
    )
    if using:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{using} strategy(ies) use this basket — point them elsewhere first, "
                "then delete it."
            ),
        )
    session.query(BasketItem).filter(BasketItem.basket_id == basket_id).delete()
    session.add(AuditLog(category="basket", message=f"Deleted basket '{basket.name}'"))
    session.delete(basket)
    return {"ok": True}


@router.post("/{basket_id}/items")
def add_item(basket_id: int, body: ItemBody, session: Session = Depends(get_session)) -> dict:
    basket = _get_or_404(session, basket_id)
    symbol = body.symbol.strip().upper()
    # Validate against the directory ONLY when it's populated — a fresh
    # container has an empty directory and must still accept edits.
    directory_has_any = session.query(Asset.symbol).first() is not None
    if directory_has_any:
        known = (
            session.query(Asset.symbol)
            .filter(Asset.symbol == symbol, Asset.asset_class == body.asset_class)
            .first()
        )
        if not known:
            raise HTTPException(
                status_code=422,
                detail=f"{symbol} is not in Alpaca's tradable {body.asset_class} list.",
            )
    exists = session.get(BasketItem, {"basket_id": basket_id, "symbol": symbol, "asset_class": body.asset_class})
    if exists:
        return _serialize(session, basket)
    session.add(BasketItem(basket_id=basket_id, symbol=symbol, asset_class=body.asset_class))
    session.add(AuditLog(category="basket", message=f"Added {symbol} to basket '{basket.name}'"))
    return _serialize(session, basket)


@router.delete("/{basket_id}/items/{asset_class}/{symbol}")
def remove_item(
    basket_id: int, asset_class: str, symbol: str, session: Session = Depends(get_session)
) -> dict:
    basket = _get_or_404(session, basket_id)
    sym = symbol.strip().upper()
    item = session.get(
        BasketItem, {"basket_id": basket_id, "symbol": sym, "asset_class": asset_class}
    )
    if not item:
        raise HTTPException(status_code=404, detail=f"{sym} is not in this basket.")
    session.delete(item)
    session.add(AuditLog(category="basket", message=f"Removed {sym} from basket '{basket.name}'"))
    return _serialize(session, basket)
