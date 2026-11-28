"""The record of price changes: what a read changed, as an event, a published list and a short Persian post."""

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from pricing_engine import load_seed_snapshot

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import price_feed  # noqa: E402

AT = datetime(2026, 11, 24, 11, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def rows():
    snapshot = load_seed_snapshot()
    return {pid: list(snapshot.provider_items(pid)) for pid in ("iranserver", "arvancloud")}


def change(items, sku, **updates):
    return [i.model_copy(update=updates) if i.sku == sku else i for i in items]


def test_a_price_and_a_size_that_moved_are_one_changed_plan(rows):
    before = rows["iranserver"]
    after = change(before, "NGP-large80", price_toman=D(8800638), ram_gb=D(32))

    e = price_feed.event("iranserver", before, after, AT, since="2026-11-23")

    assert (len(e["changed"]), e["new"], e["removed"]) == (1, [], [])
    row = e["changed"][0]
    assert (row["plan"], row["toman"], row["ram_gb"]) == ("NGP-large80", "8800638", "32")
    assert row["was"] == {"toman": "5323839", "ram_gb": "16"}
    assert price_feed.event("iranserver", before, list(before), AT) is None  # nothing moved: no event


def test_plans_that_appear_and_leave(rows):
    before = rows["iranserver"]
    gone = [i for i in before if i.sku != "NGP-small40"]
    e = price_feed.event("iranserver", before, gone, AT)
    assert [r["plan"] for r in e["removed"]] == ["NGP-small40"] and not e["changed"] and not e["new"]
    e = price_feed.event("iranserver", gone, before, AT)
    assert [r["plan"] for r in e["new"]] == ["NGP-small40"]


def test_a_post_says_who_moved_which_way_and_by_how_much(rows):
    before = rows["iranserver"]
    after = change(before, "NGP-large80", price_toman=D(8800638), ram_gb=D(32))
    text = price_feed.message(price_feed.event("iranserver", before, after, AT), "ایران‌سرور")
    assert text.splitlines()[0] == "ایران‌سرور قیمت ۱ پلن را بالا برد"
    assert "NGP-large80: از ۵٬۳۲۳٬۸۳۹ به ۸٬۸۰۰٬۶۳۸ تومان (۶۵٪ گران‌تر)، رم از ۱۶ به ۳۲ گیگ" in text
    assert text.endswith(price_feed.PAGE)

    cheaper = change(before, "NGP-large80", price_toman=D(5000000))
    assert "قیمت ۱ پلن را پایین آورد" in price_feed.message(price_feed.event("iranserver", before, cheaper, AT), "ایران‌سرور")


def test_component_prices_are_rates_and_cpu_comes_before_storage(rows):
    before = rows["arvancloud"]
    after = [i.model_copy(update={"price_toman": i.price_toman * D("1.2")}) if i.price_toman else i for i in before]
    e = price_feed.event("arvancloud", before, after, AT, source="file")
    lines = price_feed.message(e, "ابر آروان").splitlines()
    assert "تعرفه را بالا برد" in lines[0] and lines[1].startswith("IR-CPU-BASIC: از ۲۱۰٬۰۰۰ به ۲۵۲٬۰۰۰ تومان (۲۰٪ گران‌تر)")
    assert any("تعرفه دیگر" in l for l in lines) and "(قیمت‌ها دستی از صفحه رسمی خوانده شده)" in lines


class Sent:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, **kw):
        self.calls.append((url, kw["json"]))
        if self.fail:
            raise OSError("no route")
        return self

    def raise_for_status(self):
        pass


def test_each_event_is_posted_once_to_every_service_that_is_set_up(rows):
    before = rows["iranserver"]
    data = {"schema": 1, "events": []}
    price_feed.add(data, [price_feed.event("iranserver", before, change(before, "NGP-small40", price_toman=D(1)), AT)])
    config = {"telegram": {"token": "T", "chat_id": "@prices"}, "bale": {"token": "", "chat_id": ""}}
    send = Sent()

    assert price_feed.post_pending(data, config, {"iranserver": "ایران‌سرور"}, now=AT, send=send) == 1
    assert price_feed.post_pending(data, config, {"iranserver": "ایران‌سرور"}, now=AT, send=send) == 0
    url, body = send.calls[0]
    assert url == "https://api.telegram.org/botT/sendMessage" and body["chat_id"] == "@prices"
    assert body["text"].startswith("ایران‌سرور قیمت ۱ پلن را پایین آورد") and len(send.calls) == 1

    # Bale is set up later: it gets the event too, but not one from before its first day and a half
    config["bale"] = {"token": "B", "chat_id": "42"}
    old = price_feed.event("iranserver", before, change(before, "NGP-small40", price_toman=D(2)), AT - timedelta(days=5))
    price_feed.add(data, [old])
    assert price_feed.post_pending(data, config, {}, now=AT, send=send) == 1
    assert send.calls[-1][0] == "https://tapi.bale.ai/botB/sendMessage"
    assert data["events"][0]["posted"] == {"telegram": "skipped", "bale": "skipped"}


def test_a_failed_post_is_tried_again(rows):
    before = rows["iranserver"]
    data = {"schema": 1, "events": []}
    price_feed.add(data, [price_feed.event("iranserver", before, change(before, "NGP-small40", price_toman=D(1)), AT)])
    config = {"telegram": {"token": "T", "chat_id": "@prices"}}
    assert price_feed.post_pending(data, config, {}, now=AT, send=Sent(fail=True)) == 0
    assert data["events"][0]["posted"] == {}
    assert price_feed.post_pending(data, config, {}, now=AT, send=Sent()) == 1


def test_the_published_list_is_newest_first_and_keeps_counts_when_it_cuts_lines(rows):
    before = rows["arvancloud"]
    after = [i.model_copy(update={"price_toman": i.price_toman * D("1.2")}) if i.price_toman else i for i in before]
    data = {"schema": 1, "events": []}
    price_feed.add(data, [price_feed.event("arvancloud", before, after, AT - timedelta(days=1)),
                          price_feed.event("iranserver", rows["iranserver"], rows["iranserver"][1:], AT)])
    out = json.loads(price_feed.published(data, {"arvancloud": {"name_fa": "ابر آروان", "name_en": "ArvanCloud"}}))
    assert [e["provider_id"] for e in out["events"]] == ["iranserver", "arvancloud"]
    arvan = out["events"][1]
    # a long event is cut for the page, and still says how many rows there were
    assert len(arvan["changed"]) == price_feed.PAGE_LINES < arvan["counts"]["changed"] and "posted" not in arvan
    # which way the event went and by about how much come worked out, and each row has its own percent
    assert (arvan["direction"], arvan["typical_percent"], arvan["changed"][0]["percent"]) == ("up", "20", "20")
    assert (out["events"][0]["direction"], out["events"][0]["counts"]["removed"]) == (None, 1)



def test_each_change_says_what_came_before_it(rows):
    """ArvanCloud's prices of 24 November replaced ones on record since 4 October: the page says so, rather than
    letting a change after two quiet months read like one day's news. A later change names the one before it."""
    before = rows["arvancloud"]
    up = [i.model_copy(update={"price_toman": i.price_toman * D("1.2")}) if i.price_toman else i for i in before]
    up_again = [i.model_copy(update={"price_toman": i.price_toman * D("1.1")}) if i.price_toman else i for i in up]
    data = {"schema": 1, "events": []}
    price_feed.add(data, [price_feed.event("arvancloud", before, up, AT, since="2026-10-04", source="file"),
                          price_feed.event("arvancloud", up, up_again, AT + timedelta(days=3), since="2026-11-26")])
    later, first = json.loads(price_feed.published(data, {}))["events"]
    assert (first["previous"], first["previous_kind"], first["held_days"]) == ("2026-10-04", "first_record", 51)
    assert (later["previous"], later["previous_kind"], later["held_days"]) == ("2026-11-24", "change", 3)


def test_a_provider_that_joins_the_page_is_news_of_its_own():
    """HostIran, with the 124 plans of its Iranian clusters, priced by HostIran itself on 11 November."""
    plans = list(load_seed_snapshot().provider_items("hostiran"))
    e = price_feed.added("hostiran", plans, AT)
    assert (e["source"], len(e["new"]), e["changed"], e["price_date"]) == ("added", 124, [], "2026-11-11")
    assert price_feed.message(e, "هاست‌ایران").startswith("هاست‌ایران به صفحه قیمت صرفه اضافه شد، با ۱۲۴ پلن")
    out = json.loads(price_feed.published({"schema": 1, "events": [e]}, {}))["events"][0]
    assert (out["previous"], out["held_days"], out["price_date"]) == (None, None, "2026-11-11")
