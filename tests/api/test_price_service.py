"""An installation and Sarfeh's price service: told once about new prices, updated when an admin asks, quietly kept
current when the same prices are read again, and never given a price that Pendar's key didn't sign."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provider_adapters import FeedError, build_feed, generate_signing_key, sign_feed

BASE = datetime.now(UTC) + timedelta(days=10)  # newer than any catalog another test applied
_hours = itertools.count(1)


def _at() -> datetime:
    return BASE + timedelta(hours=next(_hours))


def _mirror(path, public_key):
    from finops_relay.prices import Hub, router

    app = FastAPI()
    app.include_router(router)
    app.state.hub = Hub(path, public_key, "shared-secret")
    return TestClient(app)


def _items(db):
    from finops_api.catalog_store import current_versions, row_to_item

    return [row_to_item(r) for r in current_versions(db)]


def _prices(db, provider_id="iranserver"):
    return {i.sku: i.price_toman for i in _items(db) if i.provider_id == provider_id}


@pytest.fixture
def service(api_env, monkeypatch, tmp_path):
    """Two mirrors (ir.test, out.test) reached through stand-ins for httpx; a list of what was announced."""
    from finops_api import price_service
    from finops_api.catalog_feed import apply_feed
    from finops_api.config import get_settings
    from finops_api.db import SessionLocal
    from finops_api.orm import PriceServiceState

    private, public = generate_signing_key()
    s = get_settings()
    monkeypatch.setattr(s, "price_source", "sarfeh")
    monkeypatch.setattr(s, "catalog_url", None)
    monkeypatch.setattr(s, "price_service_urls", "https://ir.test,https://out.test")
    monkeypatch.setattr(s, "catalog_public_key", public)
    monkeypatch.setattr(s, "price_auto_update", False)
    svc = SimpleNamespace(private=private, public=public, down=set(), sent=[],
                          mirrors={"ir.test": _mirror(tmp_path / "ir", public),
                                   "out.test": _mirror(tmp_path / "out", public)})

    def route(method):
        def call(url, **kw):
            u = urlsplit(url)
            if u.hostname in svc.down:
                raise httpx.ConnectError("unreachable")
            kw.pop("timeout", None)
            return svc.mirrors[u.hostname].request(method, u.path, **kw)
        return call

    monkeypatch.setattr(httpx, "get", route("GET"))
    monkeypatch.setattr(httpx, "post", route("POST"))
    monkeypatch.setattr(price_service, "notify", lambda subject, body, org_id=None: svc.sent.append(body))

    def publish(change=None, *, key=None, only=None):
        with SessionLocal() as db:
            items = _items(db)
        for i in items:
            if change and i.provider_id == "iranserver" and i.price_toman:
                i.price_toman = change(i.price_toman)
        at = _at()
        content = build_feed(items, at, {"iranserver": at.isoformat()})
        sig = sign_feed(content, key or private)
        for name, m in svc.mirrors.items():
            if only is None or name == only:
                assert m.put("/v1/prices", content=content, headers={"X-Catalog-Signature": sig}).status_code == 200
        return at

    svc.publish = publish
    started = datetime.now(UTC)
    with SessionLocal() as db:
        db.query(PriceServiceState).delete()
        original = _items(db)
        db.commit()
    yield svc
    # put the seed's prices back for the tests after these, and forget these runs: their catalogs are dated ahead,
    # and would make any catalog a later test applies look old
    from finops_api.orm import PriceSyncRun

    with SessionLocal() as db:
        content = build_feed(original, _at())
        apply_feed(db, content, sign_feed(content, private), public)
        db.query(PriceSyncRun).filter(PriceSyncRun.started_at >= started).delete()
        db.query(PriceServiceState).delete()
        db.commit()


def test_new_prices_are_announced_once_and_applied_when_an_admin_asks(service, client, auth):
    from finops_api.db import SessionLocal
    from finops_api.orm import PriceServiceState
    from finops_api.price_service import check, update

    service.publish()
    with SessionLocal() as db:
        st = check(db)  # the first look takes a token
        assert st["registered"] and st["update_available"] and st["mirror"] == "ir.test"
        update(db, by="test")
        before = _prices(db)
    assert len(service.sent) == 1

    service.publish(lambda p: p * D("1.1"))
    with SessionLocal() as db:
        st = check(db)
        assert st["update_available"] and st["changed"] == ["iranserver"]
        check(db)
        assert _prices(db) == before  # nothing changes until someone updates
    assert len(service.sent) == 2 and "IranServer" in service.sent[1] and "sudo sarfeh prices update" in service.sent[1]

    r = client.get("/api/v1/prices/service", headers=auth)
    assert r.status_code == 200 and r.json()["update_available"] is True
    r = client.post("/api/v1/admin/prices/update", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["iranserver"] and r.json()["status"]["update_available"] is False
    with SessionLocal() as db:
        assert _prices(db) == {sku: p * D("1.1") for sku, p in before.items()}
        assert db.get(PriceServiceState, 1).applied["by"] == "owner@example.ir"
        assert check(db)["update_available"] is False
    assert len(service.sent) == 2


def test_the_same_prices_read_again_are_applied_quietly(service):
    from finops_api.db import SessionLocal
    from finops_api.price_service import check, update

    service.publish()
    with SessionLocal() as db:
        check(db)
        update(db)
        at = service.publish()
        st = check(db)
        assert st["update_available"] is False
        assert st["applied"]["by"] == "confirmation" and st["applied"]["generated_at"] == at.isoformat()
        assert st["latest"]["read_on"] == at.isoformat()
    assert len(service.sent) == 1  # only the first look, when nothing was applied yet


def test_a_mirror_that_is_down_is_passed_over(service):
    from finops_api.db import SessionLocal
    from finops_api.price_service import check

    service.publish()
    service.down.add("ir.test")
    with SessionLocal() as db:
        st = check(db)
        assert st["mirror"] == "out.test" and st["error"] is None
        service.down.add("out.test")
        st = check(db)
        assert "ir.test: ConnectError" in st["error"] and "out.test: ConnectError" in st["error"]
        assert st["latest"] is not None  # what it knew stays


def test_a_mirror_cannot_change_a_price(service, tmp_path):
    from finops_api.db import SessionLocal
    from finops_api.price_service import check, update

    attacker_private, attacker_public = generate_signing_key()
    service.mirrors["ir.test"] = _mirror(tmp_path / "bad", attacker_public)
    service.publish(lambda p: p / 2, key=attacker_private, only="ir.test")
    with SessionLocal() as db:
        before = _prices(db)
        check(db)
        with pytest.raises(FeedError):
            update(db)
        assert _prices(db) == before


def test_a_big_move_is_applied_as_reviewed_by_the_publisher(service):
    from sqlalchemy import select

    from finops_api.catalog_feed import REVIEWED
    from finops_api.db import SessionLocal
    from finops_api.orm import PriceSyncRun
    from finops_api.price_service import update

    with SessionLocal() as db:
        before = _prices(db)
    service.publish(lambda p: p * 2)
    with SessionLocal() as db:
        update(db)
        assert _prices(db) == {sku: p * 2 for sku, p in before.items()}
        run = db.scalars(select(PriceSyncRun).where(PriceSyncRun.provider_id == "iranserver")
                         .order_by(PriceSyncRun.started_at.desc())).first()
        assert run.status == "applied_after_review" and run.summary["approved_by"] == REVIEWED


def test_the_catalog_that_came_with_the_release(service, tmp_path):
    from finops_api.db import SessionLocal
    from finops_api.price_service import apply_bundled

    with SessionLocal() as db:
        assert apply_bundled(db, tmp_path) == "no catalog came with this release"
        items = _items(db)
    content = build_feed(items, _at())
    (tmp_path / "catalog.json").write_bytes(content)
    (tmp_path / "catalog.json.sig").write_text(sign_feed(content, service.private))
    with SessionLocal() as db:
        assert apply_bundled(db, tmp_path).startswith("applied the catalog of")
        assert apply_bundled(db, tmp_path) == "a newer catalog is applied already"
        from finops_api.price_service import status

        assert status(db)["applied"]["by"] == "release"


def test_a_token_the_service_forgot_is_replaced(service):
    from finops_api.db import SessionLocal
    from finops_api.orm import PriceServiceState
    from finops_api.price_service import check, state_row

    service.publish()
    with SessionLocal() as db:
        state_row(db).token = "ins_0000000000000000.forgotten"
        db.commit()
        assert check(db)["error"] is None
        assert db.get(PriceServiceState, 1).token != "ins_0000000000000000.forgotten"


def test_auto_update_and_scraping_installations(service, monkeypatch):
    from finops_api.config import get_settings
    from finops_api.db import SessionLocal
    from finops_api.price_service import check, status

    monkeypatch.setattr(get_settings(), "price_auto_update", True)
    service.publish(lambda p: p + 1000)
    with SessionLocal() as db:
        st = check(db)
        assert st["update_available"] is False and st["applied"]["by"] == "automatic"
    assert service.sent == []
    monkeypatch.setattr(get_settings(), "price_source", "scrape")
    with SessionLocal() as db:
        assert status(db) == {"source": "scrape", "auto_update": True, "update_available": False}
