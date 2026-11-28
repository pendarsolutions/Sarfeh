"""What the official pages of 18 November 2026 broke or showed, read from those pages as they were."""

import gzip
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from provider_adapters import ChabokanAdapter, IranServerAdapter, MobinHostAdapter, ParseError, RawCatalog

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
NOW = datetime(2026, 11, 18, tzinfo=UTC)


def rows(adapter):
    content = gzip.decompress((FIX / f"{adapter.provider_id}-2026-11-18.html.gz").read_bytes()).decode("utf-8")
    return {i.sku: i for i in adapter.normalize_catalog(RawCatalog(adapter.provider_id, adapter.source_url, NOW, content))}


def test_iranserver_columns_are_found_by_their_header():
    # the page moved memory ahead of CPU; read by position, NGP-large80 came out as 32 vCPU and 4 GB
    by = rows(IranServerAdapter())
    large = by["NGP-large80"]
    assert (large.cpu_cores, large.ram_gb, large.storage_gb, large.price_toman) == (D(4), D(32), D(80), D(8800638))
    assert (by["NGP-small40"].cpu_cores, by["NGP-small40"].ram_gb) == (D(1), D(4))


def test_iranserver_every_family_is_read():
    # issue #2: reading only the NGP table listed IranServer at 1 core / 2 GB by a 4-core plan; NCO-small80 fits
    by = rows(IranServerAdapter())
    assert len(by) == 21
    assert {i.product_family for i in by.values()} == {"general_purpose", "high_traffic", "memory_optimized",
                                                       "compute_optimized", "high_performance"}
    small = by["NCO-small80"]
    assert (small.cpu_cores, small.ram_gb, small.storage_gb, small.included_traffic_gb, small.price_toman) == (
        D(1), D(2), D(80), D(1000), D(2122638))
    assert small.product_family == "compute_optimized" and small.attributes["family_fa"] == "پردازنده محور"
    assert small.cpu_shared is True and small.attributes["disk_class"] == "nvme"
    assert by["NHT-small20"].included_traffic_gb == D(3000)
    assert by["NGP-large80"].product_family == "general_purpose"  # the rows read before keep their identity


def test_iranserver_unknown_family_fails_the_read():
    page = gzip.decompress((FIX / "iranserver-2026-11-18.html.gz").read_bytes()).decode("utf-8")
    a = IranServerAdapter()
    with pytest.raises(ParseError, match="NXX"):
        a.normalize_catalog(RawCatalog(a.provider_id, a.source_url, NOW, page.replace("NHT-small20", "NXX-small20")))


def test_chabokan_plan_after_a_hosting_package_is_not_lost():
    # a hosting package without a hub, just before the cheapest plan, used to swallow it
    cheap = rows(ChabokanAdapter())["cheap-tehran"]
    assert (cheap.cpu_cores, cheap.ram_gb, cheap.storage_gb, cheap.price_toman) == (D(1), D(2), D(30), D(983250))
    assert cheap.datacenter == "تهران"


def test_mobinhost_capacity_used_up_is_sold_out():
    by = rows(MobinHostAdapter())
    assert {s for s, i in by.items() if i.availability == "sold_out"} == {"IR-VPS-2GB", "IR-VDS-128GB", "IR-VDS-256GB"}
    assert by["IR-VDS-128GB"].price_toman == D(8440000)  # "8,440 میلیون" on the page means thousands
