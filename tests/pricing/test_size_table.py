"""The public price table: one server size at every provider, each figure as the provider published it."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from pricing_engine import load_seed_snapshot
from pricing_engine.size_table import DISKS_GB, MEMORY_FOR, offers_for, size_table

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def snapshot():
    return load_seed_snapshot()


@pytest.fixture(scope="module")
def table(snapshot):
    return size_table(snapshot)


def test_every_size_in_the_grid_is_priced(table):
    assert len(table["sizes"]) == sum(len(m) for m in MEMORY_FOR.values()) * len(DISKS_GB)
    assert all(s["offers"] for s in table["sizes"])  # at least one provider sells every size shown


def test_an_offer_covers_the_size_and_the_cheapest_comes_first(table):
    for s in table["sizes"]:
        prices = [Decimal(o["monthly_toman"]) for o in s["offers"]]
        assert prices == sorted(prices)
        assert len({o["provider_id"] for o in s["offers"]}) == len(s["offers"])  # one entry per provider
        for o in s["offers"]:
            assert Decimal(o["cpu"]) >= s["cpu"] and Decimal(o["ram_gb"]) >= s["ram_gb"]
            assert Decimal(o["disk_gb"]) >= s["disk_gb"]
            assert o["source_url"] and o["read_on"], o  # every price can be checked against its page
        assert s["offers"][0]["times_cheapest"] == "1.0"
        ranked = {o["provider_id"] for o in s["offers"]} | {m["provider_id"] for m in s["missing"]}
        assert ranked == set(table["providers"])  # every provider is either ranked or explained


def test_two_cores_four_gb_and_fifty_gb_of_disk(snapshot):
    offers, missing = offers_for(snapshot, 2, 4, 50)
    by = {o["provider_id"]: o for o in offers}
    assert [o["provider_id"] for o in offers] == ["trusthost", "mobinhost", "chabokan", "serverir", "parspack",
                                                  "arvancloud", "liara", "hostiran", "iranserver"]
    # TrustHost's quota is traffic entering the server; the size has none, so the plan is its price
    assert (by["trusthost"]["plan"], by["trusthost"]["monthly_toman"], by["trusthost"]["included_traffic_gb"]) == (
        "آغاز", "737280", "200")
    assert (by["mobinhost"]["plan"], by["mobinhost"]["monthly_toman"]) == ("IR-VPS-6GB", "995000")
    assert by["mobinhost"]["included_traffic_gb"] == "100" and by["mobinhost"]["traffic"] == "included_quota"
    # ArvanCloud has no plans: the size is built from its component prices, at its cheapest class
    assert (by["arvancloud"]["kind"], by["arvancloud"]["plan"], by["arvancloud"]["monthly_toman"]) == (
        "components", "basic", "2019000")
    assert (by["parspack"]["kind"], by["parspack"]["monthly_toman"]) == ("by_size", "1885950")
    # Liara sells no 50 GB disk below its 4 vCPU plan, so its entry is bigger than the size asked for
    assert (by["liara"]["cpu"], by["liara"]["ram_gb"], by["liara"]["disk_gb"]) == ("4", "8", "80")
    assert by["liara"]["times_cheapest"] == "6.5"  # 4,800,000 against TrustHost's 737,280
    # HostIran doesn't publish its traffic terms: the size has no traffic, so its plan is its price, and the page
    # says what was left out
    assert (by["hostiran"]["plan"], by["hostiran"]["monthly_toman"], by["hostiran"]["traffic"]) == (
        "General Purpose 4", "4938000", "unknown")
    assert {"id": "traffic_not_stated"} in by["hostiran"]["conditions"]
    assert missing == [{"provider_id": "abrha", "reason": "starting_price_only"}]


def test_vat_is_beside_the_price_never_inside_it(snapshot):
    offers, _ = offers_for(snapshot, 2, 4, 50)
    by = {o["provider_id"]: o for o in offers}
    # Server.ir and TrustHost are the providers whose sources state their VAT: 10% of 1,690,000 and of 737,280
    assert (by["serverir"]["monthly_toman"], by["serverir"]["vat_toman"]) == ("1690000", "169000")
    assert (by["trusthost"]["monthly_toman"], by["trusthost"]["vat_toman"]) == ("737280", "73728")
    assert all(o["vat_toman"] is None for pid, o in by.items() if pid not in ("serverir", "trusthost"))


def test_what_a_figure_assumes_or_leaves_out_is_said_with_it(snapshot):
    offers, _ = offers_for(snapshot, 2, 4, 50)
    by = {o["provider_id"]: o["conditions"] for o in offers}
    # plan providers don't say an address comes with the plan; the two that price one have it in the figure
    assert by["mobinhost"] == by["serverir"] == by["iranserver"] == [{"id": "ipv4_assumed"}]
    assert by["arvancloud"] == [{"id": "ipv4_priced", "toman": "195000"}]
    assert by["liara"] == [{"id": "ipv4_priced", "toman": "200000"}]
    # Chabokan sells 30 days, and ParsPack's price list mentions a setup fee that a monthly figure can't hold
    assert by["chabokan"] == [{"id": "ipv4_assumed"}, {"id": "thirty_day_plan"}]
    assert by["parspack"] == [{"id": "ipv4_assumed"}, {"id": "setup_fee", "days": 7}]


def test_a_cpu_model_a_user_reported_is_shown_with_its_plan(snapshot):
    from pricing_engine.size_table import cpu_models

    reports = [
        {"provider_id": "serverir", "plan": "IRFH5024", "cpu_model": "Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz",
         "reported_on": "2026-11-20"},
        {"provider_id": "serverir", "plan": "IRFH5024", "cpu_model": "Intel(R)  Xeon(R) Gold 6248R CPU @ 3.00GHz",
         "cpu_mhz": 2993, "reported_on": "2026-11-25"},
        {"provider_id": "parspack", "plan": None, "cpu_model": "QEMU Virtual CPU version 2.5+", "reported_on": "2026-11-25"},
    ]
    models = cpu_models(reports)
    # the newest report wins, and the count says there were two
    assert models[("serverir", "IRFH5024")] == {"model": "Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz", "hidden": False,
                                                 "mhz": "2993", "reported_on": "2026-11-25", "reports": 2}
    assert models[("parspack", None)]["hidden"] is True  # the host shows its guests a made-up processor

    table = size_table(snapshot, reports)
    size = next(s for s in table["sizes"] if (s["cpu"], s["ram_gb"], s["disk_gb"]) == (2, 4, 50))
    by = {o["provider_id"]: o for o in size["offers"]}
    assert by["serverir"]["cpu_model"]["model"].startswith("Intel(R) Xeon(R) Gold 6248R")
    assert by["parspack"]["cpu_model"]["hidden"] is True  # ParsPack prices by size: a report without a plan is its
    assert "cpu_model" not in by["mobinhost"]
    assert all("cpu_model" not in o for s in size_table(snapshot, [])["sizes"] for o in s["offers"])


def test_a_provider_that_sells_nothing_this_large_is_named_not_dropped(snapshot):
    offers, missing = offers_for(snapshot, 16, 64, 400)
    assert {"provider_id": "chabokan", "reason": "nothing_this_size"} in missing
    assert "chabokan" not in {o["provider_id"] for o in offers}


def test_amounts_are_exact_strings(table):
    text = json.dumps(table)
    assert json.loads(text, parse_float=lambda s: pytest.fail(f"float {s} in the table")) == table
    assert all(isinstance(o["monthly_toman"], str) for s in table["sizes"] for o in s["offers"])


def test_the_published_file_matches_the_catalog():
    spec = importlib.util.spec_from_file_location("build_price_table", ROOT / "scripts" / "build_price_table.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    # compared as a flag: pytest's diff of two ~300 KB lines takes minutes
    same = build.OUT.read_text(encoding="utf-8") == build.render()
    assert same, "run: python scripts/build_price_table.py"
