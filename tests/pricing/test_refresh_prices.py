"""The public table's refresh: a reviewed price file for a provider whose page can't be read (ArvanCloud)."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FILE = ROOT / "data" / "price-files" / "arvancloud-2026-11-24.json"


@pytest.fixture(scope="module")
def refresh_prices():
    spec = importlib.util.spec_from_file_location("refresh_prices", ROOT / "scripts" / "refresh_prices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arvan(refresh_prices, state, cpu=2, ram=4, disk=50):
    table = json.loads(refresh_prices.render(state, datetime.now(UTC)))
    size = next(s for s in table["sizes"] if (s["cpu"], s["ram_gb"], s["disk_gb"]) == (cpu, ram, disk))
    return next(o for o in size["offers"] if o["provider_id"] == "arvancloud")


def test_a_price_file_replaces_the_rows_it_names_and_dates_them_today(refresh_prices):
    state = {"schema": 1, "providers": {}}
    assert arvan(refresh_prices, state)["monthly_toman"] == "2019000"
    assert arvan(refresh_prices, state)["old_after_days"] == 14  # read on 4 October: marked old

    report = refresh_prices.import_file(state, FILE, approve=set())

    assert (report["status"], report["applied"]) == ("ok", True)
    # what the file changed is an event for the public record, marked as read by hand
    assert (report["event"]["source"], report["event"]["since"], len(report["event"]["changed"])) == ("file", "2026-10-04", 33)
    offer = arvan(refresh_prices, state)
    # 2 x 252,000 + 4 x 196,200 + 50 x 18,000 + 234,000 for the first IPv4
    assert offer["monthly_toman"] == "2422800"
    assert offer["conditions"] == [{"id": "ipv4_priced", "toman": "234000"}]
    assert "old_after_days" not in offer and offer["read_on"] == datetime.now(UTC).date().isoformat()
    kept = {i["sku"]: i for i in state["providers"]["arvancloud"]["items"]}
    assert len(state["providers"]["arvancloud"]["items"]) == 50  # every row is still there
    assert kept["IR-GPU-A30"]["source_retrieved_at"] == "2026-10-04"  # a row the file doesn't name keeps its date


def test_a_file_with_an_unknown_sku_changes_nothing(refresh_prices, tmp_path):
    bad = json.loads(FILE.read_text(encoding="utf-8"))
    bad["prices"].append({"sku": "IR-CPU-NOPE", "price_toman": "1"})
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    state = {"schema": 1, "providers": {}}

    report = refresh_prices.import_file(state, path, approve=set())

    assert (report["status"], report["applied"]) == ("failed", False) and "IR-CPU-NOPE" in report["error"]
    assert state["providers"] == {}


def test_a_sharp_move_waits_for_approval(refresh_prices, tmp_path):
    doubled = json.loads(FILE.read_text(encoding="utf-8"))
    for row in doubled["prices"]:
        row["price_toman"] = str(int(row["price_toman"]) * 2)
    path = tmp_path / "doubled.json"
    path.write_text(json.dumps(doubled), encoding="utf-8")
    state = {"schema": 1, "providers": {}}

    held = refresh_prices.import_file(state, path, approve=set())
    assert (held["status"], held["applied"]) == ("quarantined", False) and state["providers"] == {}

    approved = refresh_prices.import_file(state, path, approve={"arvancloud"})
    assert approved["applied"] and "arvancloud" in state["providers"]


def test_each_run_signs_the_whole_catalog_and_sends_it_to_every_mirror(refresh_prices, tmp_path):
    """Installations update from Sarfeh's price service: what the reader publishes there is the whole catalog, signed,
    with when each provider was read. A mirror that can't be reached doesn't stop the others."""
    from provider_adapters import generate_signing_key, read_feed

    private, public = generate_signing_key()
    (tmp_path / "catalog-signing-key").write_text(private + "\n")
    state = {"schema": 1, "providers": {}}
    refresh_prices.import_file(state, FILE, approve=set())
    sent = []

    def send(url, content, signature):
        if "down" in url:
            raise ConnectionError("no route")
        sent.append((url, content, signature))
        return "stored"

    config = {"key_file": "catalog-signing-key", "hubs": ["https://ir.example", "https://down.example"]}
    at = datetime(2026, 11, 24, 15, 15, tzinfo=UTC)
    lines = refresh_prices.publish_catalog(state, at, config, tmp_path, send=send)

    assert lines[0] == "https://ir.example: stored" and lines[1].startswith("https://down.example: failed")
    url, content, signature = sent[0]
    assert url == "https://ir.example/v1/prices"
    generated_at, rows = read_feed(content, signature, public)
    assert generated_at == at
    assert len(rows["arvancloud"]) == 50 and "abrha" in rows  # providers never read live come from the seed
    data = json.loads(content)
    assert data["providers"]["arvancloud"]["read_on"] == state["providers"]["arvancloud"]["read_on"]
    assert "read_on" not in data["providers"]["abrha"]
    assert (tmp_path / "catalog" / "catalog.json").read_bytes() == content  # a copy stays beside the state file

    # read again with the same prices: a new catalog, but the same digests, so installations aren't told "new prices"
    again = json.loads(refresh_prices.build_feed(refresh_prices.snapshot_from(state, at).items, at.replace(hour=16)))
    assert again["digest"] == data["digest"] and again["generated_at"] != data["generated_at"]
