"""Three things the official pages of 18 November 2026 broke, read from those pages as they were."""

import gzip
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from provider_adapters import ChabokanAdapter, IranServerAdapter, MobinHostAdapter, RawCatalog

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


def test_chabokan_plan_after_a_hosting_package_is_not_lost():
    # a hosting package without a hub, just before the cheapest plan, used to swallow it
    cheap = rows(ChabokanAdapter())["cheap-tehran"]
    assert (cheap.cpu_cores, cheap.ram_gb, cheap.storage_gb, cheap.price_toman) == (D(1), D(2), D(30), D(983250))
    assert cheap.datacenter == "تهران"


def test_mobinhost_capacity_used_up_is_sold_out():
    by = rows(MobinHostAdapter())
    assert {s for s, i in by.items() if i.availability == "sold_out"} == {"IR-VPS-2GB", "IR-VDS-128GB", "IR-VDS-256GB"}
    assert by["IR-VDS-128GB"].price_toman == D(8440000)  # "8,440 میلیون" on the page means thousands
