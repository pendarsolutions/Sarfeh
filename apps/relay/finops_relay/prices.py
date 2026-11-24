"""Sarfeh's price service: the one signed price catalog every installation updates from.

Pendar's price reader signs a catalog after each read of the providers and sends it here (PUT /v1/prices). The
service keeps it only if the signature checks out against the public key it was given, and only if it is newer than
the one it has. Installations take a token once (POST /v1/prices/register), ask which catalog is the newest
(GET /v1/prices/latest), and download it when someone updates (GET /v1/prices/catalog.json, with the signature in
a header). They check the signature themselves as well, so neither this service nor anything between it and them can
change a price.

A token is `<install id>.<mac>`: the id with an HMAC made from a secret the mirrors share, so any mirror can check a
token another one gave out, without a shared database. Each mirror counts the installations it sees (id, version,
first and last time; no addresses) in clients.db.

    FINOPS_HUB_DATA=/app/var/prices  FINOPS_HUB_PUBLIC_KEY=<base64>  FINOPS_HUB_TOKEN_SECRET=<random, same on mirrors>
    FINOPS_HUB_REVOKED=ins_aaa,ins_bbb   (optional: tokens refused from now on)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .limits import Limiter

SCHEMA = 1
MAX_CATALOG_BYTES = 20 * 1024 * 1024
#: catalogs kept on disk: the current one, and the one before it for a download that started just before a publish
KEEP = 2

router = APIRouter(prefix="/v1/prices")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Hub:
    def __init__(self, data_dir: Path, public_key: str | None, token_secret: str | None,
                 revoked: set[str] | None = None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)) if public_key else None
        self.secret = token_secret.encode() if token_secret else None
        self.revoked = revoked or set()
        self.lock = threading.Lock()
        meta = self.dir / "meta.json"
        self.meta: dict[str, Any] | None = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else None
        self.db = sqlite3.connect(str(self.dir / "clients.db"), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, first_seen REAL NOT NULL, "
                        "last_seen REAL NOT NULL, version TEXT, checks INTEGER NOT NULL DEFAULT 0, "
                        "downloads INTEGER NOT NULL DEFAULT 0)")
        self.register_limit = Limiter(10, 3600)      # per address
        self.check_limit = Limiter(60, 3600)         # per installation: it checks about once an hour
        self.download_limit = Limiter(24, 86400)     # per installation: a catalog is new twice a day at most
        self.publish_limit = Limiter(30, 3600)       # per address

    @classmethod
    def from_env(cls) -> Hub:
        revoked = {r.strip() for r in os.environ.get("FINOPS_HUB_REVOKED", "").split(",") if r.strip()}
        # beside the relay's database unless told otherwise
        default = Path(os.environ.get("FINOPS_RELAY_DB", "/data/relay.db")).parent / "prices"
        return cls(Path(os.environ.get("FINOPS_HUB_DATA") or default),
                   os.environ.get("FINOPS_HUB_PUBLIC_KEY") or None,
                   os.environ.get("FINOPS_HUB_TOKEN_SECRET") or None, revoked)

    # ------------------------------------------------------------------ tokens

    def _mac(self, install_id: str) -> str:
        assert self.secret is not None
        digest = hmac.new(self.secret, install_id.encode(), hashlib.sha256).digest()[:24]
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def issue(self) -> tuple[str, str]:
        install_id = "ins_" + secrets.token_hex(8)
        return install_id, f"{install_id}.{self._mac(install_id)}"

    def verify(self, token: str) -> str | None:
        install_id, _, mac = token.partition(".")
        if not self.secret or not install_id.startswith("ins_") or len(install_id) > 40 or install_id in self.revoked:
            return None
        return install_id if hmac.compare_digest(mac, self._mac(install_id)) else None

    def seen(self, install_id: str, version: str | None, *, check: int = 0, download: int = 0) -> None:
        t = time.time()
        with self.lock:
            self.db.execute(
                "INSERT INTO clients (id, first_seen, last_seen, version, checks, downloads) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen, "
                "version = COALESCE(excluded.version, clients.version), checks = clients.checks + excluded.checks, "
                "downloads = clients.downloads + excluded.downloads",
                (install_id, t, t, (version or None) and version[:40], check, download))

    def active(self, days: int = 30) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM clients WHERE last_seen > ?",
                                   (time.time() - days * 86400,)).fetchone()[0]

    # ------------------------------------------------------------------ the catalog

    def publish(self, content: bytes, signature_b64: str) -> tuple[str, dict[str, Any]]:
        """Keep a catalog the price reader signed. ("stored" | "unchanged", what /latest now says)."""
        if self.key is None:
            raise HTTPException(503, "this service has no public key to check catalogs with")
        if len(content) > MAX_CATALOG_BYTES:
            raise HTTPException(413, "the catalog is too large")
        try:
            self.key.verify(base64.b64decode(signature_b64, validate=True), content)
        except (InvalidSignature, ValueError, binascii.Error) as exc:
            raise HTTPException(400, "the signature is not valid for this catalog") from exc
        data = json.loads(content)
        if data.get("schema") != SCHEMA:
            raise HTTPException(400, f"unsupported catalog schema {data.get('schema')!r}")
        generated_at = datetime.fromisoformat(data["generated_at"])
        sha = hashlib.sha256(content).hexdigest()
        with self.lock:
            cur = self.meta
            if cur and cur["sha256"] == sha:
                return "unchanged", cur
            if cur and generated_at <= datetime.fromisoformat(cur["generated_at"]):
                # a validly signed old catalog must not roll prices back
                raise HTTPException(409, f"older than the catalog this service has ({cur['generated_at']})")
            name = f"catalog-{sha[:16]}"
            (self.dir / f"{name}.json").write_bytes(content)
            (self.dir / f"{name}.sig").write_text(signature_b64, encoding="ascii")
            meta = {"generated_at": data["generated_at"], "digest": data.get("digest"), "sha256": sha,
                    "bytes": len(content), "items": len(data.get("items", [])),
                    "providers": data.get("providers") or {}, "file": name, "received_at": _now()}
            tmp = self.dir / "meta.json.tmp"
            tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.dir / "meta.json")
            self.meta = meta
            others = sorted((p for p in self.dir.glob("catalog-*.json") if p.stem != name),
                            key=lambda p: p.stat().st_mtime, reverse=True)
            for p in others[KEEP - 1:]:
                p.unlink(missing_ok=True)
                p.with_suffix(".sig").unlink(missing_ok=True)
        return "stored", meta

    def current(self) -> tuple[dict[str, Any], bytes, str]:
        meta = self.meta
        if meta is None:
            raise HTTPException(404, "no catalog has been published here yet")
        name = meta["file"]
        return meta, (self.dir / f"{name}.json").read_bytes(), (self.dir / f"{name}.sig").read_text(encoding="ascii")


def public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: meta[k] for k in ("generated_at", "digest", "sha256", "bytes", "items", "providers")}


# ---------------------------------------------------------------------- endpoints


def hub(request: Request) -> Hub:
    return request.app.state.hub


def client_ip(request: Request) -> str:
    # set by the proxy in front (nginx from CF-Connecting-IP, Caddy from the connection), never by the client
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def installation(request: Request, authorization: str = Header(default=""),
                 x_sarfeh_version: str | None = Header(default=None)) -> str:
    install_id = hub(request).verify(authorization.removeprefix("Bearer ").strip())
    if install_id is None:
        raise HTTPException(401, "a valid token is needed: POST /v1/prices/register gives one")
    request.state.version = x_sarfeh_version
    return install_id


class RegisterIn(BaseModel):
    version: str | None = Field(default=None, max_length=40)


@router.post("/register")
def register(request: Request, body: RegisterIn | None = None):
    h = hub(request)
    if h.secret is None:
        raise HTTPException(503, "this service doesn't give out tokens")
    if not h.register_limit.allow(client_ip(request)):
        raise HTTPException(429, "too many registrations from this address; try again in an hour")
    install_id, token = h.issue()
    h.seen(install_id, body.version if body else None)
    return {"install_id": install_id, "token": token}


@router.get("/latest")
def latest(request: Request, response: Response, install_id: str = Depends(installation)):
    h = hub(request)
    if not h.check_limit.allow(install_id):
        raise HTTPException(429, "checking too often; once an hour is plenty")
    h.seen(install_id, request.state.version, check=1)
    if h.meta is None:
        raise HTTPException(404, "no catalog has been published here yet")
    response.headers["Cache-Control"] = "no-store"
    return public_meta(h.meta)


@router.get("/catalog.json")
def catalog(request: Request, if_none_match: str | None = Header(default=None),
            install_id: str = Depends(installation)):
    h = hub(request)
    meta, content, signature = h.current()
    etag = f'"{meta["sha256"]}"'
    headers = {"ETag": etag, "X-Catalog-Signature": signature, "X-Catalog-Generated-At": meta["generated_at"],
               "Cache-Control": "no-store"}
    if if_none_match and if_none_match.removeprefix("W/").strip() == etag:
        return Response(status_code=304, headers=headers)
    if not h.download_limit.allow(install_id):
        raise HTTPException(429, "downloaded too often today; try again tomorrow")
    h.seen(install_id, request.state.version, download=1)
    return Response(content, media_type="application/json", headers=headers)


@router.put("")
async def publish(request: Request, x_catalog_signature: str = Header(default="")):
    """The price reader sends each new catalog here. The signature is the only credential: whoever sends it, only a
    catalog signed with Pendar's key, and newer than the one here, is kept."""
    h = hub(request)
    if not h.publish_limit.allow(client_ip(request)):
        raise HTTPException(429, "publishing too often")
    if int(request.headers.get("content-length") or 0) > MAX_CATALOG_BYTES:
        raise HTTPException(413, "the catalog is too large")
    content = await request.body()
    status, meta = h.publish(content, x_catalog_signature.strip())
    return {"status": status, **public_meta(meta)}
