"""A public, read-only demo (FINOPS_DEMO_MODE=true), for an installation of its own.

Anyone who opens the dashboard gets a session without an account, in one shared organization that holds the sample
workspace: a confirmed setup, three months of payments, two teams and their budgets. Nothing can be changed. Every
request is put to `refuses`, which lets through reads and the few requests that only calculate (a quote, a
comparison, the advisor), and those keep nothing a visitor typed.
"""

from __future__ import annotations

import re
import secrets
import threading
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pricing_engine.catalog import load_json
from shared_types import ResourceDraft

from .config import get_settings
from .dates import current_month, shift_month
from .orm import Budget, CostSnapshot, Inventory, Membership, Organization, QuoteRow, SpendEntry, Team, User
from .security import Principal, create_user_with_org
from .services import confirm_inventory, create_inventory, latest_analysis, resources_of, run_recommendations, snapshot
from .spend import ORG_SCOPE, take_snapshot

#: the shared visitor. Nobody can sign in as it: its password is random and never kept, and sign-in is refused anyway.
VISITOR_EMAIL = "visitor@demo.invalid"
#: enough to open every page (costs and comparisons are worked out on request); the gate below is what stops changes
VISITOR_ROLE = "analyst"
ORG_NAME = "شرکت نمونه"
#: team -> the sample resources it owns. The staging app has no owner, so the demo shows unallocated spend too.
TEAMS = {"محصول": ("web-prod", "db-prod"), "زیرساخت": ("backups", "ci-runner")}
#: monthly budgets in Toman. With the sample payments, the organization and one team are close to theirs.
BUDGETS = {ORG_SCOPE: "26000000", "محصول": "16000000", "زیرساخت": "6000000"}

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: starting a visit, and the requests that calculate for a page without changing the workspace
CALCULATIONS = re.compile(r"^/api/v1/(auth/demo|inventories/[^/]+/(quote|compare)|advisor/chat)$")

_building = threading.Lock()


def refuses(method: str, path: str) -> bool:
    """Whether a demo turns this request away: anything that isn't a read or one of the calculations."""
    if method in SAFE_METHODS:
        return False
    return not (method == "POST" and CALCULATIONS.match(path))


def _seed() -> dict:
    return load_json(get_settings().seed_dir / "sample_inventory.json")


def sample_workspace(db: Session, p: Principal, lang: str) -> Inventory:
    """The sample setup as a draft, with a few months of sample payments so the spend page has a trend and a gap."""
    data = _seed()
    drafts = [ResourceDraft(**r) for r in data["resources"]]
    inv = create_inventory(db, p, data["name_fa"] if lang == "fa" else data["name"], "builder", drafts, {"demo": True})
    month = current_month()
    for s in data.get("sample_spend", []):
        db.add(SpendEntry(organization_id=p.org.id, month=shift_month(month, -int(s["months_ago"])),
                          provider_id=s["provider_id"], amount_toman=Decimal(s["amount_toman"]), source="sample",
                          status="confirmed", inventory_id=inv.id, created_by=p.user.id))
    db.commit()
    org_settings = dict(p.org.settings or {})
    if "engineer_day_rate_toman" not in org_settings:
        org_settings["engineer_day_rate_toman"] = data["organization_settings"]["engineer_day_rate_toman"]
        org_settings["engineer_day_rate_label"] = data["organization_settings"]["engineer_day_rate_label"]
        p.org.settings = org_settings
        db.commit()
    return inv


def _visitor(db: Session) -> tuple[User, Organization]:
    user = db.scalar(select(User).where(User.email == VISITOR_EMAIL))
    if user is None:
        user, org = create_user_with_org(db, VISITOR_EMAIL, secrets.token_urlsafe(32), "Visitor", ORG_NAME,
                                         role=VISITOR_ROLE)
        org.settings = {"default_language": "fa"}
        db.commit()
        return user, org
    m = db.scalar(select(Membership).where(Membership.user_id == user.id))
    return user, db.get(Organization, m.organization_id)


def _workspace(db: Session, p: Principal) -> Inventory:
    """The sample setup, confirmed, with this month's estimate; made the first time."""
    for inv in db.scalars(select(Inventory).where(Inventory.organization_id == p.org.id,
                                                  Inventory.status != "archived")):
        if (inv.source_meta or {}).get("demo"):
            break
    else:
        inv = sample_workspace(db, p, "fa")
    if inv.status != "confirmed":
        confirm_inventory(db, p, inv, True)
    month = current_month()
    if db.scalar(select(CostSnapshot.id).where(CostSnapshot.inventory_id == inv.id,
                                               CostSnapshot.month == month)) is None:
        take_snapshot(db, inv, month, "confirm", p.user.id)
        db.commit()
    return inv


def _owners_and_budgets(db: Session, p: Principal, inv: Inventory) -> None:
    if db.scalar(select(Team.id).where(Team.organization_id == p.org.id).limit(1)) is not None:
        return
    rows = {r.name: r for r in resources_of(db, inv)}
    scopes = {ORG_SCOPE: ORG_SCOPE}
    for name, owned in TEAMS.items():
        team = Team(organization_id=p.org.id, name=name)
        db.add(team)
        db.flush()
        scopes[name] = team.id
        for resource in owned:
            if resource in rows:
                rows[resource].team_id = team.id
    for scope, amount in BUDGETS.items():
        db.add(Budget(organization_id=p.org.id, scope=scopes[scope], monthly_toman=Decimal(amount), alerted={}))
    db.commit()


def _this_months_payments(db: Session, p: Principal, inv: Inventory) -> None:
    """A demo that has run into a new month gets that month's sample payments, so "this month" is never empty."""
    month = current_month()
    if db.scalar(select(SpendEntry.id).where(SpendEntry.organization_id == p.org.id, SpendEntry.source == "sample",
                                             SpendEntry.month == month).limit(1)) is not None:
        return
    for s in _seed().get("sample_spend", []):
        if int(s["months_ago"]) == 0:
            db.add(SpendEntry(organization_id=p.org.id, month=month, provider_id=s["provider_id"],
                              amount_toman=Decimal(s["amount_toman"]), source="sample", status="confirmed",
                              inventory_id=inv.id, created_by=p.user.id))
    db.commit()


def _analysis(db: Session, p: Principal, inv: Inventory) -> None:
    """Savings worked out at today's prices: visitors can't run the analysis themselves."""
    rows = latest_analysis(db, inv)
    quote = db.get(QuoteRow, rows[0].quote_id) if rows else None
    if quote is None or quote.catalog_version != snapshot(db).catalog_version:
        run_recommendations(db, p, inv)


def ensure_demo(db: Session) -> tuple[User, Organization]:
    """The visitor and its organization, with the sample workspace complete and current. Safe to call on every visit:
    each part is made only if it's missing, so a build that was cut short is finished by the next one."""
    with _building:
        try:
            user, org = _visitor(db)
        except IntegrityError:  # another process made the visitor at the same moment
            db.rollback()
            user, org = _visitor(db)
        builder = Principal(user=user, org=org, role="admin")
        inv = _workspace(db, builder)
        _owners_and_budgets(db, builder, inv)
        _this_months_payments(db, builder, inv)
        _analysis(db, builder, inv)
    return user, org
