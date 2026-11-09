"""Deleting an inventory, and the sample workspace: what people see, and how they start over."""

import itertools

import pytest

_n = itertools.count()


@pytest.fixture
def org(client):
    """A fresh organization of its own, so its settings don't touch other tests'."""
    email = f"deleter{next(_n)}@example.ir"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "owner-password-1", "name": "Owner",
                                                   "organization_name": "Deleters"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _ids(client, h):
    return [i["id"] for i in client.get("/api/v1/inventories", headers=h).json()]


def test_the_sample_is_named_in_the_readers_language_and_explains_nothing_in_english(client, org):
    fa = client.post("/api/v1/inventories/demo?lang=fa", headers=org).json()
    en = client.post("/api/v1/inventories/demo?lang=en", headers=org).json()
    assert fa["name"] == "استارتاپ نمونه، تهران" and en["name"] == "Sample startup, Tehran"
    # the developer's note about the seed file stays in the seed file
    assert fa["source_meta"] == {"demo": True}


def test_deleting_the_sample_starts_over(client, org):
    demo = client.post("/api/v1/inventories/demo?lang=fa", headers=org).json()
    settings = client.get("/api/v1/settings", headers=org).json()["settings"]
    assert settings["engineer_day_rate_toman"] == "4000000" and settings["engineer_day_rate_label"]

    r = client.delete(f"/api/v1/inventories/{demo['id']}", headers=org)
    assert r.status_code == 200, r.text
    assert demo["id"] not in _ids(client, org)
    # kept for the audit trail and past reports, out of every list
    assert client.get(f"/api/v1/inventories/{demo['id']}", headers=org).json()["status"] == "archived"
    # the sample day rate went with it, so the next analysis doesn't use a made-up figure
    settings = client.get("/api/v1/settings", headers=org).json()["settings"]
    assert "engineer_day_rate_toman" not in settings and "engineer_day_rate_label" not in settings


def test_deleting_the_sample_keeps_a_day_rate_someone_entered(client, org):
    demo = client.post("/api/v1/inventories/demo", headers=org).json()
    assert client.put("/api/v1/settings", headers=org, json={"engineer_day_rate_toman": "5500000"}).status_code == 200
    client.delete(f"/api/v1/inventories/{demo['id']}", headers=org)
    assert client.get("/api/v1/settings", headers=org).json()["settings"]["engineer_day_rate_toman"] == "5500000"


def test_only_members_who_can_edit_may_delete_and_only_their_own(client, org, auth):
    demo = client.post("/api/v1/inventories/demo", headers=org).json()
    # another organization can't see it, let alone delete it
    assert client.delete(f"/api/v1/inventories/{demo['id']}", headers=auth).status_code == 404
    # a viewer in the same organization can't delete
    email = f"viewer{next(_n)}@example.ir"
    assert client.post("/api/v1/members", headers=org, json={"email": email, "password": "viewer-password-1",
                                                            "role": "viewer"}).status_code == 200
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "viewer-password-1"}).json()["access_token"]
    viewer = {"Authorization": f"Bearer {token}"}
    assert client.delete(f"/api/v1/inventories/{demo['id']}", headers=viewer).status_code == 403
    assert demo["id"] in _ids(client, org)


def test_a_price_update_the_worker_cant_take_is_a_clear_503(client, auth, monkeypatch):
    from finops_api.config import get_settings
    from finops_worker import tasks

    def queue_down(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'packaging'")

    monkeypatch.setattr(get_settings(), "sync_mode", "celery")
    monkeypatch.setattr(tasks.sync_provider, "delay", queue_down)
    r = client.post("/api/v1/admin/providers/liara/sync", headers=auth)
    assert r.status_code == 503
    assert "finops-agent doctor" in r.json()["detail"]
