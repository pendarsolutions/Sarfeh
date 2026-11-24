"""Pure logic for server monitoring. No network and no database, so all of it is testable with plain data.

What a monitored server gets is deliberately small: one unprivileged user and one read-only script. The SSH key
is restricted on the server itself, so even a stolen key can run only that script.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from pricing_engine.models import CatalogItem

#: the script's version. 2 added the CPU model and the time the host takes away (steal); a server set up before
#: keeps answering with version 1 until its setup command is run again.
COLLECTOR_VERSION = 2
COLLECTOR_USER = "finops-collector"
COLLECT_PATH = "/usr/local/lib/finops-agent/collect.sh"
HUNDRED = Decimal(100)
GIB = Decimal(1024 ** 3)
#: traffic in decimal GB gives the higher figure, so a quota is never overstated as safe
TRAFFIC_GB = Decimal(1000 ** 3)
KIB_PER_GIB = Decimal(1024 ** 2)
#: shortest observation a monthly traffic projection may be based on
MIN_PROJECTION_SECONDS = 86_400
#: reported memory and disk run below the plan's size (kernel reservations, filesystem overhead)
RAM_TOLERANCE = Decimal("0.12")
DISK_TOLERANCE = Decimal("0.12")

COLLECT_SCRIPT = r"""#!/bin/sh
# FinOps agent collector. Read-only: prints usage counters and exits.
export LC_ALL=C
echo "version=2"
echo "hostname=$(hostname)"
[ -r /etc/os-release ] && . /etc/os-release && echo "os=${PRETTY_NAME:-unknown}"
echo "cpu_cores=$(grep -c '^processor' /proc/cpuinfo)"
awk '/^model name/{sub(/^[^:]*: */, ""); print "cpu_model=" $0; exit}' /proc/cpuinfo
awk '/^cpu MHz/{sub(/^[^:]*: */, ""); printf "cpu_mhz=%.0f\n", $0; exit}' /proc/cpuinfo
awk '/^cpu /{t=0; for(i=2;i<=9;i++) t+=$i; printf "cpu_total=%.0f\ncpu_idle=%.0f\ncpu_steal=%.0f\n", t, $5+$6, $9}' /proc/stat
awk '/^MemTotal:/{printf "mem_total_kb=%.0f\n",$2} /^MemAvailable:/{printf "mem_available_kb=%.0f\n",$2}' /proc/meminfo
df -Pk / | awk 'NR==2{printf "disk_total_kb=%.0f\ndisk_used_kb=%.0f\n",$2,$3}'
awk 'NR>2{sub(/:/," "); if($1!~/^(lo|docker|veth|br-|virbr|cni|flannel|cali|tun|tap)/){r+=$2; t+=$10}} END{printf "rx_bytes=%.0f\ntx_bytes=%.0f\n",r,t}' /proc/net/dev
"""


class CollectError(ValueError):
    """The server's output could not be understood."""


@dataclass
class Sample:
    taken_at: datetime
    cpu_total: int
    cpu_idle: int
    mem_total_kb: int
    mem_available_kb: int
    disk_total_kb: int
    disk_used_kb: int
    rx_bytes: int
    tx_bytes: int
    #: time the host gave this server's CPU to someone else; None from a version 1 collector
    cpu_steal: int | None = None


@dataclass
class Collected:
    hostname: str
    os: str | None
    cpu_cores: int
    sample: Sample
    version: int = 1
    #: as /proc/cpuinfo names it; a host can show a stand-in instead of the real processor
    cpu_model: str | None = None
    cpu_mhz: int | None = None


@dataclass
class UsageSummary:
    #: busy time and stolen time together: what the server asked for. A server starved by its host still counts
    #: as in use, so it is never advised to shrink.
    p95_cpu_percent: Decimal | None = None
    #: the part of that which the host didn't deliver. High values mean the host is oversold.
    p95_steal_percent: Decimal | None = None
    p95_ram_percent: Decimal | None = None
    disk_used_gb: Decimal | None = None
    observation_days: int = 0
    monthly_download_gb: Decimal | None = None
    monthly_upload_gb: Decimal | None = None
    traffic_projected: bool = False

    def as_json(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}


def parse_collect(text: str, taken_at: datetime) -> Collected:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    def optional(key: str) -> int | None:  # what version 1 doesn't print, or a machine doesn't have
        return int(values[key]) if values.get(key, "").isdigit() else None

    try:
        return Collected(
            hostname=values.get("hostname", "")[:200],
            os=(values.get("os") or None) and values["os"][:200],
            cpu_cores=int(values["cpu_cores"]),
            sample=Sample(taken_at=taken_at, cpu_total=int(values["cpu_total"]), cpu_idle=int(values["cpu_idle"]),
                          mem_total_kb=int(values["mem_total_kb"]), mem_available_kb=int(values["mem_available_kb"]),
                          disk_total_kb=int(values["disk_total_kb"]), disk_used_kb=int(values["disk_used_kb"]),
                          rx_bytes=int(values["rx_bytes"]), tx_bytes=int(values["tx_bytes"]),
                          cpu_steal=optional("cpu_steal")),
            version=optional("version") or 1,
            cpu_model=" ".join(values.get("cpu_model", "").split())[:200] or None,
            cpu_mhz=optional("cpu_mhz"),
        )
    except (KeyError, ValueError) as exc:
        raise CollectError(f"unexpected collector output: {exc}") from exc


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def percentile95(values: Iterable[Decimal]) -> Decimal | None:
    """Nearest-rank: the value is one that was really observed, not an interpolation."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) * 95 + 99) // 100
    return ordered[rank - 1]


def summarize(samples: list[Sample], days_per_month: int = 30) -> UsageSummary:
    """Usage from raw counters. A counter that goes backwards means a reboot, and that interval is skipped."""
    samples = sorted(samples, key=lambda s: s.taken_at)
    out = UsageSummary()
    if not samples:
        return out
    ram = [HUNDRED * (s.mem_total_kb - s.mem_available_kb) / s.mem_total_kb for s in samples if s.mem_total_kb > 0]
    out.p95_ram_percent = (lambda v: None if v is None else _round(v))(percentile95(Decimal(x) for x in ram))
    out.disk_used_gb = _round(Decimal(samples[-1].disk_used_kb) / KIB_PER_GIB)
    cpu: list[Decimal] = []
    steal: list[Decimal] = []
    rx, tx = 0, 0
    measured_seconds = Decimal(0)
    for prev, cur in zip(samples, samples[1:], strict=False):
        total, idle = cur.cpu_total - prev.cpu_total, cur.cpu_idle - prev.cpu_idle
        if total > 0 and 0 <= idle <= total:
            cpu.append(HUNDRED * (total - idle) / total)
            if prev.cpu_steal is not None and cur.cpu_steal is not None and 0 <= cur.cpu_steal - prev.cpu_steal <= total:
                steal.append(HUNDRED * (cur.cpu_steal - prev.cpu_steal) / total)
        if cur.rx_bytes >= prev.rx_bytes and cur.tx_bytes >= prev.tx_bytes:
            rx += cur.rx_bytes - prev.rx_bytes
            tx += cur.tx_bytes - prev.tx_bytes
            measured_seconds += Decimal(str((cur.taken_at - prev.taken_at).total_seconds()))
    p95 = percentile95(cpu)
    out.p95_cpu_percent = None if p95 is None else _round(p95)
    p95 = percentile95(steal)
    out.p95_steal_percent = None if p95 is None else _round(p95)
    span = (samples[-1].taken_at - samples[0].taken_at).total_seconds()
    out.observation_days = int(span // 86_400)
    if measured_seconds >= MIN_PROJECTION_SECONDS:
        month = Decimal(days_per_month * 86_400)
        scale = month / measured_seconds
        out.traffic_projected = measured_seconds < month
        # the server's outgoing traffic is what users download from it
        out.monthly_download_gb = _round(Decimal(tx) * scale / TRAFFIC_GB)
        out.monthly_upload_gb = _round(Decimal(rx) * scale / TRAFFIC_GB)
    return out


def suggest_plans(cpu_cores: int | None, mem_total_kb: int | None, disk_total_kb: int | None,
                  items: Iterable[CatalogItem]) -> list[CatalogItem]:
    """Catalog plans whose specs fit what the server reports. Only ever a suggestion: a server can't say which
    plan it is on, and several providers sell the same specs, so the user always chooses."""
    if not cpu_cores or not mem_total_kb:
        return []
    ram_gb = Decimal(mem_total_kb) / KIB_PER_GIB
    disk_gb = Decimal(disk_total_kb) / KIB_PER_GIB if disk_total_kb else None
    out = []
    for item in items:
        if item.service_type != "compute_vm" or not item.sku or item.price_toman is None:
            continue
        if item.cpu_cores != cpu_cores or item.ram_gb is None:
            continue
        if abs(item.ram_gb - ram_gb) > item.ram_gb * RAM_TOLERANCE:
            continue
        if disk_gb is not None and item.storage_gb is not None and abs(item.storage_gb - disk_gb) > item.storage_gb * DISK_TOLERANCE:
            continue
        out.append(item)
    return sorted(out, key=lambda i: (i.provider_id, i.price_toman))


# --------------------------------------------------------------------------- enrollment


def enroll_script(public_key: str, source_ip: str | None = None) -> str:
    """Run once as root on a server. Idempotent. Contains only the public key, so it isn't secret."""
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", public_key.strip()):
        raise ValueError("expected an ssh-ed25519 public key")
    if source_ip and not re.fullmatch(r"[0-9a-fA-F:.]+", source_ip):
        raise ValueError("source_ip must be an IP address")
    options = [f'command="{COLLECT_PATH}"', "no-port-forwarding", "no-agent-forwarding", "no-X11-forwarding", "no-pty"]
    if source_ip:
        options.insert(0, f'from="{source_ip}"')
    home = f"/var/lib/{COLLECTOR_USER}"
    return f"""#!/bin/sh
set -e
[ "$(id -u)" = 0 ] || {{ echo "Run this as root (sudo)." >&2; exit 1; }}
if ! id {COLLECTOR_USER} >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --create-home --home-dir {home} --shell /bin/sh {COLLECTOR_USER}
  else
    adduser -S -D -h {home} -s /bin/sh {COLLECTOR_USER}
  fi
fi
# no password can be used, but the account is not locked: sshd refuses locked accounts even for keys
echo "{COLLECTOR_USER}:*" | chpasswd -e
mkdir -p {COLLECT_PATH.rsplit("/", 1)[0]} {home}/.ssh
cat > {COLLECT_PATH} <<'FINOPS_COLLECT'
{COLLECT_SCRIPT}FINOPS_COLLECT
chmod 755 {COLLECT_PATH}
# root owns the key file, so the collector user can never change its own restrictions
cat > {home}/.ssh/authorized_keys <<'FINOPS_KEY'
{",".join(options)} {public_key.strip()} finops-agent
FINOPS_KEY
chown -R root:root {home}/.ssh
chmod 755 {home} {home}/.ssh
chmod 644 {home}/.ssh/authorized_keys
if grep -Eqi '^[[:space:]]*(AllowUsers|AllowGroups)' /etc/ssh/sshd_config 2>/dev/null; then
  echo "Warning: sshd_config restricts logins. Add {COLLECTOR_USER} to AllowUsers, or the connection will be refused." >&2
fi
echo "FinOps agent: this server is ready. The collector is read-only and has no sudo."
"""


def enroll_command(public_key: str, source_ip: str | None = None) -> str:
    encoded = base64.b64encode(enroll_script(public_key, source_ip).encode()).decode()
    # root logins on small VPSes often have no sudo installed
    return f'echo {encoded} | base64 -d | if [ "$(id -u)" = 0 ]; then sh; else sudo sh; fi'


def removal_command() -> str:
    steps = f"userdel -r {COLLECTOR_USER}; rm -rf {COLLECT_PATH.rsplit('/', 1)[0]}"
    return f'if [ "$(id -u)" = 0 ]; then {steps}; else sudo sh -c "{steps}"; fi'
