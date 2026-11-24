"""The catalog a release carries (data/feed, scripts/fetch_catalog.py): an installation applies it at its first start,
so it has to pass the same check an installation makes, against the key built into Sarfeh."""

from pathlib import Path

import pytest

from finops_api.config import SARFEH_CATALOG_PUBLIC_KEY
from pricing_engine import load_seed_snapshot
from provider_adapters import read_feed

FEED = Path(__file__).resolve().parents[2] / "data" / "feed"


@pytest.mark.skipif(not (FEED / "catalog.json").exists(), reason="no catalog in data/feed")
def test_the_bundled_catalog_is_signed_with_pendars_key_and_covers_every_provider():
    content = (FEED / "catalog.json").read_bytes()
    signature = (FEED / "catalog.json.sig").read_text(encoding="ascii").strip()
    _, rows = read_feed(content, signature, SARFEH_CATALOG_PUBLIC_KEY)
    assert set(rows) == set(load_seed_snapshot().providers)
