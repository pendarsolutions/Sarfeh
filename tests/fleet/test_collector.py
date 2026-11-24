"""Reading collector output and turning raw counters into usage. Plain data, no network."""

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from fleet import CollectError, Sample, enroll_command, enroll_script, parse_collect, suggest_plans, summarize
from pricing_engine.models import CatalogItem
from pricing_engine.size_table import cpu_model_hidden

T0 = datetime(2026, 11, 1, tzinfo=UTC)
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExample"
OUTPUT = """version=1
hostname=web-1
os=Ubuntu 24.04 LTS
cpu_cores=2
cpu_total=100000
cpu_idle=90000
mem_total_kb=4000000
mem_available_kb=3000000
disk_total_kb=41000000
disk_used_kb=10485760
rx_bytes=1000
tx_bytes=2000
"""


def sample(minutes=0, total=0, idle=0, avail=2_000_000, rx=0, tx=0, steal=None):
    return Sample(T0 + timedelta(minutes=minutes), total, idle, 4_000_000, avail, 41_000_000, 10_485_760, rx, tx, steal)


def test_parse_reads_all_fields():
    c = parse_collect(OUTPUT, T0)
    assert (c.hostname, c.os, c.cpu_cores) == ("web-1", "Ubuntu 24.04 LTS", 2)
    assert (c.sample.cpu_total, c.sample.tx_bytes) == (100000, 2000)


@pytest.mark.parametrize("bad", ["", "cpu_cores=2\n", OUTPUT.replace("cpu_total=100000", "cpu_total=abc")])
def test_parse_rejects_incomplete_output(bad):
    with pytest.raises(CollectError):
        parse_collect(bad, T0)


def test_version_2_also_reads_the_cpu_model_and_what_the_host_takes():
    old = parse_collect(OUTPUT, T0)  # a server set up before: nothing is missing, the new readings are just unknown
    assert (old.version, old.cpu_model, old.cpu_mhz, old.sample.cpu_steal) == (1, None, None, None)

    new = parse_collect(OUTPUT.replace("version=1", "version=2")
                        + "cpu_model=Intel(R) Xeon(R)   Gold 6248R CPU @ 3.00GHz\ncpu_mhz=2993\ncpu_steal=1500\n", T0)
    assert (new.version, new.cpu_model, new.cpu_mhz) == (2, "Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz", 2993)
    assert new.sample.cpu_steal == 1500
    assert not cpu_model_hidden(new.cpu_model)
    assert cpu_model_hidden("QEMU Virtual CPU version 2.5+") and cpu_model_hidden("Common KVM processor")
    assert not cpu_model_hidden(None)


def test_stolen_time_is_measured_beside_use_and_still_counts_as_demand():
    # of each 1,000 ticks: 600 idle, 250 worked, 150 the host gave to someone else
    s = summarize([sample(0, 0, 0, steal=0), sample(5, 1000, 600, steal=150), sample(10, 2000, 1200, steal=300)])
    assert s.p95_steal_percent == D("15.00")
    # the server asked for 40%: a starved server must never look idle enough to shrink
    assert s.p95_cpu_percent == D("40.00")
    # without the counter (a version 1 collector) there is no steal figure, and nothing else changes
    s = summarize([sample(0, 0, 0), sample(5, 1000, 600), sample(10, 2000, 1200)])
    assert (s.p95_steal_percent, s.p95_cpu_percent) == (None, D("40.00"))


def test_the_script_only_reads_and_reports_its_version():
    script = enroll_script(PUB)
    assert 'echo "version=2"' in script and "cpu_steal=" in script and "/proc/cpuinfo" in script
    collect = script.split("<<'FINOPS_COLLECT'")[1].split("FINOPS_COLLECT")[0]
    assert not any(word in collect for word in (" > ", ">>", "rm ", "sudo", "curl", "wget", "tee "))


def test_cpu_comes_from_counter_differences():
    s = summarize([sample(0, 0, 0), sample(5, 1000, 750), sample(10, 2000, 1500)])
    assert s.p95_cpu_percent == D("25.00")


def test_ram_p95_is_an_observed_value():
    samples = [sample(i, avail=2_000_000) for i in range(19)] + [sample(19, avail=400_000)]
    s = summarize(samples)
    assert s.p95_ram_percent == D("50.00")  # one spike out of 20 sits above the 95th percentile


def test_reboot_interval_is_skipped_not_negative():
    s = summarize([sample(0, 5000, 4000, rx=9000, tx=9000), sample(5, 100, 50, rx=10, tx=10), sample(10, 1100, 550, rx=20, tx=20)])
    assert s.p95_cpu_percent == D("50.00")


def test_traffic_is_withheld_under_one_day():
    s = summarize([sample(0, tx=0), sample(60, tx=10 ** 9)])
    assert s.monthly_download_gb is None and s.monthly_upload_gb is None


def test_traffic_is_projected_and_labelled_after_one_day():
    s = summarize([sample(0, tx=0, rx=0), sample(24 * 60, tx=10 ** 9, rx=2 * 10 ** 9)], days_per_month=30)
    assert s.monthly_download_gb == D("30.00")  # outgoing
    assert s.monthly_upload_gb == D("60.00")
    assert s.traffic_projected is True
    assert s.observation_days == 1


def test_full_month_is_measured_not_projected():
    s = summarize([sample(0, tx=0), sample(30 * 24 * 60, tx=5 * 10 ** 9)], days_per_month=30)
    assert s.monthly_download_gb == D("5.00") and s.traffic_projected is False


def test_no_samples_gives_no_numbers():
    s = summarize([])
    assert s.p95_cpu_percent is None and s.p95_ram_percent is None and s.monthly_download_gb is None


def _plan(provider, sku, cpu, ram, disk, price="1000"):
    return CatalogItem(id=sku, provider_id=provider, service_type="compute_vm", pricing_model="fixed_sku", sku=sku, name=sku,
                       cpu_cores=cpu, ram_gb=ram, storage_gb=disk, price_toman=price)


def test_suggestions_allow_overhead_and_list_every_provider():
    items = [_plan("a", "fit", "2", "4", "40"), _plan("b", "fit2", "2", "4", "40"), _plan("a", "big-ram", "2", "8", "40"),
             _plan("a", "big-disk", "2", "4", "160"), _plan("a", "more-cpu", "4", "4", "40")]
    found = suggest_plans(2, 3_900_000, 40_000_000, items)
    assert [i.sku for i in found] == ["fit", "fit2"]


def test_suggestions_need_known_specs():
    assert suggest_plans(None, None, None, [_plan("a", "x", "2", "4", "40")]) == []


def test_enroll_key_is_restricted_and_root_owned():
    script = enroll_script(PUB)
    assert 'command="/usr/local/lib/finops-agent/collect.sh"' in script
    for option in ("no-port-forwarding", "no-agent-forwarding", "no-X11-forwarding", "no-pty"):
        assert option in script
    assert "chown -R root:root" in script
    assert "sudoers" not in script


def test_enroll_can_limit_source_ip_and_refuses_bad_input():
    assert 'from="5.6.7.8",command=' in enroll_script(PUB, "5.6.7.8")
    with pytest.raises(ValueError):
        enroll_script("ssh-ed25519 AAA; rm -rf /")
    with pytest.raises(ValueError):
        enroll_script(PUB, '1.1.1.1" evil="')


def test_enroll_command_decodes_to_the_script():
    cmd = enroll_command(PUB)
    encoded = cmd.split()[1]
    assert base64.b64decode(encoded).decode() == enroll_script(PUB)
    assert 'base64 -d | if [ "$(id -u)" = 0 ]; then sh; else sudo sh; fi' in cmd


def test_empty_source_ip_means_no_limit():
    # docker compose passes an unset optional variable as an empty string
    assert "from=" not in enroll_script(PUB, "")
