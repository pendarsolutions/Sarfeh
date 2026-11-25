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


def test_without_its_traffic_terms_the_page_says_why_there_is_no_price(snapshot):
    _, missing = offers_for(snapshot, 1, 1, 25)
    assert {"provider_id": "hostiran", "reason": "terms_not_stated", "terms": ["traffic_policy"]} in missing
    # its biggest disk is 240 GB: for 400 GB it has nothing, terms or not
    _, missing = offers_for(snapshot, 16, 64, 400)
    assert {"provider_id": "hostiran", "reason": "nothing_this_size"} in missing
