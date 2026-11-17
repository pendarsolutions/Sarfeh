"""The spend ledger (what was paid each month), the monthly cost snapshot (what the confirmed setup should cost), and
how the two compare.

Months are Solar Hijri ("1405-08"), see dates.py. Totals only count confirmed entries: an entry read from an invoice
waits for someone to check its amount and month first.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoice_parser import InvoiceExtraction
from pricing_engine import BillingContext, price_inventory
from pricing_engine.models import Quote, Resource

from .dates import (
    current_month,
    fa_number,
    month_days,
    month_key,
    month_label,
    month_of,
    shift_month,
    to_jalali,
    today_in_iran,
)
from .orm import (
    AuditLog,
    Budget,
    CostSnapshot,
    Inventory,
    InventoryResource,
    Organization,
    QuoteRow,
    SpendEntry,
    Team,
    Upload,
)
from .services import has_confirmed_version, last_confirmed_resources, snapshot

RIAL_PER_TOMAN = Decimal(10)
#: paid and estimated further apart than this (in percent of the estimate) is worth a look
GAP_PERCENT = Decimal(10)
#: a provider's or team's month is "unusual" when it moves by more than this from the month before (org setting
#: unusual_change_percent), and by at least this much money (unusual_change_min_toman)
UNUSUAL_PERCENT = Decimal(30)
UNUSUAL_MIN_TOMAN = Decimal(1_000_000)
#: last month's unusual changes are checked from this day of the month on, once its invoices have usually arrived
UNUSUAL_CHECK_DAY = 5
ORG_SCOPE = "organization"


# --------------------------------------------------------------------------- entries


def provider_key(entry: SpendEntry) -> str:
    """One key per payee: the catalog provider, or the vendor's name for one the catalog doesn't have."""
    return entry.provider_id or f"vendor:{(entry.vendor or '').strip().lower()}"


def entry_view(e: SpendEntry, db: Session) -> dict[str, Any]:
    upload = None
    if e.upload_id:
        from .platform import sign_download

        up = db.get(Upload, e.upload_id)
        if up is not None:
            upload = {"id": up.id, "filename": up.filename, "deleted": up.deleted_at is not None,
                      "download_url": None if up.deleted_at else sign_download(up.id)}
    return {"id": e.id, "month": e.month, "provider_id": e.provider_id, "vendor": e.vendor,
            "provider_key": provider_key(e), "amount_toman": e.amount_toman, "source": e.source, "status": e.status,
            "paid_on": e.paid_on, "reference": e.reference, "note": e.note, "inventory_id": e.inventory_id,
            "team_id": e.team_id,
            "upload": upload, "evidence": e.evidence or {}, "created_at": e.created_at, "updated_at": e.updated_at}


def invoice_month(ext: InvoiceExtraction, now: datetime | None = None) -> str:
    """The month an invoice is for: the month of its date, or this month when it shows none."""
    d = ext.issued
    if d is not None:
        try:
            if d.calendar == "jalali":
                key = month_key(d.year, d.month)
                month_days(key)  # rejects a year outside the calendar's range
                return key
            return month_of(date(d.year, d.month, d.day))
        except ValueError:
            pass
    return current_month(now)


def entry_from_invoice(db: Session, org_id: str, user_id: str | None, ext: InvoiceExtraction, upload: Upload,
                       inventory_id: str | None = None) -> SpendEntry:
    """A spend entry waiting for review, from an uploaded invoice. The same file uploaded again gives the same entry."""
    same_file = select(Upload.id).where(Upload.organization_id == org_id, Upload.sha256 == upload.sha256)
    existing = db.scalar(select(SpendEntry).where(SpendEntry.organization_id == org_id,
                                                  SpendEntry.upload_id.in_(same_file)))
    if existing is not None:
        return existing
    amount = None
    if ext.total is not None and ext.detected_currency == "TOMAN":
        amount = ext.total
    elif ext.total is not None and ext.detected_currency == "RIAL":
        amount = ext.total / RIAL_PER_TOMAN
    meta = ext.meta()
    # the provider the invoice names, or, if it names none, the one all its plans belong to
    line_providers = {line.provider_id for line in ext.lines if line.provider_id}
    provider_id = ext.provider_id or (line_providers.pop() if len(line_providers) == 1 else None)
    entry = SpendEntry(
        organization_id=org_id, month=invoice_month(ext), provider_id=provider_id, amount_toman=amount,
        source="invoice", status="needs_review", upload_id=upload.id, inventory_id=inventory_id,
        created_by=user_id,
        evidence={k: meta[k] for k in ("detected_currency", "currency_evidence", "total", "total_evidence", "issued",
                                       "provider_id", "redaction_counts")}
        | {"filename": upload.filename, "lines": meta["lines"][:50]},
    )
    db.add(entry)
    db.flush()
    return entry


# --------------------------------------------------------------------------- snapshots


def _provider_totals(quote: Quote) -> list[dict[str, Any]]:
    tax: dict[str, Decimal] = defaultdict(Decimal)
    for bd in quote.breakdowns:
        if bd.monthly_tax_toman is not None:
            tax[bd.provider_id or "unknown"] += bd.monthly_tax_toman
    return [{"provider_id": pt.provider_id, "complete": pt.complete,
             "monthly_toman": str(pt.monthly_total_toman if pt.monthly_total_toman is not None
                                  else pt.priced_subtotal_toman),
             "tax_toman": str(tax[pt.provider_id])} for pt in quote.provider_totals]


def _resource_totals(quote: Quote) -> list[dict[str, Any]]:
    return [{"resource_id": bd.resource_id, "provider_id": bd.provider_id or "unknown",
             "monthly_toman": str((bd.monthly_total_toman if bd.monthly_total_toman is not None
                                   else bd.priced_subtotal_toman) + (bd.monthly_tax_toman or Decimal(0)))}
            for bd in quote.breakdowns]


def take_snapshot(db: Session, inv: Inventory, month: str, trigger: str, user_id: str | None = None,
                  resources: list[Resource] | None = None) -> CostSnapshot | None:
    """Price the inventory's confirmed setup at today's prices and keep it as `month`'s estimate.

    Priced with tax where the provider states a VAT rule, since that is what its invoices charge. Replaces an earlier
    snapshot of the same month. None when the inventory has never been confirmed.
    """
    resources = resources if resources is not None else last_confirmed_resources(db, inv)
    if not resources:
        return None
    snap = snapshot(db)
    quote = price_inventory(resources, snap, BillingContext(include_tax=True))
    qrow = QuoteRow(inventory_id=inv.id, organization_id=inv.organization_id,
                    inventory_snapshot=[r.model_dump(mode="json") for r in resources],
                    catalog_version=quote.catalog_version, policy_version=quote.policy_version,
                    deterministic_engine_version=quote.engine_version, catalog_snapshot_at=quote.catalog_snapshot_at,
                    complete=quote.complete, include_tax=True, total_toman=quote.total_toman,
                    priced_subtotal_toman=quote.priced_subtotal_toman, total_tax_toman=quote.total_tax_toman,
                    content=quote.model_dump(mode="json"), content_hash=quote.content_hash, created_by=user_id)
    db.add(qrow)
    db.flush()
    old = db.scalar(select(CostSnapshot).where(CostSnapshot.inventory_id == inv.id, CostSnapshot.month == month))
    if old is not None:
        db.delete(old)
        db.flush()
    total = None if quote.total_toman is None else quote.total_toman + (quote.total_tax_toman or Decimal(0))
    row = CostSnapshot(organization_id=inv.organization_id, inventory_id=inv.id, month=month, quote_id=qrow.id,
                       complete=quote.complete, total_toman=total, priced_subtotal_toman=quote.priced_subtotal_toman,
                       provider_totals=_provider_totals(quote), resource_totals=_resource_totals(quote),
                       trigger=trigger, created_by=user_id)
    db.add(row)
    db.add(AuditLog(organization_id=inv.organization_id, user_id=user_id, action="spend.snapshot",
                    entity_type="inventory", entity_id=inv.id,
                    detail={"month": month, "quote_id": qrow.id, "trigger": trigger, "replaced": old is not None}))
    db.flush()
    return row


def take_monthly_snapshots(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Make sure every inventory with a confirmed version has this month's snapshot. Run hourly; does nothing for an
    inventory that already has one, so the 1st's snapshot is kept for the rest of the month."""
    month = current_month(now)
    have = set(db.scalars(select(CostSnapshot.inventory_id).where(CostSnapshot.month == month)))
    taken = []
    for inv in db.scalars(select(Inventory).where(Inventory.status != "archived")).all():
        if inv.id in have:
            continue
        if take_snapshot(db, inv, month, "schedule") is not None:
            taken.append(inv.id)
    db.commit()
    return {"month": month, "taken": len(taken)}


def snapshot_view(s: CostSnapshot) -> dict[str, Any]:
    return {"id": s.id, "inventory_id": s.inventory_id, "month": s.month, "quote_id": s.quote_id,
            "complete": s.complete, "total_toman": s.total_toman, "priced_subtotal_toman": s.priced_subtotal_toman,
            "provider_totals": s.provider_totals, "trigger": s.trigger, "created_at": s.created_at}


# --------------------------------------------------------------------------- comparison


def _gap(paid: Decimal | None, estimated: Decimal | None, month_has_payments: bool) -> dict[str, Any] | None:
    """How a month's (or a provider's) payments differ from the estimate, when that is worth a look."""
    if estimated is None or not month_has_payments:
        return None
    paid = paid or Decimal(0)
    if estimated == 0:
        return {"kind": "not_in_setup", "diff_toman": paid, "percent": None} if paid > 0 else None
    if paid == 0:
        return {"kind": "not_paid", "diff_toman": -estimated, "percent": Decimal(-100)}
    diff = paid - estimated
    percent = (diff * 100 / estimated).quantize(Decimal("0.1"))
    if abs(percent) <= GAP_PERCENT:
        return None
    return {"kind": "paid_more" if diff > 0 else "paid_less", "diff_toman": diff, "percent": percent}


def _change(cur: Decimal | None, prev: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    if cur is None or prev is None:
        return None, None
    diff = cur - prev
    return diff, ((diff * 100 / prev).quantize(Decimal("0.1")) if prev else None)


def estimate_inventory(db: Session, org: Organization) -> Inventory | None:
    """The setup the organization's monthly estimates come from: the one chosen in Settings, else the one confirmed
    last, preferring a real one to the sample. Several inventories are often versions of the same setup (each invoice
    upload makes one), so their estimates are never added together."""
    rows = [i for i in db.scalars(select(Inventory).where(Inventory.organization_id == org.id,
                                                          Inventory.status != "archived"))
            if has_confirmed_version(db, i)]
    chosen = (org.settings or {}).get("estimate_inventory_id")
    for inv in rows:
        if inv.id == chosen:
            return inv
    rows.sort(key=lambda i: (not (i.source_meta or {}).get("demo"), i.confirmed_at or i.created_at), reverse=True)
    return rows[0] if rows else None


def _team_of(db: Session, inventory: Inventory | None) -> dict[str, str | None]:
    if inventory is None:
        return {}
    return {r.id: r.team_id for r in db.scalars(select(InventoryResource)
                                                .where(InventoryResource.inventory_id == inventory.id))}


def team_breakdown(entries: list[SpendEntry], snap: CostSnapshot | None, team_of: dict[str, str | None],
                   teams: dict[str, str]) -> list[dict[str, Any]]:
    """Who a month's payments belong to (showback).

    A payment with a team is that team's. A payment without one is shared among the teams by their part of that
    provider's estimate this month (each resource counts for the team it has now); what can't be shared that way (a
    provider with no estimate, or resources without a team) stays unallocated, as its own line. Shares are whole
    Toman, rounded down, and the remainder stays unallocated, so the lines add up to exactly what was paid.
    """
    direct: dict[str | None, Decimal] = defaultdict(Decimal)
    shared: dict[str | None, Decimal] = defaultdict(Decimal)
    estimated: dict[str | None, Decimal] = defaultdict(Decimal)
    est_by_provider: dict[str, dict[str | None, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for r in (snap.resource_totals or []) if snap is not None else []:
        team = team_of.get(r["resource_id"])
        team = team if team in teams else None
        amount = Decimal(r["monthly_toman"])
        est_by_provider[r["provider_id"]][team] += amount
        estimated[team] += amount
    unassigned: dict[str, Decimal] = defaultdict(Decimal)
    for e in entries:
        if e.team_id in teams:
            direct[e.team_id] += e.amount_toman
        else:
            unassigned[provider_key(e)] += e.amount_toman
    for key, amount in unassigned.items():
        shares = est_by_provider.get(key, {})
        whole = sum(shares.values(), Decimal(0))
        given = Decimal(0)
        if whole > 0:
            for team, part in shares.items():
                if team is None:
                    continue
                cut = (amount * part / whole).quantize(Decimal(1), rounding=ROUND_DOWN)
                shared[team] += cut
                given += cut
        direct[None] += amount - given
    rows = []
    for team in sorted(set(direct) | set(shared) | set(estimated) | set(teams), key=lambda t: (t is None, teams.get(t, ""))):
        paid = direct.get(team, Decimal(0)) + shared.get(team, Decimal(0))
        rows.append({"team_id": team, "name": teams.get(team) if team else None, "direct_toman": direct.get(team, Decimal(0)),
                     "shared_toman": shared.get(team, Decimal(0)), "paid_toman": paid,
                     "estimated_toman": estimated.get(team) if snap is not None else None})
    return rows


def _movers(cur: dict[str, Decimal], prev: dict[str, Decimal], percent: Decimal, minimum: Decimal) -> list[dict[str, Any]]:
    """Keys whose month moved by more than `percent` and at least `minimum` from the month before."""
    out = []
    for key in sorted(set(cur) | set(prev)):
        a, b = prev.get(key, Decimal(0)), cur.get(key, Decimal(0))
        if a <= 0:
            continue
        diff = b - a
        pct = (diff * 100 / a).quantize(Decimal("0.1"))
        if abs(pct) > percent and abs(diff) >= minimum:
            out.append({"key": key, "previous_toman": a, "current_toman": b, "change_toman": diff, "change_percent": pct})
    return out


def _thresholds(org: Organization | None) -> tuple[Decimal, Decimal]:
    st = (org.settings or {}) if org is not None else {}
    return (Decimal(str(st.get("unusual_change_percent", UNUSUAL_PERCENT))),
            Decimal(str(st.get("unusual_change_min_toman", UNUSUAL_MIN_TOMAN))))


def summary(db: Session, org_id: str, inventory: Inventory | None = None, months: int = 12,
            now: datetime | None = None, use_default_inventory: bool = True) -> dict[str, Any]:
    """The last `months` months, oldest first: paid per provider and per team, the change from the month before, the
    estimate (from `inventory`, by default the organization's estimate inventory) and the gaps between them."""
    org = db.get(Organization, org_id)
    if inventory is None and use_default_inventory and org is not None:
        inventory = estimate_inventory(db, org)
    last = current_month(now)
    keys = [shift_month(last, -i) for i in range(months, -1, -1)]  # one extra month before, for the first change
    entries = db.scalars(select(SpendEntry).where(SpendEntry.organization_id == org_id,
                                                  SpendEntry.month.in_(keys))).all()
    teams = {t.id: t.name for t in db.scalars(select(Team).where(Team.organization_id == org_id))}
    team_of = _team_of(db, inventory)
    percent, minimum = _thresholds(org)
    counted: dict[str, list[SpendEntry]] = defaultdict(list)
    paid: dict[str, dict[str, Decimal]] = {k: defaultdict(Decimal) for k in keys}
    labels: dict[str, dict[str, Any]] = {}
    for e in entries:
        if e.status != "confirmed" or e.amount_toman is None:
            continue
        key = provider_key(e)
        paid[e.month][key] += e.amount_toman
        counted[e.month].append(e)
        labels.setdefault(key, {"provider_id": e.provider_id, "vendor": e.vendor})
    snaps: dict[str, CostSnapshot] = {}
    if inventory is not None:
        snaps = {s.month: s for s in db.scalars(select(CostSnapshot).where(CostSnapshot.inventory_id == inventory.id,
                                                                           CostSnapshot.month.in_(keys)))}
    by_team = {k: team_breakdown(counted[k], snaps.get(k), team_of, teams) for k in keys}
    out = []
    prev_total: Decimal | None = None
    for i, key in enumerate(keys):
        has_paid = bool(counted[key])
        total = sum(paid[key].values(), Decimal(0)) if has_paid else None
        if i == 0:
            prev_total = total
            continue
        s = snaps.get(key)
        estimated: dict[str, Decimal] = {}
        if s is not None:
            for pt in s.provider_totals:
                estimated[pt["provider_id"]] = Decimal(pt["monthly_toman"]) + Decimal(pt.get("tax_toman") or 0)
                labels.setdefault(pt["provider_id"], {"provider_id": pt["provider_id"], "vendor": None})
        by_provider = []
        for pk in sorted(set(paid[key]) | set(estimated)):
            p_amount = paid[key].get(pk) if has_paid else None
            e_amount = estimated.get(pk, Decimal(0)) if s is not None else None
            by_provider.append({"key": pk, **labels[pk], "paid_toman": p_amount, "estimated_toman": e_amount,
                                "gap": _gap(p_amount, e_amount, has_paid)})
        teams_now = [t | {"gap": _gap(t["paid_toman"] if has_paid else None, t["estimated_toman"], has_paid)}
                     for t in by_team[key] if t["paid_toman"] or t["estimated_toman"] or t["team_id"] is not None]
        est_total = s.total_toman if s is not None and s.complete else (s.priced_subtotal_toman if s else None)
        change, change_pct = _change(total, prev_total)
        prev_key = keys[i - 1]
        movers = []
        if has_paid and counted[prev_key]:
            movers = ([{"kind": "provider", **labels.get(m["key"], {}), **m}
                       for m in _movers(paid[key], paid[prev_key], percent, minimum)]
                      + [{"kind": "team", "team_id": m["key"], "name": teams.get(m["key"]), **m}
                         for m in _movers({t["team_id"]: t["paid_toman"] for t in by_team[key] if t["team_id"]},
                                          {t["team_id"]: t["paid_toman"] for t in by_team[prev_key] if t["team_id"]},
                                          percent, minimum)])
        first, last_day = month_days(key)
        out.append({"month": key, "starts_on": first, "ends_on": last_day, "paid_toman": total,
                    "entries": len(counted[key]), "change_toman": change, "change_percent": change_pct,
                    "estimated_toman": est_total, "estimate_complete": s.complete if s else None,
                    "snapshot": snapshot_view(s) if s else None,
                    "gap": _gap(total, est_total, has_paid), "by_provider": by_provider, "by_team": teams_now,
                    "movers": movers})
        prev_total = total
    review = db.scalars(select(SpendEntry).where(SpendEntry.organization_id == org_id,
                                                 SpendEntry.status == "needs_review")
                        .order_by(SpendEntry.created_at)).all()
    paid_months = [m["paid_toman"] for m in out if m["paid_toman"] is not None]
    return {"current_month": last, "gap_percent": GAP_PERCENT,
            "unusual_change_percent": percent, "unusual_change_min_toman": minimum,
            "inventory": {"id": inventory.id, "name": inventory.name} if inventory else None,
            "teams": [{"id": k, "name": v} for k, v in sorted(teams.items(), key=lambda t: t[1])],
            "period_paid_toman": sum(paid_months, Decimal(0)) if paid_months else None,
            "months": out, "to_review": [entry_view(e, db) for e in review],
            "generated_at": datetime.now(UTC)}


# --------------------------------------------------------------------------- budgets


def budget_status(db: Session, org: Organization, now: datetime | None = None) -> dict[str, Any]:
    """This month's budgets: paid so far, a projected month-end, and how much of the budget is used.

    The projection is the largest of what is paid so far, this month's estimate and last month's total: payments
    arrive in lumps (a monthly plan is paid at once), so a straight-line projection from a few days would mislead.
    """
    s = summary(db, org.id, months=2, now=now)
    cur, prev = s["months"][-1], s["months"][-2]
    teams = {t["id"]: t["name"] for t in s["teams"]}

    def figures(scope: str) -> tuple[Decimal, Decimal | None, Decimal | None]:
        if scope == ORG_SCOPE:
            return cur["paid_toman"] or Decimal(0), cur["estimated_toman"], prev["paid_toman"]
        team = next((t for t in cur["by_team"] if t["team_id"] == scope), None)
        before = next((t for t in prev["by_team"] if t["team_id"] == scope), None)
        return ((team or {}).get("paid_toman") or Decimal(0), (team or {}).get("estimated_toman"),
                (before or {}).get("paid_toman") or None)

    out = []
    budgets = db.scalars(select(Budget).where(Budget.organization_id == org.id)).all()
    for b in sorted(budgets, key=lambda b: (b.scope != ORG_SCOPE, teams.get(b.scope, ""))):
        if b.scope != ORG_SCOPE and b.scope not in teams:
            continue
        mtd, est, last = figures(b.scope)
        projected = max([v for v in (mtd, est, last) if v is not None])
        used = (mtd * 100 / b.monthly_toman).quantize(Decimal("0.1"))
        level = 100 if mtd >= b.monthly_toman else 80 if mtd * 100 >= b.monthly_toman * 80 else 0
        out.append({"id": b.id, "scope": b.scope, "team_id": None if b.scope == ORG_SCOPE else b.scope,
                    "name": teams.get(b.scope), "monthly_toman": b.monthly_toman, "paid_toman": mtd,
                    "estimated_toman": est, "last_month_toman": last, "projected_toman": projected,
                    "used_percent": used, "level": level, "projected_over": projected > b.monthly_toman})
    return {"month": s["current_month"], "inventory": s["inventory"], "budgets": out, "teams": s["teams"]}


def _money(v: Decimal, lang: str) -> str:
    text = format(v.quantize(Decimal(1)), ",f")
    return f"{fa_number(text)} تومان" if lang == "fa" else f"{text} Toman"


def budget_alerts(db: Session, now: datetime | None = None) -> int:
    """Alert when a budget's month-to-date passes 80% and again at 100%, each once a month."""
    from .alerts import notify

    sent = 0
    org_ids = set(db.scalars(select(Budget.organization_id)))
    for org in db.scalars(select(Organization).where(Organization.id.in_(org_ids))).all():
        lang = (org.settings or {}).get("default_language", "fa")
        status = budget_status(db, org, now)
        month = status["month"]
        for st in status["budgets"]:
            b = db.get(Budget, st["id"])
            done = b.alerted if (b.alerted or {}).get("month") == month else {}
            if not st["level"] or st["level"] <= done.get("level", 0):
                continue
            who = st["name"] or (org.name if lang == "en" else f"کل {org.name}")
            pct = format(st["used_percent"], "f")
            if lang == "fa":
                subject = f"بودجه {who}: {fa_number(pct)}٪ مصرف شد"
                body = (f"پرداختی {month_label(month, 'fa')} تا امروز {_money(st['paid_toman'], 'fa')} است، از بودجه "
                        f"{_money(st['monthly_toman'], 'fa')}. پیش‌بینی پایان ماه: {_money(st['projected_toman'], 'fa')}.")
            else:
                subject = f"Budget {who}: {pct}% used"
                body = (f"Paid so far in {month_label(month, 'en')}: {_money(st['paid_toman'], 'en')} of a "
                        f"{_money(st['monthly_toman'], 'en')} budget. Projected for the month: "
                        f"{_money(st['projected_toman'], 'en')}.")
            notify(subject, body, org_id=org.id)
            b.alerted = {"month": month, "level": st["level"]}
            db.add(AuditLog(organization_id=org.id, action="budget.alert", entity_type="budget", entity_id=b.id,
                            detail={"month": month, "level": st["level"], "paid_toman": str(st["paid_toman"])}))
            sent += 1
    db.commit()
    return sent


def unusual_change_alerts(db: Session, now: datetime | None = None) -> int:
    """Once a month, from the 5th (when last month's invoices are usually in), alert on providers and teams whose
    last month moved by more than the organization's threshold from the month before."""
    from .alerts import notify

    if to_jalali(today_in_iran(now))[2] < UNUSUAL_CHECK_DAY:
        return 0
    previous = shift_month(current_month(now), -1)
    sent = 0
    org_ids = set(db.scalars(select(SpendEntry.organization_id).where(SpendEntry.month == previous)))
    for org in db.scalars(select(Organization).where(Organization.id.in_(org_ids))).all():
        settings = dict(org.settings or {})
        if settings.get("unusual_checked_month") == previous:
            continue
        lang = settings.get("default_language", "fa")
        month = summary(db, org.id, months=2, now=now)["months"][-2]
        names = {}
        if month["movers"]:
            from .services import snapshot as catalog

            providers = catalog(db).providers
            for m in month["movers"]:
                if m["kind"] == "team":
                    names[id(m)] = m["name"] or "—"
                elif m.get("provider_id") in providers:
                    pr = providers[m["provider_id"]]
                    names[id(m)] = (pr.name_fa or pr.name_en) if lang == "fa" else pr.name_en
                else:
                    names[id(m)] = m.get("vendor") or m["key"]
            lines = []
            for m in month["movers"]:
                pct = format(abs(m["change_percent"]), "f")
                up = m["change_toman"] > 0
                if lang == "fa":
                    lines.append(f"{names[id(m)]}: {_money(m['current_toman'], 'fa')}، {fa_number(pct)}٪ "
                                 f"{'بیشتر' if up else 'کمتر'} از ماه قبل ({_money(m['previous_toman'], 'fa')})")
                else:
                    lines.append(f"{names[id(m)]}: {_money(m['current_toman'], 'en')}, {pct}% "
                                 f"{'up' if up else 'down'} on the month before ({_money(m['previous_toman'], 'en')})")
            label = month_label(previous, lang)
            notify(f"تغییر غیرعادی هزینه در {label}" if lang == "fa" else f"Unusual spend changes in {label}",
                   "\n".join(lines), org_id=org.id)
            sent += 1
        settings["unusual_checked_month"] = previous
        org.settings = settings
    db.commit()
    return sent
