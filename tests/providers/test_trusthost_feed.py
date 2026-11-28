"""TrustHost's price feed, read from a copy saved on 2026-11-28 (tests never reach live sites)."""

import gzip
import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from provider_adapters import ParseError, RawCatalog, TrustHostAdapter, plan_sync

FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
AT = datetime(2026, 11, 28, 12, 30, tzinfo=UTC)


def feed() -> dict:
    return json.loads(gzip.open(FIXTURES / "trusthost-2026-11-28.json.gz", "rt", encoding="utf-8").read())


def raw(data: dict | str) -> RawCatalog:
    content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return RawCatalog("trusthost", TrustHostAdapter.source_url, AT, content, content_type="application/json")


def test_the_21_plans_of_the_tehran_datacenters():
    items = TrustHostAdapter().normalize_catalog(raw(feed()))
    by = {i.sku: i for i in items}
    assert len(items) == 21
    assert {i.product_family for i in items} == {"general_purpose", "compute_optimized", "memory_optimized",
                                                 "storage_optimized"}
    j = by["جرقه"]
    assert (j.cpu_cores, j.ram_gb, j.storage_gb, j.storage_type, j.included_traffic_gb, j.price_toman) == (
        1, 2, 50, "SSD", 100, D(495360))
    assert by["انبار ۴"].storage_gb == 2048 and by["فراخ ۶"].ram_gb == 128
    assert {(i.datacenter, i.billing_period, i.tax_included, i.source_price_date) for i in items} == {
        ("Fanap Tehran, Respina Tehran", "month", False, "2026-11-28")}


def test_shared_vcpus_and_one_ipv4_included():
    items = TrustHostAdapter().normalize_catalog(raw(feed()))
    assert {i.cpu_shared for i in items} == {True}
    assert {i.attributes["ipv4_included"] for i in items} == {1}
    assert items[0].attributes["datacenters_fa"] == ["فناپ تهران", "رسپینا تهران"]


def test_an_older_feed_without_these_terms_reads_as_before():
    data = feed()
    for p in data["plans"]:
        for k in ("datacenters", "cpu_type", "ipv4_included"):
            del p[k]
        p["datacenter"] = "فناپ تهران"
    items = TrustHostAdapter().normalize_catalog(raw(data))
    assert {(i.datacenter, i.cpu_shared) for i in items} == {("Fanap Tehran", None)}
    assert all("ipv4_included" not in i.attributes for i in items)


def test_traffic_is_a_quota_with_a_published_overage_rate():
    items = TrustHostAdapter().normalize_catalog(raw(feed()))
    assert {i.attributes["traffic_policy"] for i in items} == {"included_quota"}
    assert {i.attributes["overage_toman_per_gb"] for i in items} == {"900"}
    # volume rates for large purchases are kept, not applied: an overage is never priced below what a customer pays
    assert items[0].attributes["overage_volume_rates"][0] == {"min_gb": 20000, "toman_per_gb": "880"}


def test_the_catalog_holds_what_the_feed_says(snapshot):
    plan = plan_sync(TrustHostAdapter(), raw(feed()), snapshot.provider_items("trusthost"))
    assert plan.status == "ok" and plan.changes == []


@pytest.mark.parametrize(("change", "message"), [
    (lambda d: d["tax"].update(percent="12"), "VAT terms changed"),
    (lambda d: d["tax"].update(included_in_prices=True), "VAT terms changed"),
    (lambda d: d["traffic"].update(metered_direction="both"), "traffic terms changed"),
    (lambda d: d.update(traffic=None), "no traffic terms"),
    (lambda d: d["plans"][0]["price_toman"].update(monthly=None), "monthly price"),
    (lambda d: d["plans"][0].update(family="gpu"), "known family"),
    (lambda d: d["plans"][0].update(vcpu=None), "vCPU is not a number"),
    (lambda d: d.update(plans=[]), "lists no plans"),
    (lambda d: d.update(schema=2), "unknown schema"),
    (lambda d: d["plans"][0].update(cpu_type="burstable"), "unknown CPU type"),
    (lambda d: d["plans"][0].update(ipv4_included="1"), "IPv4 count is not a whole number"),
    (lambda d: d["plans"][0].update(ipv4_included=-1), "IPv4 count is not a whole number"),
])
def test_a_changed_feed_fails_rather_than_guesses(change, message):
    data = feed()
    change(data)
    with pytest.raises(ParseError, match=message):
        TrustHostAdapter().normalize_catalog(raw(data))
    assert plan_sync(TrustHostAdapter(), raw(data), []).status == "failed"


def test_an_error_page_is_not_json():
    with pytest.raises(ParseError, match="didn't return JSON"):
        TrustHostAdapter().normalize_catalog(raw("<html>503</html>"))
