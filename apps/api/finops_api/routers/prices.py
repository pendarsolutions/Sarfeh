"""Sarfeh's price service in the dashboard: where prices come from, whether new ones are out, and updating to them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from provider_adapters import FeedError

from ..config import get_settings
from ..db import get_db
from ..price_service import PriceServiceError, check, status, update
from ..security import Principal, audit, current_principal, require

router = APIRouter(prefix="/api/v1", tags=["prices"])


def _uses_service() -> None:
    if not get_settings().uses_price_service:
        raise HTTPException(409, "this installation doesn't take its prices from Sarfeh's price service")


@router.get("/prices/service")
def price_service_status(db: Session = Depends(get_db), _: Principal = Depends(current_principal)):
    return status(db)


@router.post("/admin/prices/check")
def check_now(db: Session = Depends(get_db), _: Principal = Depends(require("admin"))):
    _uses_service()
    return check(db)


@router.post("/admin/prices/update")
def update_now(db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    _uses_service()
    try:
        out = update(db, by=p.user.email)
    except PriceServiceError as exc:
        raise HTTPException(502, f"the price service can't be reached: {exc}") from exc
    except FeedError as exc:
        raise HTTPException(502, f"the catalog was refused: {exc}") from exc
    audit(db, p, "prices.update", "price_catalog", out["generated_at"], {"changed": out["changed"]})
    db.commit()
    return {**out, "status": status(db)}
