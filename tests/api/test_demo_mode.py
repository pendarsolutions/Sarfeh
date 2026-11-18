"""A public demo: anyone gets in without an account, sees the sample workspace, and can change nothing."""

from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

V = "/api/v1"


@pytest.fixture
def demo(client, monkeypatch):
    """The API as a public demo runs it, for one test."""
    from finops_api.config import get_settings

    monkeypatch.setenv("FINOPS_DEMO_MODE", "true")
    get_settings.cache_clear()
    yield client
    monkeypatch.delenv("FINOPS_DEMO_MODE")
    get_settings.cache_clear()


@pytest.fixture
def visitor(demo):
    r = demo.post(f"{V}/auth/demo")
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_off_unless_asked_for(client):
    assert client.post(f"{V}/auth/demo").status_code == 404
    assert client.get(f"{V}/auth/status").json()["demo"] is False


def test_the_gate_lets_through_reads_and_calculations_only():
    from finops_api.demo import refuses

    assert not refuses("GET", "/api/v1/spend") and not refuses("HEAD", "/api/v1/inventories/abc/report")
    for path in ("/api/v1/auth/demo", "/api/v1/inventories/abc/quote", "/api/v1/inventories/abc/compare",
                 "/api/v1/advisor/chat"):
        assert not refuses("POST", path), path
        assert refuses("DELETE", path) and refuses("PUT", path), path
    for path in ("/api/v1/inventories/abc/confirm", "/api/v1/inventories/abc/quote/more", "/api/v1/auth/login",
                 "/api/v1/inventories", "/api/v1/servers", "/somewhere/api/v1/auth/demo"):
        assert refuses("POST", path), path


def test_a_visitor_gets_in_without_an_account_and_sees_the_sample_workspace(demo):
    status = demo.get(f"{V}/auth/status").json()
    assert status["demo"] is True and not status["setup_needed"] and not status["registration_open"]
    session = demo.post(f"{V}/auth/demo").json()
    assert session["role"] == "analyst" and session["organization"]["name"] == "شرکت نمونه"
    h = {"Authorization": f"Bearer {session['access_token']}"}

    (inv,) = demo.get(f"{V}/inventories", headers=h).json()
    assert inv["status"] == "confirmed"
    month = demo.get(f"{V}/spend", headers=h).json()["months"][-1]
    assert Decimal(month["paid_toman"]) == Decimal("23690000") and month["estimated_toman"]
    assert {t["name"] for t in month["by_team"]} == {"محصول", "زیرساخت", None}  # None: the unallocated line
    whole, *teams = demo.get(f"{V}/budgets", headers=h).json()["budgets"]
    # 23,690,000 paid of 26,000,000: close to the budget, which is what a demo should show
    assert (whole["scope"], whole["used_percent"], whole["level"]) == ("organization", "91.1", 80)
    assert {b["name"] for b in teams} == {"محصول", "زیرساخت"}
    recs = demo.get(f"{V}/inventories/{inv['id']}/recommendations", headers=h).json()
    assert recs["recommendations"] and recs["summary"]


def test_visitors_share_one_workspace(demo):
    a, b = demo.post(f"{V}/auth/demo").json(), demo.post(f"{V}/auth/demo").json()
    assert a["organization"]["id"] == b["organization"]["id"] and a["user"]["id"] == b["user"]["id"]
    h = {"Authorization": f"Bearer {b['access_token']}"}
    assert len(demo.get(f"{V}/inventories", headers=h).json()) == 1
    assert len(demo.get(f"{V}/teams", headers=h).json()) == 2


def test_nothing_can_be_changed(demo, visitor):
    inv = demo.get(f"{V}/inventories", headers=visitor).json()[0]["id"]

    def spend():
        return {k: v for k, v in demo.get(f"{V}/spend", headers=visitor).json().items() if k != "generated_at"}

    before = spend()
    writes = [
        ("post", "/inventories", {"name": "Mine", "resources": []}),
        ("post", "/inventories/demo", None),
        ("delete", f"/inventories/{inv}", None),
        ("post", f"/inventories/{inv}/archive", None),
        ("post", f"/inventories/{inv}/recommendations", None),
        ("post", "/spend/entries", {"month": "1405-08", "provider_id": "liara", "amount": "1"}),
        ("put", "/budgets", {"monthly_toman": "1"}),
        ("post", "/teams", {"name": "Mine"}),
        ("put", "/settings", {"default_language": "en"}),
        ("post", "/servers", {"name": "x", "host": "192.0.2.1"}),
        ("post", "/auth/change-password", {"current_password": "x", "new_password": "a-new-password-1"}),
        ("post", "/auth/register", {"email": "x@example.ir", "password": "a-new-password-1",
                                    "organization_name": "X"}),
        ("post", "/auth/login", {"email": "visitor@demo.invalid", "password": "anything-at-all"}),
        ("post", "/auth/forgot-password", {"email": "visitor@demo.invalid"}),
    ]
    for method, path, body in writes:
        r = demo.request(method, f"{V}{path}", headers=visitor, json=body)
        assert (r.status_code, r.json()) == (403, {"detail": "read_only_demo"}), (method, path)
    assert spend() == before
    assert [i["id"] for i in demo.get(f"{V}/inventories", headers=visitor).json()] == [inv]


def test_admin_pages_stay_closed_to_visitors(demo, visitor):
    for path in ("/members", "/audit-logs", "/servers/enroll-command", "/bale"):
        assert demo.get(f"{V}{path}", headers=visitor).status_code == 403, path


def test_pages_that_calculate_still_work_and_keep_nothing(demo, visitor):
    from finops_api.db import SessionLocal
    from finops_api.orm import AIRun, AuditLog, QuoteRow

    inv = demo.get(f"{V}/inventories", headers=visitor).json()[0]["id"]

    def rows():
        with SessionLocal() as db:
            return tuple(db.scalar(select(func.count()).select_from(m)) for m in (QuoteRow, AuditLog, AIRun))

    first = demo.post(f"{V}/inventories/{inv}/quote", headers=visitor, json={"include_tax": True})
    assert first.status_code == 200, first.text
    kept = rows()
    again = demo.post(f"{V}/inventories/{inv}/quote", headers=visitor, json={"include_tax": True})
    assert again.json()["id"] == first.json()["id"]  # one quote per price catalog, however many people look
    assert demo.post(f"{V}/inventories/{inv}/compare", headers=visitor, json={}).status_code == 200
    answer = demo.post(f"{V}/advisor/chat", headers=visitor,
                       json={"inventory_id": inv, "question": "How much do I pay each month?", "lang": "en"})
    assert answer.status_code == 200 and answer.json()["answer"] and answer.json()["ai_run_id"] is None
    assert rows() == kept  # a thousand visitors leave the database the size it was


def test_a_demo_never_uses_a_paid_model(monkeypatch):
    from finops_api.config import get_settings

    monkeypatch.setenv("FINOPS_DEMO_MODE", "true")
    monkeypatch.setenv("FINOPS_AI_PROVIDER", "anthropic")
    get_settings.cache_clear()
    try:
        assert get_settings().ai_provider == "offline"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_a_new_month_gets_its_sample_payments(demo, visitor, monkeypatch):
    from finops_api import demo as demo_module
    from finops_api.dates import current_month, shift_month
    from finops_api.db import SessionLocal
    from finops_api.orm import CostSnapshot, SpendEntry

    org = demo.get(f"{V}/settings", headers=visitor).json()["organization"]["id"]
    ahead = shift_month(current_month(), 1)
    monkeypatch.setattr(demo_module, "current_month", lambda: ahead)

    def payments():
        with SessionLocal() as db:
            return db.scalars(select(SpendEntry.amount_toman).where(SpendEntry.organization_id == org,
                                                                    SpendEntry.month == ahead)).all()

    assert payments() == []
    assert demo.post(f"{V}/auth/demo").status_code == 200  # the first visit of the new month
    assert sorted(payments()) == [Decimal(a) for a in (3120000, 3850000, 5320000, 11400000)]
    assert demo.post(f"{V}/auth/demo").status_code == 200
    assert len(payments()) == 4  # and only once
    with SessionLocal() as db:  # leave the shared test database as other tests expect it
        db.execute(delete(SpendEntry).where(SpendEntry.organization_id == org, SpendEntry.month == ahead))
        db.execute(delete(CostSnapshot).where(CostSnapshot.organization_id == org, CostSnapshot.month == ahead))
        db.commit()
