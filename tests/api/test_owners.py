"""Owners and budgets: every toman has a team (or is shown as unallocated), and teams hear before a budget runs out."""

import itertools
from decimal import Decimal

import pytest

from finops_api.dates import current_month, shift_month

_n = itertools.count()
V = "/api/v1"


@pytest.fixture
def org(client):
    r = client.post(f"{V}/auth/register", json={"email": f"owner{next(_n)}@example.ir", "password": "owner-password-1",
                                                "name": "Owner", "organization_name": "Owners"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def sent(monkeypatch):
    """Alerts that would have gone out by email or Bale."""
    out = []
    monkeypatch.setattr("finops_api.alerts.notify", lambda subject, body, org_id=None: out.append((subject, body)))
    return out


def _team(client, h, name):
    r = client.post(f"{V}/teams", headers=h, json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _member(client, h, role):
    email = f"{role}{next(_n)}@example.ir"
    assert client.post(f"{V}/members", headers=h, json={"email": email, "password": "member-password-1", "name": role,
                                                        "role": role}).status_code == 200
    tok = client.post(f"{V}/auth/login", json={"email": email, "password": "member-password-1"}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _confirmed_demo(client, h):
    demo = client.post(f"{V}/inventories/demo", headers=h).json()
    assert client.post(f"{V}/inventories/{demo['id']}/confirm", headers=h,
                       json={"confirm_all_rows": True}).status_code == 200
    return demo


def _owners(client, h, demo, owners):
    by_name = {r["name"]: r["id"] for r in demo["resources"]}
    for name, team in owners.items():
        r = client.put(f"{V}/inventories/{demo['id']}/resources/{by_name[name]}/team", headers=h, json={"team_id": team})
        assert r.status_code == 200, r.text
    return r.json()


def _this_month(client, h):
    return client.get(f"{V}/spend", headers=h).json()["months"][-1]


def test_teams_are_named_once_and_managed_by_admins(client, org):
    product = _team(client, org, "Product")
    assert client.post(f"{V}/teams", headers=org, json={"name": "Product"}).status_code == 409
    assert client.patch(f"{V}/teams/{product}", headers=org, json={"name": "Checkout"}).json()["name"] == "Checkout"
    analyst = _member(client, org, "analyst")
    assert client.post(f"{V}/teams", headers=analyst, json={"name": "Mine"}).status_code == 403
    assert [t["name"] for t in client.get(f"{V}/teams", headers=analyst).json()] == ["Checkout"]


def test_owning_a_resource_keeps_the_inventory_confirmed(client, org):
    demo = _confirmed_demo(client, org)
    view = _owners(client, org, demo, {"web-prod": _team(client, org, "Product")})
    assert view["status"] == "confirmed"
    assert next(r for r in view["resources"] if r["name"] == "web-prod")["team_id"]
    other = client.post(f"{V}/auth/register", json={"email": f"x{next(_n)}@example.ir", "password": "other-pass-123",
                                                    "name": "X", "organization_name": "X"}).json()
    foreign = client.post(f"{V}/teams", headers={"Authorization": f"Bearer {other['access_token']}"},
                          json={"name": "Theirs"}).json()["id"]
    rid = demo["resources"][0]["id"]
    assert client.put(f"{V}/inventories/{demo['id']}/resources/{rid}/team", headers=org,
                      json={"team_id": foreign}).status_code == 404


def test_payments_are_shown_back_to_teams_and_the_rest_is_unallocated(client, org):
    product, platform = _team(client, org, "Product"), _team(client, org, "Platform")
    demo = _confirmed_demo(client, org)
    # ArvanCloud's two resources belong to different teams; Liara's staging app to nobody
    _owners(client, org, demo, {"web-prod": product, "db-prod": product, "backups": platform, "ci-runner": platform})
    r = client.post(f"{V}/spend/entries", headers=org, json={"month": current_month(), "vendor": "Hetzner",
                                                             "amount": "650000", "team_id": platform})
    assert r.status_code == 200 and r.json()["team_id"] == platform

    m = _this_month(client, org)
    rows = {t["name"]: t for t in m["by_team"]}
    # every toman is on exactly one line
    assert sum(Decimal(t["paid_toman"]) for t in m["by_team"]) == Decimal(m["paid_toman"]) == Decimal("24340000")
    # ArvanCloud's 11,400,000 is shared 6,490,600 : 2,385,000, as the two teams' parts of its estimate
    arvan = Decimal(11_400_000)
    assert Decimal(rows["Product"]["shared_toman"]) == Decimal(3_850_000) + (arvan * Decimal(6_490_600) / Decimal(8_875_600)).to_integral_value(rounding="ROUND_DOWN")
    assert Decimal(rows["Platform"]["direct_toman"]) == Decimal(650_000)
    # Liara's staging app has no team, so its payment stays unallocated, shown as its own line
    unallocated = rows[None]
    assert Decimal(unallocated["paid_toman"]) >= Decimal(3_120_000) and unallocated["team_id"] is None
    assert Decimal(rows["Product"]["estimated_toman"]) == Decimal(3_850_000 + 6_490_600)


def test_deleting_a_team_leaves_its_costs_unallocated(client, org):
    team = _team(client, org, "Temp")
    demo = _confirmed_demo(client, org)
    _owners(client, org, demo, {"web-prod": team})
    client.put(f"{V}/budgets", headers=org, json={"team_id": team, "monthly_toman": "1000000"})
    assert client.delete(f"{V}/teams/{team}", headers=org).status_code == 200
    view = client.get(f"{V}/inventories/{demo['id']}", headers=org).json()
    assert all(r["team_id"] is None for r in view["resources"])
    assert client.get(f"{V}/budgets", headers=org).json()["budgets"] == []


def test_budgets_show_month_to_date_and_a_projection(client, org):
    team = _team(client, org, "Platform")
    demo = _confirmed_demo(client, org)
    _owners(client, org, demo, {"ci-runner": team})
    assert client.put(f"{V}/budgets", headers=org, json={"monthly_toman": "28000000"}).status_code == 200
    status = client.put(f"{V}/budgets", headers=org, json={"team_id": team, "monthly_toman": "6000000"}).json()
    whole, platform = status["budgets"]
    assert whole["scope"] == "organization" and whole["paid_toman"] == "23690000"
    assert whole["used_percent"] == "84.6" and whole["level"] == 80 and not whole["projected_over"]
    # Iranserver's 5,320,000 this month is all Platform's (its only resource there is the CI runner)
    assert platform["name"] == "Platform" and platform["paid_toman"] == "5320000" and platform["level"] == 80
    # projected: the largest of paid so far, the estimate (5,323,839) and last month (5,320,000)
    assert platform["projected_toman"] == "5323839"
    assert client.put(f"{V}/budgets", headers=org, json={"monthly_toman": None}).json()["budgets"][0]["scope"] == team
    viewer = _member(client, org, "viewer")
    assert client.get(f"{V}/budgets", headers=viewer).status_code == 200
    assert client.put(f"{V}/budgets", headers=_member(client, org, "analyst"),
                      json={"monthly_toman": "1"}).status_code == 403


def test_budget_alerts_go_out_at_80_and_100_percent_once_a_month(client, org, sent, api_env):
    from finops_api.db import SessionLocal
    from finops_api.spend import budget_alerts

    client.post(f"{V}/inventories/demo", headers=org)  # 23,690,000 paid this month
    client.put(f"{V}/budgets", headers=org, json={"monthly_toman": "28000000"})  # 84.6%
    with SessionLocal() as db:
        budget_alerts(db)
        budget_alerts(db)  # the next hour: already told
    mine = [s for s in sent if "84.6" in s[0] or "۸۴٫۶" in s[0]]
    assert len(mine) == 1 and "۲۳٬۶۹۰٬۰۰۰ تومان" in mine[0][1]  # Persian, the organization's default language
    client.post(f"{V}/spend/entries", headers=org, json={"month": current_month(), "provider_id": "liara",
                                                          "amount": "5000000"})
    with SessionLocal() as db:
        budget_alerts(db)
    over = [s for s in sent if "۱۰۲٫۵٪" in s[0]]
    assert len(over) == 1  # 28,690,000 of 28,000,000: 102.5%


def test_unusual_changes_alert_once_for_last_month(client, org, sent):
    from finops_api.db import SessionLocal
    from finops_api.spend import unusual_change_alerts

    client.put(f"{V}/settings", headers=org, json={"default_language": "en"})
    last, before = shift_month(current_month(), -1), shift_month(current_month(), -2)
    for month, liara, arvan in ((before, "2000000", "5000000"), (last, "3500000", "5200000")):
        client.post(f"{V}/spend/entries", headers=org, json={"month": month, "provider_id": "liara", "amount": liara})
        client.post(f"{V}/spend/entries", headers=org, json={"month": month, "provider_id": "arvancloud", "amount": arvan})
    m = next(x for x in client.get(f"{V}/spend", headers=org).json()["months"] if x["month"] == last)
    assert [(x["kind"], x["key"], x["change_percent"]) for x in m["movers"]] == [("provider", "liara", "75")]
    with SessionLocal() as db:
        unusual_change_alerts(db)
        unusual_change_alerts(db)
    mine = [s for s in sent if s[0].startswith("Unusual spend changes")]
    assert len(mine) == 1 and "Liara: 3,500,000 Toman, 75.0% up" in mine[0][1] and "ArvanCloud" not in mine[0][1]


def test_estimates_come_from_the_setup_confirmed_last_or_the_one_chosen(client, org):
    sample = _confirmed_demo(client, org)
    import json
    from pathlib import Path

    seed = json.loads((Path(__file__).parents[2] / "data/seeds/sample_inventory.json").read_text(encoding="utf-8"))
    app = next(r for r in seed["resources"] if r["name"] == "staging-app")
    built = client.post(f"{V}/inventories", headers=org, json={"name": "Ours", "resources": [app]}).json()
    assert client.post(f"{V}/inventories/{built['id']}/confirm", headers=org,
                       json={"confirm_all_rows": True}).status_code == 200
    # a real setup is preferred to the sample, without anyone choosing
    assert client.get(f"{V}/spend", headers=org).json()["inventory"]["id"] == built["id"]
    assert client.put(f"{V}/settings", headers=org, json={"estimate_inventory_id": sample["id"]}).status_code == 200
    assert client.get(f"{V}/spend", headers=org).json()["inventory"]["id"] == sample["id"]
    assert client.put(f"{V}/settings", headers=org, json={"estimate_inventory_id": "nope"}).status_code == 422


def test_a_spreadsheet_can_name_the_team(client, org):
    _team(client, org, "Data")
    ok = "month,provider,amount,team\n1405-07,liara,100000,data\n"
    r = client.post(f"{V}/spend/import", headers=org, files={"file": ("t.csv", ok.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    bad = "month,provider,amount,team\n1405-07,liara,100000,Nobody\n"
    r = client.post(f"{V}/spend/import", headers=org, files={"file": ("t.csv", bad.encode(), "text/csv")})
    assert r.status_code == 422 and "no team named" in r.json()["detail"]["errors"][0]["message"]
