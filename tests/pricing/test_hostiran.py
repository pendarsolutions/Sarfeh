"""HostIran's plans, from its price API (the Beheshti cluster, the same numbers as the table it sent on 2026-11-25).

Its prices follow one formula: 384,000 Toman a core, 288,000 a GB of memory, 10,800 a GB of disk and 450,000 a server.
Every row fitting it is a check that the API was read right."""

from decimal import Decimal as D

from pricing_engine.size_table import offers_for


def test_every_plan_fits_the_formula_the_table_follows(snapshot):
    rows = [i for i in snapshot.items if i.provider_id == "hostiran"]
    assert len(rows) == 17
    for r in rows:
        expected = r.cpu_cores * D(384000) + r.ram_gb * D(288000) + r.storage_gb * D(10800) + D(450000)
        assert r.price_toman == expected, r.sku
    # what the table doesn't say stays unsaid
    assert {(r.cpu_shared, r.storage_type, r.included_traffic_gb) for r in rows} == {(None, None, None)}


def test_the_page_prices_it_without_traffic_and_says_so(snapshot):
    offers, _ = offers_for(snapshot, 1, 1, 25)
    hostiran = next(o for o in offers if o["provider_id"] == "hostiran")
    # its 20 GB plans are too small for 25 GB, so the first that covers it is General Purpose 3
    assert (hostiran["plan"], hostiran["monthly_toman"], hostiran["datacenter"]) == ("General Purpose 3", "3570000", "Beheshti")
    assert hostiran["conditions"] == [{"id": "ipv4_assumed"}, {"id": "traffic_not_stated"}]
    # its biggest disk is 240 GB: for 400 GB it has nothing
    _, missing = offers_for(snapshot, 16, 64, 400)
    assert {"provider_id": "hostiran", "reason": "nothing_this_size"} in missing


def test_a_real_workload_still_needs_its_traffic_terms(snapshot):
    """Only the page, which prices no traffic, leaves them out. The engine behind the dashboard's estimates and
    suggestions doesn't price a HostIran plan without them."""
    from pricing_engine import Resource
    from pricing_engine.models import PriceStatus
    from pricing_engine.pricing import price_resource

    r = Resource(id="r", service_type="compute_vm", provider_id="hostiran", sku="General Purpose 1",
                 expected_monthly_download_gb=D(0), expected_monthly_upload_gb=D(0))
    assert price_resource(r, snapshot).status == PriceStatus.PRICE_UNAVAILABLE
