"""Signed price catalog: one reader (Pendar's, for Sarfeh's price service, or an installation of your own) reads the
providers, signs and publishes; installations download and verify.

Signing is Ed25519 over the exact file bytes, so a mirror or a compromised host can't change a price. Applying a
feed still goes through the normal versioning.

Besides the rows, a feed says what is in it: a digest of all rows and one per provider, each leaving out the read date
(which changes on every read), so an installation can tell a catalog with new prices from one that only says the same
prices were read again; and, per provider, when its prices were last read.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from pricing_engine.models import CatalogItem

from .base import AdapterHealth, ParseError, RawCatalog

SCHEMA = 1
EXCLUDED = {"price_rial", "version_id", "valid_from", "valid_to"}
#: changes on every read even when nothing else about the row did, so it's left out of the digests
VOLATILE = {"source_retrieved_at"}


class FeedError(ValueError):
    """The feed is unsigned, tampered with or not understood."""


def generate_signing_key() -> tuple[str, str]:
    """(private key as base64, public key as base64)."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def content_digest(rows: list[dict[str, Any]]) -> str:
    """The same rows at the same prices give the same digest, whenever they were read."""
    kept = [{k: v for k, v in r.items() if k not in VOLATILE} for r in rows]
    return hashlib.sha256(json.dumps(kept, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_feed(items: list[CatalogItem], generated_at: datetime, read_on: dict[str, str] | None = None) -> bytes:
    """`read_on`: per provider, when its prices were last read (providers without one were never read live)."""
    rows = [i.model_dump(mode="json", exclude=EXCLUDED) for i in sorted(items, key=lambda i: i.id)]
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_provider.setdefault(r["provider_id"], []).append(r)
    providers = {pid: {"digest": content_digest(rs), "items": len(rs),
                       **({"read_on": read_on[pid]} if read_on and pid in read_on else {})}
                 for pid, rs in sorted(by_provider.items())}
    return json.dumps({"schema": SCHEMA, "generated_at": generated_at.isoformat(), "digest": content_digest(rows),
                       "providers": providers, "items": rows}, ensure_ascii=False, sort_keys=True).encode()


def feed_info(content: bytes) -> dict[str, Any]:
    """What a feed says about itself, read without checking the signature: use it only on a feed already checked
    (or to show what a file claims)."""
    data = json.loads(content)
    return {"generated_at": data.get("generated_at"), "digest": data.get("digest"),
            "providers": data.get("providers") or {}, "items": len(data.get("items", []))}


def sign_feed(content: bytes, private_key_b64: str) -> str:
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(key.sign(content)).decode()


def read_feed(content: bytes, signature_b64: str, public_key_b64: str) -> tuple[datetime, dict[str, list[dict[str, Any]]]]:
    """Verify first and parse afterwards. Returns (generated_at, rows per provider)."""
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), content)
    except (InvalidSignature, ValueError) as exc:
        raise FeedError("catalog signature is not valid") from exc
    data = json.loads(content)
    if data.get("schema") != SCHEMA:
        raise FeedError(f"unsupported catalog schema {data.get('schema')!r}")
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for row in data["items"]:
        by_provider.setdefault(row["provider_id"], []).append(row)
    return datetime.fromisoformat(data["generated_at"]), by_provider


class FeedAdapter:
    """Presents one provider's verified feed rows to the normal sync pipeline."""

    key_skus: set[str] = set()
    source_url = "catalog-feed"

    def __init__(self, provider_id: str, current: list[CatalogItem], rows: list[dict[str, Any]]):
        self.provider_id = provider_id
        self.rows = rows
        self.scope = {(i.service_type, i.product_family) for i in current} | {
            (r["service_type"], r.get("product_family")) for r in rows}

    async def fetch_catalog(self) -> RawCatalog:
        raise ParseError("feed rows are already downloaded")

    async def healthcheck(self) -> AdapterHealth:
        return AdapterHealth(self.provider_id, True, "catalog feed")

    def normalize_catalog(self, raw: RawCatalog) -> list[CatalogItem]:
        return [CatalogItem.model_validate({**r, "valid_from": raw.fetched_at}) for r in self.rows]
