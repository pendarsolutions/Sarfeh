"""HostIran's price API, read from a copy saved on 2026-11-25 (tests never reach live sites)."""

import gzip
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from provider_adapters import HostIranAdapter, ParseError, RawCatalog, SourceBlockedError, plan_sync

FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
AT = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def raw(name: str) -> RawCatalog:
    content = gzip.open(FIXTURES / name, "rt", encoding="utf-8").read()
    return RawCatalog("hostiran", HostIranAdapter.source_url, AT, content)


def test_the_plans_hostiran_asked_to_be_listed_with_in_toman():
    items = HostIranAdapter().normalize_catalog(raw("hostiran-2026-11-25.json.gz"))
    by = {i.sku: i for i in items}
    assert len(items) == 17
    # the API is in Rial: the table HostIran sent shows a tenth, in Toman
    assert by["General Purpose 1"].price_toman == D(1338000)
    assert (by["CPU Optimized 6"].cpu_cores, by["CPU Optimized 6"].ram_gb, by["CPU Optimized 6"].storage_gb,
            by["CPU Optimized 6"].price_toman) == (64, 128, 240, D(64482000))
    assert {i.product_family for i in items} == {"general_purpose", "memory_optimized", "cpu_optimized"}
    assert {(i.datacenter, i.source_price_date, i.billing_period) for i in items} == {("Beheshti", "2026-11-11", "month")}
    # what the API doesn't state stays unset, so the engine says it can't price a plan exactly instead of guessing
    assert {(i.cpu_shared, i.storage_type, i.included_traffic_gb) for i in items} == {(None, None, None)}
    assert {i.attributes["traffic_policy"] for i in items} == {"unknown"}


def test_the_catalog_holds_what_the_api_says(snapshot):
    a = HostIranAdapter()
    plan = plan_sync(a, raw("hostiran-2026-11-25.json.gz"), snapshot.provider_items("hostiran"))
    assert plan.status == "ok" and plan.changes == []


def test_the_browser_check_fails_the_read_visibly():
    with pytest.raises(SourceBlockedError, match="browser check"):
        HostIranAdapter().normalize_catalog(raw("hostiran-challenge-2026-11-25.html.gz"))
    plan = plan_sync(HostIranAdapter(), raw("hostiran-challenge-2026-11-25.html.gz"), [])
    assert plan.status == "failed"


def test_a_changed_api_fails_rather_than_guesses():
    bad = RawCatalog("hostiran", HostIranAdapter.source_url, AT, '{"packages": [{"name": "iran", "nodes": []}]}')
    with pytest.raises(ParseError, match="no beheshti node"):
        HostIranAdapter().normalize_catalog(bad)
    with pytest.raises(ParseError, match="didn't return JSON"):
        HostIranAdapter().normalize_catalog(RawCatalog("hostiran", HostIranAdapter.source_url, AT, "<html></html>"))
