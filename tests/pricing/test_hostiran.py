"""HostIran's plans, from its price API: its Iranian clusters (tests/providers/test_hostiran_api.py reads them).

The Beheshti professional cluster with fast disks, the one HostIran first asked to be listed with (the same numbers
as the table it sent on 2026-11-25), follows one formula: 384,000 Toman a core, 288,000 a GB of memory, 10,800 a GB
of disk and 450,000 a server. Every row of it fitting is a check that the API was read right."""

from decimal import Decimal as D

from pricing_engine.catalog import CatalogSnapshot
from pricing_engine.size_table import offers_for


def test_every_beheshti_fast_disk_plan_fits_the_formula_its_table_follows(snapshot):
    rows = [i for i in snapshot.provider_items("hostiran")
            if i.datacenter == "Beheshti" and i.attributes.get("disk_class") == "ssd"]
    assert len(rows) == 17 and all(r.sku == r.name for r in rows)  # the plain plan names it has had from the start
    for r in rows:
        expected = r.cpu_cores * D(384000) + r.ram_gb * D(288000) + r.storage_gb * D(10800) + D(450000)
        assert r.price_toman == expected, r.sku
    # what the table doesn't say stays unsaid (its disk class is an attribute: a storage type would change the rows)
    assert {(r.cpu_shared, r.storage_type, r.included_traffic_gb) for r in rows} == {(None, None, None)}
    assert {(r.cpu_shared, r.included_traffic_gb) for r in snapshot.provider_items("hostiran")} == {(None, None)}


def test_the_page_shows_its_cheapest_cluster_for_a_size_and_says_which(snapshot):
    offers, _ = offers_for(snapshot, 1, 1, 25)
    hostiran = next(o for o in offers if o["provider_id"] == "hostiran")
    # its 20 GB plans are too small for 25 GB; the cheapest that covers it is in its economy cluster, G8 by its API
    assert (hostiran["plan"], hostiran["plan_name"], hostiran["monthly_toman"]) == (
        "Cost-Optimized 3 (Economy)", "Cost-Optimized 3", "1398000")
    assert (hostiran["cluster"], hostiran["generation"], hostiran["disk_type"], hostiran["datacenter"]) == (
        "کلاستر اقتصادی", "G8", "ssd", None)
    assert hostiran["conditions"] == [{"id": "ipv4_assumed"}, {"id": "traffic_not_stated"}]
    # at 8 vCPU, 16 GB and 200 GB it is the enterprise cluster (G9) with economy disks, HDD and SSD together
    offers, _ = offers_for(snapshot, 8, 16, 200)
    hostiran = next(o for o in offers if o["provider_id"] == "hostiran")
    assert (hostiran["plan"], hostiran["monthly_toman"], hostiran["cluster"], hostiran["generation"],
            hostiran["disk_type"]) == ("Memory Optimized 4 (Enterprise, economy disk)", "10722000", "کلاستر سازمانی",
                                       "G9", "hybrid")
    # its biggest disk is 240 GB: for 400 GB it has nothing
    _, missing = offers_for(snapshot, 16, 64, 400)
    assert {"provider_id": "hostiran", "reason": "nothing_this_size"} in missing


def test_a_plan_without_a_storage_type_still_says_its_disk(snapshot):
    """The Beheshti fast-disk plans have no storage type (it would change their identity), only a disk class; the
    page shows that. Their cluster has no generation in the API, so none is shown."""
    beheshti = CatalogSnapshot(
        providers=snapshot.providers, policy=snapshot.policy, billing_rules=snapshot.billing_rules,
        equivalence_rules=snapshot.equivalence_rules, recommendation_rules=snapshot.recommendation_rules,
        snapshot_at=snapshot.snapshot_at,
        items=[i for i in snapshot.items if i.provider_id != "hostiran" or i.sku == i.name])
    offers, _ = offers_for(beheshti, 1, 1, 25)
    hostiran = next(o for o in offers if o["provider_id"] == "hostiran")
    assert (hostiran["plan"], hostiran["monthly_toman"], hostiran["datacenter"], hostiran["cluster"],
            hostiran["generation"], hostiran["disk_type"]) == (
        "General Purpose 3", "3570000", "Beheshti", "کلاستر حرفه ای", None, "ssd")


def test_a_real_workload_still_needs_its_traffic_terms(snapshot):
    """Only the page, which prices no traffic, leaves them out. The engine behind the dashboard's estimates and
    suggestions doesn't price a HostIran plan without them."""
    from pricing_engine import Resource
    from pricing_engine.models import PriceStatus
    from pricing_engine.pricing import price_resource

    for sku in ("General Purpose 1", "General Purpose 1 (Enterprise)"):
        r = Resource(id="r", service_type="compute_vm", provider_id="hostiran", sku=sku,
                     expected_monthly_download_gb=D(0), expected_monthly_upload_gb=D(0))
        assert price_resource(r, snapshot).status == PriceStatus.PRICE_UNAVAILABLE
