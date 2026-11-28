"""TrustHost: its Iran cloud server plans, from the price feed TrustHost publishes for price comparison.

    https://my.trusthost.co/feeds/server-prices.php

TrustHost asked to be listed and published this feed. It is read straight from TrustHost's billing system (WHMCS), so
the prices are the ones it invoices; https://trusthost.co/server/ shows the same table. It holds the 21 plans of the
Tehran (Fanap) datacenter in four families: general purpose, compute, memory and storage.

Traffic: each plan includes a monthly quota of traffic entering the server (Sarfeh's "upload"); traffic leaving the
server is free. Beyond the quota, traffic is bought from the customer's wallet at a per-GB rate the feed states, and
the engine prices it at that rate. Larger purchases get volume rates, kept as an attribute and not applied, so an
overage is never priced below what a customer pays. Prices exclude VAT (10%, a separate invoice line).

Anything the feed doesn't state stays unset, and a feed whose terms differ from these (VAT, traffic direction) fails
the read rather than being priced on assumptions that no longer hold.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from pricing_engine.models import CatalogItem

from .base import HttpAdapterMixin, ParseError, RawCatalog, base_item

FAMILIES = {"general_purpose", "compute_optimized", "memory_optimized", "storage_optimized"}
DATACENTERS = {"فناپ تهران": "Fanap Tehran"}
VAT_PERCENT = "10"


def _dec(value: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ParseError(f"TrustHost feed: {what} is not a number ({value!r})") from exc
    if d <= 0:
        raise ParseError(f"TrustHost feed: {what} must be positive ({value!r})")
    return d


class TrustHostAdapter(HttpAdapterMixin):
    provider_id = "trusthost"
    source_url = "https://my.trusthost.co/feeds/server-prices.php"
    scope = {("compute_vm", f) for f in FAMILIES}
    key_skus = {"جرقه", "رشد", "پرداز ۱", "فراخ ۱", "انبار ۱"}

    def normalize_catalog(self, raw: RawCatalog) -> list[CatalogItem]:
        try:
            data = json.loads(raw.content)
        except json.JSONDecodeError as exc:
            raise ParseError(f"TrustHost's price feed didn't return JSON ({exc})") from exc
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ParseError("TrustHost's price feed has an unknown schema")
        if data.get("currency_unit") != "toman":
            raise ParseError(f"TrustHost's price feed is not in Toman ({data.get('currency_unit')!r})")
        tax = data.get("tax") or {}
        if tax.get("percent") != VAT_PERCENT or tax.get("included_in_prices") is not False:
            raise ParseError(f"TrustHost's VAT terms changed ({tax}); the trusthost-vat rule needs updating")
        overage = self._overage(data.get("traffic"))
        plans = data.get("plans") or []
        if not plans:
            raise ParseError("TrustHost's price feed lists no plans")
        # the feed is read live from the billing system, so its prices are current when it was generated;
        # catalog_updated_at is when a plan was last edited, which can be long before
        price_date = (data.get("generated_at") or "")[:10] or None

        items = []
        for p in plans:
            name = p.get("name")
            family = p.get("family")
            monthly = (p.get("price_toman") or {}).get("monthly")
            if not name or family not in FAMILIES or monthly is None:
                raise ParseError(f"TrustHost plan without a name, a known family or a monthly price: {p.get('id')}")
            quota = p.get("inbound_traffic_gb")
            dc_fa = p.get("datacenter")
            attrs: dict[str, Any] = {
                "whmcs_pid": p.get("id"),
                "catalog_updated_at": data.get("catalog_updated_at"),
                "family_fa": p.get("family_name"),
                "datacenter_fa": dc_fa,
                "port_gbps": p.get("port_gbps"),
                "order_url": p.get("order_url"),
                "term_prices_toman": {k: v for k, v in (p.get("price_toman") or {}).items() if v is not None},
            }
            if quota is not None:
                attrs.update(overage)
            items.append(base_item(
                self.provider_id, raw, service_type="compute_vm", pricing_model="fixed_sku", product_family=family,
                sku=name, name=name, datacenter=DATACENTERS.get(dc_fa, dc_fa),
                cpu_cores=_dec(p.get("vcpu"), f"{name} vCPU"), cpu_unit="vCPU",
                ram_gb=_dec(p.get("ram_gb"), f"{name} RAM"), storage_gb=_dec(p.get("disk_gb"), f"{name} disk"),
                storage_type=p.get("disk_type"),
                included_traffic_gb=_dec(quota, f"{name} traffic quota") if quota is not None else None,
                billing_period="month", price_toman=_dec(monthly, f"{name} monthly price"), unit="plan_month",
                tax_included=False, source_price_date=price_date,
                notes=(f"inbound {quota}GB/month included, outbound free; beyond the quota "
                       f"{overage.get('overage_toman_per_gb')} Toman/GB; invoices add {VAT_PERCENT}% VAT."
                       if quota is not None else f"traffic quota not stated; invoices add {VAT_PERCENT}% VAT."),
                attributes=attrs,
            ))
        return items

    @staticmethod
    def _overage(traffic: Any) -> dict[str, Any]:
        """The over-quota rate, checked against the terms the engine prices TrustHost with."""
        if not isinstance(traffic, dict):
            raise ParseError("TrustHost's price feed states no traffic terms")
        if traffic.get("metered_direction") != "inbound" or traffic.get("outbound") != "free":
            raise ParseError(f"TrustHost's traffic terms changed ({traffic.get('metered_direction')!r}, "
                             f"{traffic.get('outbound')!r}); engine_policy needs updating")
        rate = _dec(traffic.get("overage_toman_per_gb"), "overage rate")
        return {
            "overage_toman_per_gb": str(rate),
            "overage_volume_rates": traffic.get("overage_volume_rates") or [],
        }
