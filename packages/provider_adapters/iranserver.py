"""IranServer Iran VPS adapter - parses every plan table on the official page.

The page has one tab per family (general purpose, high traffic, memory, CPU, heavy processing), each a table of the
same shape. On 2026-10-04 only the general-purpose (NGP) table was in the server-rendered HTML; by 2026-11-18 all five
were, and reading NGP alone listed IranServer by its dearer plans (issue #2: NCO-small80 was missing at 1 core / 2 GB).
A plan of a family this adapter doesn't know fails the read rather than being dropped unseen.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from pricing_engine.models import CatalogItem

from .base import HttpAdapterMixin, ParseError, RawCatalog, base_item, parse_number

SKU_RE = re.compile(r"^\s*((N[A-Z]{2})-[A-Za-z0-9]+)\s*$")
#: plan-name prefix -> product family. NGP rows were the only ones read before, so theirs stays `general_purpose`.
FAMILIES = {"NGP": "general_purpose", "NHT": "high_traffic", "NMO": "memory_optimized", "NCO": "compute_optimized",
            "NHP": "high_performance"}
#: header words -> column. The page has reordered its columns before (memory moved ahead of CPU in November
#: 2026), so cells are found by their header, never by position.
HEADERS = {"ram": ("رم", "حافظه"), "cpu": ("پردازنده", "هسته"), "disk": ("فضا", "دیسک"), "traffic": ("ترافیک",),
           "price": ("قیمت",)}


def _columns(row) -> dict[str, int]:
    table = row.find_parent("table")
    head = table.find("tr") if table else None
    cells = [c.get_text(" ", strip=True) for c in head.find_all(["th", "td"])] if head else []
    found = {}
    for name, words in HEADERS.items():
        hits = [i for i, text in enumerate(cells) if any(w in text for w in words)]
        if len(hits) != 1:
            raise ParseError(f"plan table: no single '{name}' column in header {cells}")
        found[name] = hits[0]
    return found


def _tab_names(soup) -> dict[str, str]:
    """Tab index -> its label ('پردازنده محور'), to name each table's family in IranServer's words."""
    return {t["data-index"]: " ".join(t.get_text(" ", strip=True).split())
            for t in soup.select('[role="tab"][data-index]')}


def _tab_of(row, names: dict[str, str]) -> str | None:
    pane = row.find_parent(attrs={"role": "tabpanel"})
    return names.get(pane.get("data-index")) if pane else None


class IranServerAdapter(HttpAdapterMixin):
    provider_id = "iranserver"
    source_url = "https://www.iranserver.com/vps/iran/"
    scope = {("compute_vm", f) for f in FAMILIES.values()}
    key_skus = {"NGP-small40", "NGP-medium40", "NGP-large80", "NCO-small80", "NMO-small40"}

    def normalize_catalog(self, raw: RawCatalog) -> list[CatalogItem]:
        soup = BeautifulSoup(raw.content, "lxml")
        tabs = _tab_names(soup)
        seen: set[str] = set()
        items: list[CatalogItem] = []
        for row in soup.select("tr"):
            tds = row.find_all("td")
            if len(tds) < 6:
                continue
            first = tds[0].find("span")
            m = SKU_RE.match(first.get_text() if first else "")
            if not m or m.group(1) in seen:
                continue
            sku, prefix = m.groups()
            family = FAMILIES.get(prefix)
            if family is None:
                raise ParseError(f"{sku}: plan family {prefix} is new; add it to FAMILIES")
            seen.add(sku)
            col = _columns(row)
            tip = tds[0].select_one("[title]")
            tooltip = BeautifulSoup(tip["title"], "lxml").get_text(" ", strip=True) if tip else ""
            price = parse_number(tds[col["price"]].get_text())
            if price is None:
                raise ParseError(f"{sku}: price cell empty")
            tab = _tab_of(row, tabs)
            attributes = {"traffic_policy": "included_quota", "tooltip_fa": tooltip}
            if tab:
                attributes["family_fa"] = tab
            if "NVMe" in tooltip:
                attributes["disk_class"] = "nvme"
            items.append(base_item(
                self.provider_id, raw, service_type="compute_vm", pricing_model="fixed_sku",
                product_family=family, sku=sku, name=sku, cpu_cores=parse_number(tds[col["cpu"]].get_text()),
                cpu_shared=True if "اشتراکی" in tooltip else None, ram_gb=parse_number(tds[col["ram"]].get_text()),
                storage_gb=parse_number(tds[col["disk"]].get_text()),
                included_traffic_gb=parse_number(tds[col["traffic"]].get_text()),
                billing_period="month", price_toman=price, unit="plan_month",
                notes=f"{tab or family} family; official tooltip: {tooltip}" if tooltip else f"{tab or family} family.",
                attributes=attributes,
            ))
        if not items:
            raise ParseError("no IranServer plan rows found in any table")
        return items
