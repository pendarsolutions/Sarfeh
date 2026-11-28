"""HostIran's price API, read from a copy saved on 2026-11-25 (tests never reach live sites)."""

import gzip
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from provider_adapters import HostIranAdapter, ParseError, RawCatalog, SourceBlockedError, plan_sync
from provider_adapters.base import make_item_id

FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
SAVED = "hostiran-2026-11-25.json.gz"
AT = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def content(name: str = SAVED) -> str:
    return gzip.open(FIXTURES / name, "rt", encoding="utf-8").read()


def raw(name: str = SAVED, data: dict | None = None) -> RawCatalog:
    text = json.dumps(data, ensure_ascii=False) if data is not None else content(name)
    return RawCatalog("hostiran", HostIranAdapter.source_url, AT, text)


def package(data: dict, name: str) -> dict:
    return next(p for p in data["packages"] if p["name"] == name)


@pytest.fixture(scope="module")
def items():
    return HostIranAdapter().normalize_catalog(raw())


def test_the_iranian_clusters_sold_at_their_listed_price(items):
    # each note starts with the node read and its package: payam and nsx-payam (a 40% discount beside their prices),
    # the cds-h hosts, foreign locations and nodes without plans are left out. The API doesn't say which datacenter
    # the iran package is in.
    assert Counter((i.notes.split(". ")[0], i.datacenter, i.attributes["disk_class"]) for i in items) == {
        ("کلاستر حرفه ای - دیسک پرسرعت (beheshti)", "Beheshti", "ssd"): 17,
        ("کلاستر حرفه ای - دیسک NVME (beheshti)", "Beheshti", "nvme"): 17,
        ("کلاستر سازمانی - دیسک پرسرعت (iran)", None, "ssd"): 17,
        ("کلاستر سازمانی - دیسک اقتصادی (iran)", None, "hybrid"): 16,
        ("کلاستر اقتصادی - دیسک پرسرعت (iran)", None, "ssd"): 6,
        ("کلاستر سازمانی پلاس - دیسک NVME (iran)", None, "nvme"): 17,
        ("کلاستر حرفه ای - دیسک NVME (mashhad)", "Mashhad", "nvme"): 17,
        ("کلاستر حرفه ای - دیسک NVME (ahwaz)", "Ahwaz", "nvme"): 17,
    }
    # the engine finds a plan by its SKU, and the page prints it as the plan's name: each says its cluster
    assert len({i.sku for i in items}) == len(items) == 124
    by = {i.sku: i for i in items}
    assert [(by[s].name, by[s].price_toman, by[s].storage_type) for s in (
        "General Purpose 4 (Enterprise)", "General Purpose 4 (Enterprise, economy disk)", "Cost-Optimized 3 (Economy)",
        "General Purpose 4 (Enterprise Plus, NVMe)", "General Purpose 4 (Beheshti, NVMe)",
        "General Purpose 4 (Mashhad, NVMe)", "General Purpose 4 (Ahwaz, NVMe)")] == [
        ("General Purpose 4", D(2874000), "SSD"), ("General Purpose 4", D(2658000), "hybrid"),
        ("Cost-Optimized 3", D(1398000), "SSD"), ("General Purpose 4", D(4290000), "NVMe"),
        ("General Purpose 4", D(5778000), "NVMe"), ("General Purpose 4", D(5778000), "NVMe"),
        ("General Purpose 4", D(5778000), "NVMe")]
    assert {i.product_family for i in items} == {"general_purpose", "memory_optimized", "cpu_optimized",
                                                  "cost_optimized"}
    assert {i.product_family for i in items} <= {f for _, f in HostIranAdapter.scope}
    assert {(i.source_price_date, i.billing_period) for i in items} == {("2026-11-11", "month")}
    # what the API doesn't state stays unset, so the engine says it can't price a plan exactly instead of guessing
    assert {(i.cpu_shared, i.included_traffic_gb) for i in items} == {(None, None)}
    assert {i.attributes["traffic_policy"] for i in items} == {"unknown"}


def test_the_plans_hostiran_first_asked_to_be_listed_with_stay_the_same_rows(items):
    """Beheshti's professional cluster with fast disks, the table HostIran sent: the same SKUs, ids and prices as
    before the other clusters were read, so the change record doesn't show them as removed and added again."""
    rows = [i for i in items if i.notes.startswith("کلاستر حرفه ای - دیسک پرسرعت (beheshti)")]
    assert len(rows) == 17 and all(i.sku == i.name for i in rows)
    by = {i.sku: i for i in rows}
    # the API is in Rial: the table HostIran sent shows a tenth, in Toman
    assert by["General Purpose 1"].price_toman == D(1338000)
    assert (by["CPU Optimized 6"].cpu_cores, by["CPU Optimized 6"].ram_gb, by["CPU Optimized 6"].storage_gb,
            by["CPU Optimized 6"].price_toman) == (64, 128, 240, D(64482000))
    assert (by["General Purpose 1"].id, by["CPU Optimized 6"].id) == ("9047849920ca7ede", "0920bef0644bf52f")
    # a storage type would change a row's identity: their disk class is an attribute instead
    assert {(i.storage_type, i.datacenter, i.attributes["disk_class"]) for i in rows} == {(None, "Beheshti", "ssd")}
    assert all(i.id == make_item_id("hostiran", "compute_vm", i.product_family, i.sku, None, None, None, None)
               for i in rows)


def test_a_generation_only_where_the_api_states_one(items):
    """The Iran location's tiers (hostiran.iran) name a cluster in their title and its compute type in meta: the
    enterprise cluster is g9, the economy cluster g8. Nothing in the API says what the professional and enterprise
    plus clusters run on, so they say nothing."""
    assert {(i.attributes["cluster"], i.attributes.get("generation")) for i in items} == {
        ("کلاستر سازمانی", "G9"), ("کلاستر اقتصادی", "G8"), ("کلاستر سازمانی پلاس", None), ("کلاستر حرفه ای", None)}
    # without the tiers no row has one, and the plans are read the same
    data = json.loads(content())
    del data["hostiran"]
    bare = HostIranAdapter().normalize_catalog(raw(data=data))
    assert all("generation" not in i.attributes for i in bare)
    assert [(i.id, i.price_toman) for i in bare] == [(i.id, i.price_toman) for i in items]


# Rial a core, a GB of memory, a GB of disk and a server, which every plan of the node follows exactly
RATES = {
    ("iran", "کلاستر سازمانی - دیسک پرسرعت"): (2280000, 1080000, 108000, 4500000),
    ("iran", "کلاستر سازمانی - دیسک اقتصادی"): (2280000, 1080000, 72000, 4500000),
    ("iran", "کلاستر اقتصادی - دیسک پرسرعت"): (1800000, 780000, 108000, 4500000),
    ("beheshti", "کلاستر حرفه ای - دیسک پرسرعت"): (3840000, 2880000, 108000, 4500000),
}


def test_what_the_api_says_the_disks_are():
    """Why fast disk is SSD and economy disk hybrid (HDD+SSD). The Iran location's tiers price one server of 1 core,
    1 GB and 20 GB in a cluster with 'FULL SSD' (all_flash) or 'HDD+SSD' (hybrid) disks. At that size, the enterprise
    cluster's fast-disk plans cost what its all-flash tier does, its economy-disk plans what its hybrid tier does, and
    the economy cluster's fast-disk plans what its all-flash tier does. Fast disk costs the same per GB in every
    cluster that has it, Beheshti's included; economy disk costs less."""
    data = json.loads(content())
    tiers = data["hostiran"]["iran"]
    for (pkg, node), (core, memory, disk, server) in RATES.items():
        plans = next(n for n in package(data, pkg)["nodes"] if n["name"] == node)["packages"]
        for p in plans:
            price = core * p["cores"] + memory * p["memory"] + disk * p["disk"] + server
            assert price == p["calculator"]["monthly_cost"], (node, p["name"])
    for node, tier, kind in [("کلاستر سازمانی - دیسک پرسرعت", "g9_all_flash", ("g9", "all_flash")),
                             ("کلاستر سازمانی - دیسک اقتصادی", "g9_hybrid", ("g9", "hybrid")),
                             ("کلاستر اقتصادی - دیسک پرسرعت", "g8_all_flash", ("g8", "all_flash"))]:
        core, memory, disk, server = RATES[("iran", node)]
        t = tiers[tier]
        assert t["title"].split(" + ")[0] == node.split(" - ")[0]
        assert (t["meta"]["compute_type"], t["meta"]["storage_type"]) == kind
        price = core * t["meta"]["core"] + memory * t["meta"]["memory"] + disk * t["meta"]["disk"] + server
        assert price == t["key"]["monthly_price"], tier


def test_the_catalog_holds_what_the_api_says(snapshot, items):
    plan = plan_sync(HostIranAdapter(), raw(), snapshot.provider_items("hostiran"))
    assert plan.status == "ok" and plan.changes == []
    # more than a sync compares: every field of every row, the cluster attributes included
    seed = {i.id: i.model_dump(exclude={"valid_from"}) for i in snapshot.provider_items("hostiran")}
    assert seed == {i.id: i.model_dump(exclude={"valid_from"}) for i in items}


def test_the_browser_check_fails_the_read_visibly():
    with pytest.raises(SourceBlockedError, match="browser check"):
        HostIranAdapter().normalize_catalog(raw("hostiran-challenge-2026-11-25.html.gz"))
    plan = plan_sync(HostIranAdapter(), raw("hostiran-challenge-2026-11-25.html.gz"), [])
    assert plan.status == "failed"


def test_a_changed_api_fails_rather_than_guesses(snapshot):
    bad = RawCatalog("hostiran", HostIranAdapter.source_url, AT, '{"packages": [{"name": "iran", "nodes": []}]}')
    with pytest.raises(ParseError, match="no plans for 8 of the clusters read: beheshti"):
        HostIranAdapter().normalize_catalog(bad)
    with pytest.raises(ParseError, match="didn't return JSON"):
        HostIranAdapter().normalize_catalog(RawCatalog("hostiran", HostIranAdapter.source_url, AT, "<html></html>"))
    # a cluster gone is named, never dropped unseen: the sync fails and the last good rows stay
    data = json.loads(content())
    package(data, "ahwaz")["nodes"] = []
    with pytest.raises(ParseError, match="no plans for 1 of the clusters read: ahwaz"):
        HostIranAdapter().normalize_catalog(raw(data=data))
    assert plan_sync(HostIranAdapter(), raw(data=data), snapshot.provider_items("hostiran")).status == "failed"
    # a discount beside a package's prices: whether they include it, the API doesn't say
    data = json.loads(content())
    package(data, "iran")["discount"] = 20
    with pytest.raises(ParseError, match="iran package has a discount"):
        HostIranAdapter().normalize_catalog(raw(data=data))
