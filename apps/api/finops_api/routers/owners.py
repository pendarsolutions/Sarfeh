"""Owners and budgets: teams, which team owns a resource, and monthly budgets for the organization and each team."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..orm import Budget, InventoryResource, Server, SpendEntry, Team
from ..security import Principal, audit, current_principal, require
from ..services import get_inventory, inventory_view, snapshot
from ..spend import ORG_SCOPE, budget_status

router = APIRouter(prefix="/api/v1", tags=["owners"])


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def _team(db: Session, p: Principal, team_id: str) -> Team:
    t = db.get(Team, team_id)
    if t is None or t.organization_id != p.org.id:
        raise HTTPException(404, "team not found")
    return t


def check_team(db: Session, p: Principal, team_id: str | None) -> str | None:
    """A team id from a request: None, or a team of this organization."""
    if team_id is None:
        return None
    return _team(db, p, team_id).id


def _unique_name(db: Session, p: Principal, name: str, except_id: str | None = None) -> str:
    name = name.strip()
    if not name:
        raise HTTPException(422, "a team needs a name")
    clash = db.scalar(select(Team).where(Team.organization_id == p.org.id, Team.name == name, Team.id != (except_id or "")))
    if clash is not None:
        raise HTTPException(409, "a team with this name already exists")
    return name


@router.get("/teams")
def list_teams(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    rows = db.scalars(select(Team).where(Team.organization_id == p.org.id).order_by(Team.name))
    return [{"id": t.id, "name": t.name} for t in rows]


@router.post("/teams")
def create_team(body: TeamIn, db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    t = Team(organization_id=p.org.id, name=_unique_name(db, p, body.name))
    db.add(t)
    db.flush()
    audit(db, p, "team.create", "team", t.id, {"name": t.name})
    db.commit()
    return {"id": t.id, "name": t.name}


@router.patch("/teams/{team_id}")
def rename_team(team_id: str, body: TeamIn, db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    t = _team(db, p, team_id)
    old, t.name = t.name, _unique_name(db, p, body.name, t.id)
    audit(db, p, "team.rename", "team", t.id, {"from": old, "to": t.name})
    db.commit()
    return {"id": t.id, "name": t.name}


@router.delete("/teams/{team_id}")
def delete_team(team_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    """What the team owned becomes unallocated (nothing else changes), and its budget goes with it."""
    t = _team(db, p, team_id)
    for model in (InventoryResource, Server, SpendEntry):
        db.execute(update(model).where(model.team_id == t.id).values(team_id=None))
    for b in db.scalars(select(Budget).where(Budget.organization_id == p.org.id, Budget.scope == t.id)):
        db.delete(b)
    audit(db, p, "team.delete", "team", t.id, {"name": t.name})
    db.delete(t)
    db.commit()
    return {"id": team_id, "deleted": True}


class OwnerIn(BaseModel):
    team_id: str | None = None


@router.put("/inventories/{inventory_id}/resources/{resource_id}/team")
def set_resource_team(inventory_id: str, resource_id: str, body: OwnerIn, db: Session = Depends(get_db),
                      p: Principal = Depends(require("analyst"))):
    """Who owns a resource. Not a pricing input, so the inventory stays confirmed."""
    inv = get_inventory(db, p, inventory_id)
    row = db.get(InventoryResource, resource_id)
    if row is None or row.inventory_id != inv.id:
        raise HTTPException(404, "resource not found")
    row.team_id = check_team(db, p, body.team_id)
    audit(db, p, "inventory.resource.team", "inventory_resource", row.id, {"team_id": row.team_id})
    db.commit()
    return inventory_view(db, inv, snapshot(db).policy)


# --------------------------------------------------------------------------- budgets


class BudgetIn(BaseModel):
    #: None for the whole organization
    team_id: str | None = None
    #: None removes the budget
    monthly_toman: Decimal | None = Field(default=None, gt=0, le=Decimal("1e15"))


@router.get("/budgets")
def get_budgets(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return budget_status(db, p.org)


@router.put("/budgets")
def put_budget(body: BudgetIn, db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    scope = check_team(db, p, body.team_id) or ORG_SCOPE
    b = db.scalar(select(Budget).where(Budget.organization_id == p.org.id, Budget.scope == scope))
    if body.monthly_toman is None:
        if b is not None:
            db.delete(b)
    elif b is None:
        db.add(Budget(organization_id=p.org.id, scope=scope, monthly_toman=body.monthly_toman, alerted={}))
    else:
        b.monthly_toman = body.monthly_toman
        b.alerted = {}  # a new amount: its 80% and 100% alerts can go out again
    audit(db, p, "budget.set", "organization", p.org.id,
          {"scope": scope, "monthly_toman": str(body.monthly_toman) if body.monthly_toman is not None else None})
    db.commit()
    return budget_status(db, p.org)
