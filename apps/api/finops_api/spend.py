"""The spend ledger (what was paid each month), the monthly cost snapshot (what the confirmed setup should cost), and
how the two compare.

Months are Solar Hijri ("1405-08"), see dates.py. Totals only count confirmed entries: an entry read from an invoice
waits for someone to check its amount and month first.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoice_parser import InvoiceExtraction
from pricing_engine import BillingContext, price_inventory
from pricing_engine.models import Quote, Resource

from .dates import current_month, month_days, month_key, month_of, shift_month
from .orm import AuditLog, CostSnapshot, Inventory, QuoteRow, SpendEntry, Upload
from .services import last_confirmed_resources, snapshot

RIAL_PER_TOMAN = Decimal(10)
#: paid and estimated further apart than this (in percent of the estimate) is worth a look
GAP_PERCENT = Decimal(10)


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
                       provider_totals=_provider_totals(quote), trigger=trigger, created_by=user_id)
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


def summary(db: Session, org_id: str, inventory: Inventory | None, months: int = 12,
            now: datetime | None = None) -> dict[str, Any]:
    """The last `months` months, oldest first: paid per provider, the change from the month before, and, for the
    inventory the estimates come from, the snapshot and the gaps between them."""
    last = current_month(now)
    keys = [shift_month(last, -i) for i in range(months, -1, -1)]  # one extra month before, for the first change
    entries = db.scalars(select(SpendEntry).where(SpendEntry.organization_id == org_id,
                                                  SpendEntry.month.in_(keys))).all()
    paid: dict[str, dict[str, Decimal]] = {k: defaultdict(Decimal) for k in keys}
    labels: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.status != "confirmed" or e.amount_toman is None:
            continue
        key = provider_key(e)
        paid[e.month][key] += e.amount_toman
        counts[e.month] += 1
        labels.setdefault(key, {"provider_id": e.provider_id, "vendor": e.vendor})
    snaps: dict[str, CostSnapshot] = {}
    if inventory is not None:
        snaps = {s.month: s for s in db.scalars(select(CostSnapshot).where(CostSnapshot.inventory_id == inventory.id,
                                                                           CostSnapshot.month.in_(keys)))}
    out = []
    prev_total: Decimal | None = None
    for i, key in enumerate(keys):
        total = sum(paid[key].values(), Decimal(0)) if counts[key] else None
        if i == 0:
            prev_total = total
            continue
        s = snaps.get(key)
        estimated: dict[str, Decimal] = {}
        if s is not None:
            for pt in s.provider_totals:
                estimated[pt["provider_id"]] = Decimal(pt["monthly_toman"]) + Decimal(pt.get("tax_toman") or 0)
                labels.setdefault(pt["provider_id"], {"provider_id": pt["provider_id"], "vendor": None})
        has_paid = counts[key] > 0
        by_provider = []
        for pk in sorted(set(paid[key]) | set(estimated)):
            p_amount = paid[key].get(pk) if has_paid else None
            e_amount = estimated.get(pk, Decimal(0)) if s is not None else None
            by_provider.append({"key": pk, **labels[pk], "paid_toman": p_amount, "estimated_toman": e_amount,
                                "gap": _gap(p_amount, e_amount, has_paid)})
        est_total = s.total_toman if s is not None and s.complete else (s.priced_subtotal_toman if s else None)
        change, change_pct = _change(total, prev_total)
        first, last_day = month_days(key)
        out.append({"month": key, "starts_on": first, "ends_on": last_day, "paid_toman": total,
                    "entries": counts[key], "change_toman": change, "change_percent": change_pct,
                    "estimated_toman": est_total, "estimate_complete": s.complete if s else None,
                    "snapshot": snapshot_view(s) if s else None,
                    "gap": _gap(total, est_total, has_paid), "by_provider": by_provider})
        prev_total = total
    review = db.scalars(select(SpendEntry).where(SpendEntry.organization_id == org_id,
                                                 SpendEntry.status == "needs_review")
                        .order_by(SpendEntry.created_at)).all()
    paid_months = [m["paid_toman"] for m in out if m["paid_toman"] is not None]
    return {"current_month": last, "gap_percent": GAP_PERCENT,
            "inventory": {"id": inventory.id, "name": inventory.name} if inventory else None,
            "period_paid_toman": sum(paid_months, Decimal(0)) if paid_months else None,
            "months": out, "to_review": [entry_view(e, db) for e in review],
            "generated_at": datetime.now(UTC)}
