"""The spend ledger: what a team paid each month, read from invoices or typed in, against the monthly estimate."""

import itertools
from decimal import Decimal

import pytest

from finops_api.dates import current_month, shift_month

_n = itertools.count()
V = "/api/v1"


@pytest.fixture
def org(client):
    """A fresh organization, so totals aren't shared with other tests."""
    r = client.post(f"{V}/auth/register", json={"email": f"payer{next(_n)}@example.ir", "password": "owner-password-1",
                                                "name": "Owner", "organization_name": "Payers"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _month(client, h, key, inventory_id=None):
    qs = f"?inventory_id={inventory_id}" if inventory_id else ""
    data = client.get(f"{V}/spend{qs}", headers=h).json()
    return next(m for m in data["months"] if m["month"] == key)


def _add(client, h, **body):
    r = client.post(f"{V}/spend/entries", headers=h, json={"month": current_month(), **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_payments_add_up_by_month_and_provider(client, org):
    now, before = current_month(), shift_month(current_month(), -1)
    _add(client, org, provider_id="liara", amount="2000000", month=before)
    _add(client, org, provider_id="liara", amount="1500000", source="wallet_topup")
    _add(client, org, provider_id="liara", amount="15000000", currency="RIAL", reference="TX-1")  # 1,500,000 Toman
    _add(client, org, vendor="Hetzner", amount="900000", note="backup box")
    m = _month(client, org, now)
    assert m["paid_toman"] == "3900000" and m["entries"] == 3
    assert {p["key"]: p["paid_toman"] for p in m["by_provider"]} == {"liara": "3000000", "vendor:hetzner": "900000"}
    # month over month, from the 2,000,000 paid before
    assert m["change_toman"] == "1900000" and m["change_percent"] == "95"
    assert m["starts_on"] <= m["ends_on"]
    # nothing to compare with until an inventory's estimate is asked for
    assert m["estimated_toman"] is None and m["gap"] is None


def test_an_entry_needs_a_payee_a_known_provider_and_a_real_month(client, org):
    def post(**body):
        return client.post(f"{V}/spend/entries", headers=org, json={"month": current_month(), "amount": "1000", **body})

    assert post().status_code == 422  # no provider or vendor
    assert post(provider_id="nope").status_code == 422
    assert post(provider_id="liara", month="2026-11").status_code == 422  # Gregorian months span two Solar months
    assert post(provider_id="liara", amount="-5").status_code == 422
    assert post(provider_id="liara", paid_on="1404/12/30").status_code == 422  # 1404 is not a leap year
    ok = post(provider_id="liara", paid_on="۱۴۰۵/۰۸/۲۰")
    assert ok.status_code == 200 and ok.json()["paid_on"] == "2026-11-11"


def test_edit_and_delete_an_entry(client, org):
    e = _add(client, org, provider_id="arvancloud", amount="500000")
    r = client.patch(f"{V}/spend/entries/{e['id']}", headers=org, json={"amount": "750000", "note": "late fee"})
    assert r.status_code == 200 and r.json()["amount_toman"] == "750000" and r.json()["note"] == "late fee"
    assert client.patch(f"{V}/spend/entries/{e['id']}", headers=org, json={"provider_id": None}).status_code == 422
    assert client.delete(f"{V}/spend/entries/{e['id']}", headers=org).status_code == 200
    assert client.get(f"{V}/spend/entries?month={current_month()}", headers=org).json() == []
    actions = [a["action"] for a in client.get(f"{V}/audit-logs", headers=org).json()]
    assert {"spend.create", "spend.update", "spend.delete"} <= set(actions)


INVOICE = """صورتحساب سرور.آی‌آر
تاریخ صدور: ۱۴۰۵/۰۷/۲۸
سررسید: ۱۴۰۵/۰۸/۰۵
IRAT3024 x 2  ۷۰٬۰۰۰٬۰۰۰ ریال
مالیات: ۷٬۰۰۰٬۰۰۰ ریال
مبلغ قابل پرداخت: ۷۷٬۰۰۰٬۰۰۰ ریال
"""


def test_an_invoice_becomes_an_entry_to_check(client, org):
    r = client.post(f"{V}/spend/from-invoice", headers=org,
                    files={"file": ("invoice.csv", INVOICE.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["status"] == "needs_review" and e["source"] == "invoice"
    assert e["provider_id"] == "serverir" and e["month"] == "1405-07"  # the month it was issued, Mehr
    assert e["amount_toman"] == "7700000"  # 77,000,000 Rial, the amount payable
    assert e["evidence"]["detected_currency"] == "RIAL" and "قابل پرداخت" in e["evidence"]["total_evidence"]
    assert e["upload"]["filename"] == "invoice.csv"
    # waiting for review, it doesn't count yet, and the page says it is waiting
    summary = client.get(f"{V}/spend", headers=org).json()
    assert [x["id"] for x in summary["to_review"]] == [e["id"]]
    assert all(m["paid_toman"] is None for m in summary["months"])
    # the same file again is the same entry, not a second payment
    again = client.post(f"{V}/spend/from-invoice", headers=org,
                        files={"file": ("copy.csv", INVOICE.encode(), "text/csv")}).json()
    assert again["id"] == e["id"]

    ok = client.patch(f"{V}/spend/entries/{e['id']}", headers=org, json={"confirm": True})
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    paid = {m["month"]: m["paid_toman"] for m in client.get(f"{V}/spend?months=60", headers=org).json()["months"]}
    assert paid["1405-07"] == "7700000"
    assert client.get(f"{V}/spend", headers=org).json()["to_review"] == []


def test_an_invoice_in_an_unknown_currency_waits_for_the_amount(client, org):
    e = client.post(f"{V}/spend/from-invoice", headers=org,
                    files={"file": ("i.csv", b"Plan | Price\nNGP-small40 | 2012399\n", "text/csv")}).json()
    assert e["status"] == "needs_review" and e["amount_toman"] is None
    assert e["evidence"]["total"] == "2012399" and e["evidence"]["detected_currency"] == "UNKNOWN"
    assert e["month"] == current_month()  # no date on it
    assert e["provider_id"] == "iranserver"  # no name on it either, but its one plan is Iranserver's
    r = client.patch(f"{V}/spend/entries/{e['id']}", headers=org, json={"confirm": True})
    assert r.status_code == 422  # not without an amount
    r = client.patch(f"{V}/spend/entries/{e['id']}", headers=org,
                     json={"amount": "2012399", "currency": "RIAL", "confirm": True})
    assert r.status_code == 200 and r.json()["amount_toman"] == "201239.9"


def test_an_invoice_for_an_inventory_also_goes_to_the_ledger(client, org):
    inv = client.post(f"{V}/inventories/from-invoice", headers=org,
                      files={"file": ("inv.csv", INVOICE.encode(), "text/csv")}).json()
    entry_id = inv["source_meta"]["spend_entry_id"]
    entries = client.get(f"{V}/spend/entries?month=1405-07", headers=org).json()
    assert [(e["id"], e["status"], e["inventory_id"]) for e in entries] == [(entry_id, "needs_review", inv["id"])]


def test_the_estimate_is_taken_on_confirmation_and_compared_with_what_was_paid(client, org):
    demo = client.post(f"{V}/inventories/demo", headers=org).json()
    now = current_month()
    # before it's confirmed there is nothing to estimate, only the sample payments
    m = _month(client, org, now, demo["id"])
    assert m["paid_toman"] == "23690000" and m["estimated_toman"] is None
    assert client.post(f"{V}/inventories/{demo['id']}/confirm", headers=org,
                       json={"confirm_all_rows": True}).status_code == 200

    m = _month(client, org, now, demo["id"])
    # 20,574,439 for the setup and Server.ir's 350,000 VAT, as an invoice would charge it
    assert m["estimated_toman"] == "20924439" and m["estimate_complete"] is True
    assert m["snapshot"]["trigger"] == "confirm"
    assert m["gap"] == {"kind": "paid_more", "diff_toman": "2765561", "percent": "13.2"}
    gaps = {p["key"]: p["gap"] for p in m["by_provider"]}
    assert gaps["arvancloud"]["kind"] == "paid_more" and gaps["arvancloud"]["percent"] == "28.4"
    assert gaps["serverir"] is None  # 3,500,000 and its VAT, paid exactly
    assert gaps["liara"] is None and gaps["iranserver"] is None  # within 10%
    assert m["change_toman"] == "2545000" and m["change_percent"] == "12"
    # an earlier month has payments but no estimate: nothing is made up for it
    before = _month(client, org, shift_month(now, -1), demo["id"])
    assert before["paid_toman"] == "21145000" and before["estimated_toman"] is None and before["gap"] is None

    # taking it again replaces this month's, and keeps one per month
    again = client.post(f"{V}/inventories/{demo['id']}/snapshot", headers=org)
    assert again.status_code == 200 and again.json()["trigger"] == "manual"
    snaps = client.get(f"{V}/inventories/{demo['id']}/snapshots", headers=org).json()
    assert [(s["month"], s["trigger"]) for s in snaps] == [(now, "manual")]


def test_deleting_the_sample_takes_its_payments_and_estimates(client, org):
    demo = client.post(f"{V}/inventories/demo", headers=org).json()
    client.post(f"{V}/inventories/{demo['id']}/confirm", headers=org, json={"confirm_all_rows": True})
    mine = _add(client, org, provider_id="liara", amount="100000")
    assert client.delete(f"{V}/inventories/{demo['id']}", headers=org).status_code == 200
    left = client.get(f"{V}/spend/entries", headers=org).json()
    assert [e["id"] for e in left] == [mine["id"]]  # a real payment stays
    assert client.get(f"{V}/inventories/{demo['id']}/snapshots", headers=org).json() == []


def test_the_monthly_job_estimates_every_confirmed_inventory_once(client, org, api_env):
    from finops_api.db import SessionLocal
    from finops_api.orm import CostSnapshot
    from finops_api.spend import take_monthly_snapshots

    confirmed = client.post(f"{V}/inventories/demo", headers=org).json()
    client.post(f"{V}/inventories/{confirmed['id']}/confirm", headers=org, json={"confirm_all_rows": True})
    never = client.post(f"{V}/inventories/demo", headers=org).json()
    with SessionLocal() as db:  # as if the month had just begun: nothing taken yet
        db.query(CostSnapshot).filter(CostSnapshot.inventory_id == confirmed["id"]).delete()
        db.commit()
        take_monthly_snapshots(db)
        take_monthly_snapshots(db)  # again within the month: nothing new
        rows = db.query(CostSnapshot).filter(CostSnapshot.inventory_id.in_([confirmed["id"], never["id"]])).all()
    assert [(r.inventory_id, r.trigger) for r in rows] == [(confirmed["id"], "schedule")]

    # during an edit, the month's estimate prices the version confirmed last
    rid = confirmed["resources"][0]["id"]
    client.patch(f"{V}/inventories/{confirmed['id']}/resources/{rid}", headers=org, json={"fields": {"quantity": "9"}})
    snap = client.post(f"{V}/inventories/{confirmed['id']}/snapshot", headers=org).json()
    assert Decimal(snap["total_toman"]) == Decimal("20924439")


def test_spend_history_from_a_spreadsheet(client, org):
    csv_text = ("ماه,ارائه‌دهنده,مبلغ,واحد,توضیحات\n"
                "۱۴۰۵/۰۵,Liara,۲٬۵۰۰٬۰۰۰,تومان,\n"
                "1405-06,لیارا,25000000,ریال,wallet\n"
                "2026-10-01,Hetzner,800000,,storage box\n")
    r = client.post(f"{V}/spend/import", headers=org, files={"file": ("history.csv", csv_text.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json() == {"imported": 3, "months": ["1405-05", "1405-06", "1405-07"]}
    entries = {e["month"]: e for e in client.get(f"{V}/spend/entries", headers=org).json()}
    assert entries["1405-05"]["provider_id"] == "liara" and entries["1405-05"]["amount_toman"] == "2500000"
    assert entries["1405-06"]["amount_toman"] == "2500000" and entries["1405-06"]["note"] == "wallet"
    assert entries["1405-07"]["vendor"] == "Hetzner" and entries["1405-07"]["provider_id"] is None
    assert all(e["source"] == "csv" for e in entries.values())


def test_a_spreadsheet_with_mistakes_imports_nothing(client, org):
    csv_text = "month,provider,amount\n1405-05,liara,abc\n2026-10,liara,100\n1405-06,,100\n1405-07,liara,100\n"
    r = client.post(f"{V}/spend/import", headers=org, files={"file": ("h.csv", csv_text.encode(), "text/csv")})
    assert r.status_code == 422
    assert [e["row"] for e in r.json()["detail"]["errors"]] == [2, 3, 4]
    assert client.get(f"{V}/spend/entries", headers=org).json() == []
    r = client.post(f"{V}/spend/import", headers=org, files={"file": ("h.csv", b"when,who\n1,2\n", "text/csv")})
    assert r.status_code == 422 and "missing column" in r.json()["detail"]["message"]


def test_viewers_read_and_other_organizations_see_nothing(client, org):
    e = _add(client, org, provider_id="liara", amount="100000")
    r = client.post(f"{V}/members", headers=org, json={"email": f"viewer{next(_n)}@example.ir",
                                                        "password": "viewer-password-1", "name": "V", "role": "viewer"})
    assert r.status_code == 200, r.text
    login = client.post(f"{V}/auth/login", json={"email": r.json()["email"], "password": "viewer-password-1"}).json()
    viewer = {"Authorization": f"Bearer {login['access_token']}"}
    assert client.get(f"{V}/spend", headers=viewer).status_code == 200
    assert client.post(f"{V}/spend/entries", headers=viewer,
                       json={"month": current_month(), "provider_id": "liara", "amount": "1"}).status_code == 403
    assert client.delete(f"{V}/spend/entries/{e['id']}", headers=viewer).status_code == 403

    other = client.post(f"{V}/auth/register", json={"email": f"other{next(_n)}@example.ir", "password": "other-pass-12",
                                                    "name": "O", "organization_name": "Other"}).json()
    oh = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(f"{V}/spend/entries", headers=oh).json() == []
    assert client.patch(f"{V}/spend/entries/{e['id']}", headers=oh, json={"note": "x"}).status_code == 404
    assert client.delete(f"{V}/spend/entries/{e['id']}", headers=oh).status_code == 404
