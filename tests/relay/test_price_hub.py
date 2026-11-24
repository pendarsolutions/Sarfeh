"""Sarfeh's price service (the mirrors Pendar runs): tokens, rate limits, and keeping only catalogs Pendar signed."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provider_adapters import build_feed, generate_signing_key, read_feed, sign_feed

SECRET = "shared-by-both-mirrors"


def mirror(tmp_path, public_key, secret=SECRET):
    from finops_relay.prices import Hub, router

    app = FastAPI()
    app.include_router(router)
    app.state.hub = Hub(tmp_path, public_key, secret)
    return TestClient(app)


@pytest.fixture
def keys():
    return generate_signing_key()


def catalog(at: datetime, **read_on: str) -> bytes:
    return build_feed([], at, read_on or None)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_token_from_one_mirror_works_on_the_other_and_nowhere_else(tmp_path, keys):
    iran, outside = mirror(tmp_path / "ir", keys[1]), mirror(tmp_path / "out", keys[1])
    stranger = mirror(tmp_path / "x", keys[1], secret="another-secret")
    token = iran.post("/v1/prices/register", json={"version": "0.6.0"}).json()["token"]
    content = catalog(datetime(2026, 11, 24, tzinfo=UTC))
    assert outside.put("/v1/prices", content=content, headers={"X-Catalog-Signature": sign_feed(content, keys[0])}
                       ).status_code == 200
    assert outside.get("/v1/prices/latest", headers=bearer(token)).status_code == 200
    assert stranger.get("/v1/prices/latest", headers=bearer(token)).status_code == 401
    assert outside.get("/v1/prices/latest").status_code == 401
    assert outside.get("/v1/prices/latest", headers=bearer(token[:-2] + "xx")).status_code == 401
    # each mirror counts the installations it sees, with their version and no address
    assert iran.app.state.hub.active() == 1
    row = outside.app.state.hub.db.execute("SELECT * FROM clients").fetchone()
    assert row[0] == token.split(".")[0] and row[4] == 1


def test_only_a_newer_catalog_signed_with_pendars_key_is_kept(tmp_path, keys):
    private, public = keys
    hub = mirror(tmp_path, public)
    token = hub.post("/v1/prices/register").json()["token"]
    assert hub.get("/v1/prices/latest", headers=bearer(token)).status_code == 404  # nothing published yet

    at = datetime(2026, 11, 24, 11, 46, tzinfo=UTC)
    content = catalog(at, iranserver="2026-11-24T11:46:03+00:00")

    def put(body, sig):
        return hub.put("/v1/prices", content=body, headers={"X-Catalog-Signature": sig})

    assert put(content, sign_feed(content, generate_signing_key()[0])).status_code == 400  # someone else's key
    assert put(content + b" ", sign_feed(content, private)).status_code == 400  # changed after signing
    assert put(content, "not base64!").status_code == 400
    r = put(content, sign_feed(content, private))
    assert r.status_code == 200 and r.json()["status"] == "stored"
    assert put(content, sign_feed(content, private)).json()["status"] == "unchanged"
    older = catalog(at - timedelta(hours=12))
    assert put(older, sign_feed(older, private)).status_code == 409  # a signed old catalog can't roll prices back

    latest = hub.get("/v1/prices/latest", headers=bearer(token)).json()
    assert latest["generated_at"] == at.isoformat()
    assert latest["providers"] == {} and latest["digest"]

    # the download carries the signature, and an installation can check it against the key it has built in
    got = hub.get("/v1/prices/catalog.json", headers=bearer(token))
    assert got.status_code == 200
    assert read_feed(got.content, got.headers["X-Catalog-Signature"], public)[0] == at
    again = hub.get("/v1/prices/catalog.json", headers={**bearer(token), "If-None-Match": got.headers["ETag"]})
    assert again.status_code == 304

    # a newer catalog replaces it, and only the last two are kept on disk
    for hours in (1, 2, 3):
        newer = catalog(at + timedelta(hours=hours))
        assert put(newer, sign_feed(newer, private)).json()["status"] == "stored"
    assert len(list(tmp_path.glob("catalog-*.json"))) == 2
    # and it survives a restart
    assert mirror(tmp_path, public).app.state.hub.meta["generated_at"] == (at + timedelta(hours=3)).isoformat()


def test_rate_limits(tmp_path, keys):
    hub = mirror(tmp_path, keys[1])
    codes = [hub.post("/v1/prices/register").status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    # an address of its own, set by the proxy in front, gets its own allowance
    assert hub.post("/v1/prices/register", headers={"X-Real-IP": "5.6.7.8"}).status_code == 200
    token = hub.post("/v1/prices/register", headers={"X-Real-IP": "5.6.7.9"}).json()["token"]
    content = catalog(datetime(2026, 11, 24, tzinfo=UTC))
    hub.put("/v1/prices", content=content, headers={"X-Catalog-Signature": sign_feed(content, keys[0])})
    checks = [hub.get("/v1/prices/latest", headers=bearer(token)).status_code for _ in range(61)]
    assert checks[-1] == 429 and set(checks[:60]) == {200}
    downloads = [hub.get("/v1/prices/catalog.json", headers=bearer(token)).status_code for _ in range(25)]
    assert downloads[-1] == 429 and set(downloads[:24]) == {200}


def test_without_keys_it_refuses_rather_than_guesses(tmp_path):
    hub = mirror(tmp_path, None, secret=None)
    assert hub.post("/v1/prices/register").status_code == 503
    content = catalog(datetime(2026, 11, 24, tzinfo=UTC))
    assert hub.put("/v1/prices", content=content, headers={"X-Catalog-Signature": base64.b64encode(b"x").decode()}
                   ).status_code == 503


def test_the_whole_service_starts_without_a_bale_bot(tmp_path, monkeypatch, keys):
    monkeypatch.setenv("FINOPS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("FINOPS_HUB_DATA", str(tmp_path / "prices"))
    monkeypatch.setenv("FINOPS_HUB_PUBLIC_KEY", keys[1])
    monkeypatch.setenv("FINOPS_HUB_TOKEN_SECRET", SECRET)
    monkeypatch.delenv("FINOPS_RELAY_BOT_TOKEN", raising=False)
    from finops_relay import main

    with TestClient(main.app) as c:
        assert c.get("/health").json() == {"status": "ok", "bot": False, "bot_username": None, "prices": None}
        assert c.post("/v1/prices/register").status_code == 200
        content = catalog(datetime(2026, 11, 24, tzinfo=UTC))
        c.put("/v1/prices", content=content, headers={"X-Catalog-Signature": sign_feed(content, keys[0])})
        assert c.get("/health").json()["prices"] == "2026-11-24T00:00:00+00:00"
        assert json.loads((tmp_path / "prices" / "meta.json").read_text())["items"] == 0
