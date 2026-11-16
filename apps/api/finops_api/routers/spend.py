"""The spend ledger: record what was paid, read it back by month, and compare it with the monthly snapshot."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoice_parser import extract_text, parse_invoice

from ..config import get_settings
from ..dates import MONTH_PATTERN, current_month, parse_day, parse_month_text
from ..db import get_db
from ..orm import CostSnapshot, SpendEntry, Upload
from ..platform import store_private, validate_upload
from ..security import Principal, audit, current_principal, require
from ..services import get_inventory, snapshot
from ..spend import RIAL_PER_TOMAN, entry_from_invoice, entry_view, snapshot_view, summary, take_snapshot

router = APIRouter(prefix="/api/v1", tags=["spend"])

MAX_AMOUNT = Decimal("1e15")


def _day(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return parse_day(str(v))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


class EntryFields(BaseModel):
    model_config = {"extra": "forbid"}
    month: str | None = Field(default=None, pattern=MONTH_PATTERN)
    provider_id: str | None = Field(default=None, max_length=50)
    vendor: str | None = Field(default=None, max_length=200)
    amount: Decimal | None = Field(default=None, gt=0, le=MAX_AMOUNT)
    #: the unit `amount` is in; stored in Toman (Rial ÷ 10)
    currency: Literal["TOMAN", "RIAL"] = "TOMAN"
    source: Literal["manual", "wallet_topup"] | None = None
    #: Solar Hijri (1405/08/20) or ISO (2026-11-10)
    paid_on: date | None = None
    reference: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)

    _paid_on = field_validator("paid_on", mode="before")(_day)


class EntryIn(EntryFields):
    month: str = Field(pattern=MONTH_PATTERN)
    amount: Decimal = Field(gt=0, le=MAX_AMOUNT)


class EntryPatch(EntryFields):
    #: mark an entry read from an invoice as checked, so it counts
    confirm: bool = False


def _payee(db: Session, provider_id: str | None, vendor: str | None) -> tuple[str | None, str | None]:
    vendor = (vendor or "").strip() or None
    if provider_id:
        if provider_id not in snapshot(db).providers:
            raise HTTPException(422, f"unknown provider '{provider_id}'; name it as a vendor instead")
        return provider_id, None
    return None, vendor


def _toman(amount: Decimal, currency: str) -> Decimal:
    value = amount / RIAL_PER_TOMAN if currency == "RIAL" else amount
    if value != value.quantize(Decimal("0.0001")):
        raise HTTPException(422, "amount has too many decimal places")
    return value


def _entry(db: Session, p: Principal, entry_id: str) -> SpendEntry:
    e = db.get(SpendEntry, entry_id)
    if e is None or e.organization_id != p.org.id:
        raise HTTPException(404, "spend entry not found")
    return e


def _audit_detail(e: SpendEntry) -> dict[str, Any]:
    return {"month": e.month, "provider_id": e.provider_id, "vendor": e.vendor,
            "amount_toman": str(e.amount_toman) if e.amount_toman is not None else None, "status": e.status}


@router.get("/spend")
def spend_summary(inventory_id: str | None = None, months: int = Query(12, ge=1, le=60),
                  db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    """Paid per month and provider; with `inventory_id`, that inventory's monthly estimates and the gaps."""
    inv = get_inventory(db, p, inventory_id) if inventory_id else None
    return summary(db, p.org.id, inv, months)


@router.get("/spend/entries")
def list_entries(month: str | None = Query(None, pattern=MONTH_PATTERN), db: Session = Depends(get_db),
                 p: Principal = Depends(current_principal)):
    q = select(SpendEntry).where(SpendEntry.organization_id == p.org.id)
    if month:
        q = q.where(SpendEntry.month == month)
    rows = db.scalars(q.order_by(SpendEntry.month.desc(), SpendEntry.created_at.desc()).limit(1000))
    return [entry_view(e, db) for e in rows]


@router.post("/spend/entries")
def create_entry(body: EntryIn, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    provider_id, vendor = _payee(db, body.provider_id, body.vendor)
    if provider_id is None and vendor is None:
        raise HTTPException(422, "choose a provider, or name the vendor")
    e = SpendEntry(organization_id=p.org.id, month=body.month, provider_id=provider_id, vendor=vendor,
                   amount_toman=_toman(body.amount, body.currency), source=body.source or "manual",
                   status="confirmed", paid_on=body.paid_on, reference=body.reference, note=body.note,
                   evidence={"entered": {"amount": str(body.amount), "currency": body.currency}}
                   if body.currency == "RIAL" else {},
                   created_by=p.user.id)
    db.add(e)
    db.flush()
    audit(db, p, "spend.create", "spend_entry", e.id, _audit_detail(e))
    db.commit()
    return entry_view(e, db)


@router.patch("/spend/entries/{entry_id}")
def update_entry(entry_id: str, body: EntryPatch, db: Session = Depends(get_db),
                 p: Principal = Depends(require("analyst"))):
    e = _entry(db, p, entry_id)
    given = body.model_fields_set
    if "month" in given and body.month:
        e.month = body.month
    if given & {"provider_id", "vendor"}:
        e.provider_id, e.vendor = _payee(db, body.provider_id if "provider_id" in given else e.provider_id,
                                         body.vendor if "vendor" in given else e.vendor)
    if "amount" in given:
        e.amount_toman = _toman(body.amount, body.currency) if body.amount is not None else None
        if body.currency == "RIAL" and body.amount is not None:
            e.evidence = {**(e.evidence or {}), "entered": {"amount": str(body.amount), "currency": "RIAL"}}
    if "source" in given and body.source and e.source in ("manual", "wallet_topup"):
        e.source = body.source
    for f in ("paid_on", "reference", "note"):
        if f in given:
            setattr(e, f, getattr(body, f))
    if e.status == "confirmed" or body.confirm:
        missing = [n for n, ok in (("amount", e.amount_toman is not None),
                                   ("provider", e.provider_id is not None or e.vendor is not None)) if not ok]
        if missing:
            raise HTTPException(422, f"before it counts, the entry needs: {', '.join(missing)}")
    if body.confirm:
        e.status = "confirmed"
    audit(db, p, "spend.confirm" if body.confirm else "spend.update", "spend_entry", e.id, _audit_detail(e))
    db.commit()
    return entry_view(e, db)


@router.delete("/spend/entries/{entry_id}")
def delete_entry(entry_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    e = _entry(db, p, entry_id)
    audit(db, p, "spend.delete", "spend_entry", e.id, _audit_detail(e) | {"source": e.source})
    db.delete(e)
    db.commit()
    return {"id": entry_id, "deleted": True}


@router.post("/spend/from-invoice")
async def spend_from_invoice(file: UploadFile = File(...), db: Session = Depends(get_db),
                             p: Principal = Depends(require("analyst"))):
    """Read the provider, total and date off an invoice into an entry that waits for someone to check it."""
    data = await file.read()
    kind = validate_upload(file.filename or "upload", file.content_type or "", data)
    try:
        text = extract_text(data, kind)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - malformed PDFs etc.
        raise HTTPException(422, f"could not read file: {type(exc).__name__}") from exc
    sku_map = {i.sku: i.provider_id for i in snapshot(db).items if i.sku}
    ext = parse_invoice(text, sku_map)
    upload = Upload(organization_id=p.org.id, filename=file.filename or "upload", content_type=kind,
                    size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                    delete_after=datetime.now(UTC) + timedelta(days=get_settings().invoice_retention_days))
    db.add(upload)
    db.flush()
    upload.storage_path = store_private(p.org.id, upload.id, data)
    e = entry_from_invoice(db, p.org.id, p.user.id, ext, upload)
    audit(db, p, "spend.from_invoice", "spend_entry", e.id, {"upload_id": upload.id, **_audit_detail(e)})
    db.commit()
    return entry_view(e, db)


# --------------------------------------------------------------------------- CSV import

COLUMNS = {
    "month": r"month|period|ماه|دوره",
    "provider": r"provider|vendor|supplier|ارائه[‌ ]?دهنده|تامین[‌ ]?کننده|شرکت",
    "amount": r"amount|paid|total|cost|مبلغ|پرداختی|هزینه",
    "currency": r"currency|unit|واحد|ارز",
    "paid_on": r"paid[ _]?on|payment[ _]?date|date|تاریخ(?:[‌ ]?پرداخت)?",
    "reference": r"reference|ref|invoice|شماره|فاکتور",
    "note": r"note|description|comment|توضیح(?:ات)?",
}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬٫", "01234567890123456789,.")


def _columns(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for i, cell in enumerate(header):
        name = cell.strip().lower()
        for key, pattern in COLUMNS.items():
            if key not in found and re.fullmatch(rf"(?:{pattern})", name, re.I):
                found[key] = i
                break
    return found


def _provider_lookup(db: Session) -> dict[str, str]:
    names: dict[str, str] = {}
    for pid, prov in snapshot(db).providers.items():
        for n in (pid, prov.name_en, prov.name_fa):
            if n:
                names[n.strip().lower()] = pid
    return names


@router.post("/spend/import")
async def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db),
                     p: Principal = Depends(require("analyst"))):
    """Spend history from a spreadsheet: columns month, provider and amount (others optional). Every row is checked
    first; if any is wrong, nothing is saved and each problem is listed with its row number."""
    data = await file.read()
    if len(data) > 2_000_000:
        raise HTTPException(413, "the file is larger than 2 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "save the spreadsheet as CSV (UTF-8)") from exc
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise HTTPException(422, "the file needs a header row and at least one row of spend")
    cols = _columns(rows[0])
    missing = [k for k in ("month", "provider", "amount") if k not in cols]
    if missing:
        raise HTTPException(422, {"message": f"missing column(s): {', '.join(missing)}",
                                  "expected": "month, provider, amount[, currency, paid_on, reference, note]"})
    lookup = _provider_lookup(db)
    entries: list[SpendEntry] = []
    errors: list[dict[str, Any]] = []
    if len(rows) > 5001:
        raise HTTPException(422, "import at most 5000 rows at a time")
    for n, row in enumerate(rows[1:], start=2):
        def cell(key: str, row: list[str] = row) -> str:
            i = cols.get(key)
            return row[i].strip() if i is not None and i < len(row) else ""
        try:
            month = parse_month_text(cell("month"))
            raw = cell("amount").translate(_DIGITS).replace(",", "").replace(" ", "")
            amount = Decimal(raw)
            if not Decimal(0) < amount <= MAX_AMOUNT:
                raise ValueError("amount must be more than zero")
            unit = cell("currency").lower()
            currency = "RIAL" if unit in ("rial", "irr", "ریال") else "TOMAN"
            if unit and unit not in ("toman", "irt", "تومان", "rial", "irr", "ریال"):
                raise ValueError(f"unknown currency '{cell('currency')}': use Toman or Rial")
            payee = cell("provider")
            if not payee:
                raise ValueError("provider is empty")
            provider_id = lookup.get(payee.lower())
            paid_on = parse_day(cell("paid_on")) if cell("paid_on") else None
        except (ValueError, InvalidOperation) as exc:
            errors.append({"row": n, "message": str(exc) or "amount is not a number"})
            continue
        try:
            amount_toman = _toman(amount, currency)
        except HTTPException as exc:
            errors.append({"row": n, "message": exc.detail})
            continue
        entries.append(SpendEntry(organization_id=p.org.id, month=month, provider_id=provider_id,
                                  vendor=None if provider_id else payee[:200], amount_toman=amount_toman,
                                  source="csv", status="confirmed", paid_on=paid_on,
                                  reference=cell("reference")[:200] or None, note=cell("note")[:2000] or None,
                                  evidence={"file": file.filename, "row": n}, created_by=p.user.id))
    if errors:
        raise HTTPException(422, {"message": f"{len(errors)} row(s) need fixing; nothing was imported",
                                  "errors": errors[:100]})
    db.add_all(entries)
    audit(db, p, "spend.import", "organization", p.org.id, {"file": file.filename, "rows": len(entries)})
    db.commit()
    return {"imported": len(entries), "months": sorted({e.month for e in entries})}


# --------------------------------------------------------------------------- snapshots


@router.get("/inventories/{inventory_id}/snapshots")
def list_snapshots(inventory_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    inv = get_inventory(db, p, inventory_id)
    rows = db.scalars(select(CostSnapshot).where(CostSnapshot.inventory_id == inv.id)
                      .order_by(CostSnapshot.month.desc()))
    return [snapshot_view(s) for s in rows]


@router.post("/inventories/{inventory_id}/snapshot")
def snapshot_now(inventory_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    """Price this month's estimate again, at today's prices and the inventory's last confirmed version. Past months'
    estimates stay as they were taken."""
    inv = get_inventory(db, p, inventory_id)
    row = take_snapshot(db, inv, current_month(), "manual", p.user.id)
    if row is None:
        raise HTTPException(409, "inventory must be confirmed before its cost can be estimated")
    db.commit()
    return snapshot_view(row)
