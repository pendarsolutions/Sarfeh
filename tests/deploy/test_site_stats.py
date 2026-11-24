"""The website's visit counts (deploy/sarfeh-site/stats.py): people counted once a day, and nothing kept about them."""

import importlib.util
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("sarfeh_site_stats", ROOT / "deploy" / "sarfeh-site" / "stats.py")
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
PHONE = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36"
IMESSAGE = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_1) AppleWebKit/601.2.4 (KHTML, like Gecko) Version/9.0.1 "
            "Safari/601.2.4 facebookexternalhit/1.1 Facebot Twitterbot/1.0")
PRICES, HOME = "/sarfeh/prices/", "/sarfeh/"
NOON = "2026-11-21T08:30:00+00:00"  # midday in Tehran


def line(uri=PRICES, ua=CHROME, ip="5.160.10.0", ref="", cc="IR", status=200, t=NOON, method="GET"):
    """A request as site.conf logs it."""
    return json.dumps({"t": t, "ip": ip, "cc": cc, "m": method, "u": uri, "s": status, "r": ref, "a": ua})


def old_line(uri=PRICES, ua=CHROME, ip="5.160.10.7", ref="-", status=200, t="21/Nov/2026:08:30:00 +0000"):
    """A request as the container logged it before, in nginx's stock format."""
    return f'172.18.0.5 - - [{t}] "GET {uri} HTTP/1.1" {status} 827 "{ref}" "{ua}" "{ip}"'


def counted(*lines, day="2026-11-21"):
    state = stats.new_state()
    stats.ingest(state, lines)
    return stats.summary(state["open"][day]) if day in state["open"] else None


def test_both_log_formats_are_read_and_anything_else_is_skipped():
    new, old = stats.parse(line(ref="https://github.com/")), stats.parse(old_line(ref="https://github.com/"))
    assert new["time"] == old["time"] and new["uri"] == old["uri"] == PRICES and new["status"] == old["status"] == 200
    assert (new["referer"], new["ua"]) == (old["referer"], old["ua"]) == ("https://github.com/", CHROME)
    assert stats.parse(old_line(ip="-"))["ip"] == ""  # the health check: it never comes through the edge
    for junk in ("", "2026/11/21 08:30:00 [notice] 1#1: start worker processes", '{"t": "not a time"}',
                 '1.2.3.4 - - [21/Nov/2026:08:30:00 +0000] "\\x16\\x03\\x01" 400 157 "-" "-" "-"'):
        assert stats.parse(junk) is None, junk


def test_a_person_is_counted_once_a_day_however_many_pages_they_open():
    c = counted(line(), line(HOME), line(), line(ua=PHONE))
    assert c["visitors"] == 2 and c["views"] == {"prices": 3, "home": 1}
    assert c["devices"] == {"desktop": 1, "phone": 1} and c["countries"] == {"IR": 2}
    # an address is only ever used without its last part, in either log format
    assert counted(old_line(ip="5.160.10.7"), old_line(ip="5.160.10.200"), line(ip="5.160.10.0"))["visitors"] == 1
    assert stats.short_address("2a01:4f8:c17:8a1::2") == stats.short_address("2a01:4f8:c17::") == "2a01:4f8:c17::"


def test_the_health_check_and_automated_requests_arent_visitors():
    c = counted(old_line(ip="-", ua="Wget"), line(ua="curl/8.21.0"), line(ua="Mozilla/5.0 (compatible; Googlebot/2.1)"),
                line(ua=""), line(status=404), line(method="HEAD"), line(uri="/sarfeh/assets/site.js"), line())
    assert c["visitors"] == 1 and c["automated"] == 3


def test_a_link_pasted_into_a_post_or_chat_shows_as_that_apps_preview():
    c = counted(line(ua="LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)"),
                line(ua="TelegramBot (like TwitterBot)"), line(ua="TelegramBot (like TwitterBot)"), line(ua=IMESSAGE))
    assert c["previews"] == {"LinkedIn": 1, "Telegram": 2, stats.APPLE: 1} and c["visitors"] == 0


@pytest.mark.parametrize(("referer", "expected"), [
    ("https://www.linkedin.com/feed/", "linkedin.com"),
    ("android-app://com.linkedin.android/", "linkedin.com"),
    ("http://lnkd.in/", "linkedin.com"),
    ("android-app://org.telegram.messenger/", "telegram.org"),
    ("https://www.google.co.uk/", "google.com"),
    ("https://github.com/pendarsolutions/Sarfeh", "github.com"),
    ("https://pendarsolutions.ir/", "pendarsolutions.ir"),
    ("https://pendarsolutions.ir/sarfeh/", None),  # a move within the site is not where they came from
    ("", ""),
    ("https://evil.example/<script>alert(1)</script>", "evil.example"),
])
def test_where_a_visit_came_from(referer, expected):
    assert stats.source(referer) == expected


def test_a_visitors_source_is_the_first_place_they_came_from_that_day():
    c = counted(line(HOME, ref="https://www.linkedin.com/"), line(PRICES, ref="https://pendarsolutions.ir/sarfeh/"),
                line(ua=PHONE), line(ua=PHONE, ref="https://github.com/"))
    assert c["sources"] == {"linkedin.com": 1, "github.com": 1}


def test_arriving_on_a_comparison_is_told_from_reloading_the_page():
    c = counted(line(PRICES + "?cpu=4&ram=8&disk=100"), line(PRICES + "?lang=en&cpu=8&ram=16&disk=200"),
                line(PRICES + "?cpu=2&ram=4&disk=50", ua=PHONE), line(PRICES + "?cpu=x&ram=8&disk=100", ip="9.9.9.0"),
                line(PRICES + "?cpu=999999&ram=8&disk=100", ip="9.9.9.0"),
                # the page writes the size on screen into its own address, so this visitor's reload names one too
                line(ip="7.7.7.0"), line(PRICES + "?cpu=2&ram=4&disk=50", ip="7.7.7.0"))
    assert c["sizes"] == {"4-8-100": 1, "2-4-50": 1} and c["shared"] == 2 and c["visitors"] == 4


def test_reading_the_price_data_directly_is_counted_apart_from_the_page():
    data = "/sarfeh/prices.json"
    c = counted(line(), line(data, ref="https://pendarsolutions.ir/sarfeh/prices/"),  # the page loading its table
                line(data, ua="curl/8.21.0"), line(data, ua="python-requests/2.32", ip="9.9.9.0"),
                line(data, ref="https://example.org/dashboard"), line(data, ua="Googlebot/2.1"))
    assert c["data"] == 3 and c["visitors"] == 1


def test_whoever_opens_the_panel_is_left_out_of_that_days_counts():
    c = counted(line(), line(HOME), line("/sarfeh/stats/"), line(ua=PHONE),
                line("/sarfeh/stats/", ip="9.9.9.0", status=401))  # a wrong password marks nobody
    assert (c["visitors"], c["own"], c["views"]) == (1, 2, {"prices": 1})


def test_days_are_tehran_days_and_a_finished_day_keeps_counts_only():
    state = stats.new_state()
    stats.ingest(state, [line(t="2026-11-20T20:29:59+00:00"),  # 23:59:59 in Tehran
                         line(t="2026-11-20T20:30:00+00:00", ref="https://www.linkedin.com/")])  # midnight
    assert sorted(state["open"]) == ["2026-11-20", "2026-11-21"]
    stats.close_days(state, date(2026, 11, 21))
    assert sorted(state["open"]) == ["2026-11-21"] and state["days"]["2026-11-20"]["visitors"] == 1
    stats.close_days(state, date(2026, 11, 22))
    assert state["open"] == {} and state["days"]["2026-11-21"]["sources"] == {"linkedin.com": 1}
    kept = json.dumps(state)
    assert "5.160" not in kept and "Mozilla" not in kept and "key" not in kept and "visitors\": {" not in kept


def test_the_panel_shows_the_counts_and_carries_no_inline_script(tmp_path):
    state = stats.new_state()
    stats.ingest(state, [line(t="2026-11-19T08:00:00+00:00", ref="https://www.linkedin.com/"),
                         line(t="2026-11-19T09:00:00+00:00", ua=PHONE, ref="https://evil.example/<script>alert(1)</script>"),
                         line(PRICES + "?cpu=4&ram=8&disk=100", ua=PHONE)])
    now = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    stats.close_days(state, now.astimezone(stats.TEHRAN).date())
    state["github"] = {"now": {"stars": 7, "forks": 1, "issues": 2, "installer": 12, "bundle": 3},
                       "releases": [{"tag": "v0.4.0", "installer": 12, "bundle": 3}]}
    stats.write_panel(state, now, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["all.html", "index.html", "panel.js", "week.html"]
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<b>3</b>" in page and "1 visitor today" in page  # three in the period, one of them today
    assert "LinkedIn" in page and "evil.example" in page and "4 vCPU, 8 GB memory, 100 GB disk" in page
    assert "Thu 19 Nov|2 visitors|2 page views" in page and "v0.4.0" in page and "Installer downloads" in page
    # the site's content policy allows scripts only from its own files, and a referrer can't put markup on the page
    assert re.findall(r"<script(?![^>]*\bsrc=)", page) == [] and "<script>alert" not in page
    assert 'href="index.html" aria-current="page"' in page


def test_github_is_read_at_most_once_an_hour_and_a_failure_keeps_the_last_figures():
    calls = []

    def fetch(path, token):
        calls.append(path)
        if path.endswith("/releases?per_page=30"):
            return [{"tag_name": "v0.4.0", "assets": [{"name": "install.sh", "download_count": 9},
                                                      {"name": "sarfeh-0.4.0.tar.gz", "download_count": 2}]},
                    {"tag_name": "v0.3.1", "assets": [{"name": "install.sh", "download_count": 3}]}]
        return {"stargazers_count": 5, "forks_count": 1, "open_issues_count": 0}

    state = stats.new_state()
    stats.read_github(state, datetime(2026, 11, 21, 9, 0, tzinfo=UTC), fetch=fetch)
    assert state["github"]["now"] == {"stars": 5, "forks": 1, "issues": 0, "installer": 12, "bundle": 2}
    assert state["github"]["by_day"]["2026-11-21"]["installer"] == 12 and len(calls) == 2
    stats.read_github(state, datetime(2026, 11, 21, 9, 30, tzinfo=UTC), fetch=fetch)
    assert len(calls) == 2  # too soon to ask again

    def broken(path, token):
        raise TimeoutError("no answer")

    stats.read_github(state, datetime(2026, 11, 21, 11, 0, tzinfo=UTC), fetch=broken)
    assert state["github"]["now"]["stars"] == 5 and "TimeoutError" in state["github"]["error"]


def test_the_log_is_taken_from_nginx_and_put_back_if_nginx_doesnt_answer(tmp_path, monkeypatch):
    log = tmp_path / "access.log"
    log.write_text(line() + "\n", encoding="utf-8")
    answers = iter([1, 0])
    monkeypatch.setattr(stats.time, "sleep", lambda s: None)
    monkeypatch.setattr(stats.subprocess, "run", lambda *a, **k: type("Done", (), {"returncode": next(answers),
                                                                                    "stderr": b"no such container"})())
    with pytest.raises(SystemExit, match="no such container"):
        stats.take_log(log, "sarfeh-site")
    assert log.exists() and not (tmp_path / "access.log.1").exists()  # still nginx's to write to
    taken = stats.take_log(log, "sarfeh-site")
    assert taken == tmp_path / "access.log.1" and not log.exists()
    assert stats.take_log(log, "sarfeh-site") == taken  # a run cut short finishes that file first
    taken.unlink()
    assert stats.take_log(log, "sarfeh-site") is None  # nothing was logged
