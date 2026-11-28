"""TrustHost's plans: a quota of traffic entering the server, free traffic leaving it, a published overage rate."""

from decimal import Decimal as D

from pricing_engine import Resource
from pricing_engine.models import BillingContext, LineStatus, PriceStatus
from pricing_engine.pricing import price_resource


def jaraghe(download: str, upload: str, quantity: int = 1) -> Resource:
    return Resource(id="r", service_type="compute_vm", provider_id="trusthost", sku="جرقه", quantity=quantity,
                    expected_monthly_download_gb=D(download), expected_monthly_upload_gb=D(upload))


def test_the_seed_holds_the_21_plans(snapshot):
    rows = [i for i in snapshot.items if i.provider_id == "trusthost"]
    assert len(rows) == 21
    assert all(r.included_traffic_gb and r.attributes["overage_toman_per_gb"] == "900" for r in rows)


def test_traffic_leaving_the_server_is_free(snapshot):
    p = price_resource(jaraghe(download="5000", upload="40"), snapshot)
    assert p.status == PriceStatus.EXACT
    assert p.monthly_total_toman == D(495360)
    traffic = next(c for c in p.components if c.code == "traffic")
    assert traffic.status == LineStatus.INCLUDED and traffic.quantity == 40


def test_traffic_entering_beyond_the_quota_is_priced_at_the_published_rate(snapshot):
    # 100 GB included in Jaraghe: 150 GB in is 50 GB over, at 900 Toman/GB, on each of two servers
    p = price_resource(jaraghe(download="0", upload="150", quantity=2), snapshot)
    assert p.status == PriceStatus.EXACT
    over = next(c for c in p.components if c.code == "traffic.overage")
    assert (over.quantity, over.unit_price_toman, over.subtotal_toman) == (100, 900, 90000)
    assert p.monthly_total_toman == D(495360) * 2 + D(90000)


def test_vat_is_a_separate_line(snapshot):
    p = price_resource(jaraghe(download="0", upload="0"), snapshot, BillingContext(include_tax=True))
    assert [(t.code, t.subtotal_toman) for t in p.tax_lines] == [("tax.vat", D(49536))]
