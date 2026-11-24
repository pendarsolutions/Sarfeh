#!/usr/bin/env python3
"""Visit counts for pendarsolutions.ir/sarfeh/, read from the web server's own log: the owner's private panel.

sarfeh-stats.timer runs this every ten minutes (see README.md). A run takes the access log away from nginx, adds
what is in it to the counts in state/stats.json, deletes it, and writes the panel's pages into stats/. Nothing runs
in a visitor's browser and no cookie is set.

What is kept is counts per day. To count a person once a day, a visitor is the pair (address without its last part,
browser), hashed with a random key that is thrown away when the day ends: the counts can't be traced back to anyone,
or joined from one day to the next. Automated requests are counted apart. So are the owner's own visits: whoever
opens the panel is left out of that day's counts.

Standard library only, so it runs with the server's own python3.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

TEHRAN = timezone(timedelta(hours=3, minutes=30))  # days are counted as visitors live them
SITE_HOST = "pendarsolutions.ir"
BASE = "/sarfeh/"
REPO = "pendarsolutions/Sarfeh"
CONTAINER = "sarfeh-site"
PAGES = {BASE: "home", BASE + "prices/": "prices"}
PAGE_NAMES = {"prices": "Price comparison", "home": "Home page"}

#: apps that fetch a page once to draw a link preview, when someone pastes the link. Checked in this order.
PREVIEWS = (("linkedinbot", "LinkedIn"), ("telegrambot", "Telegram"), ("whatsapp", "WhatsApp"), ("slackbot", "Slack"),
            ("discordbot", "Discord"), ("skypeuripreview", "Skype"), ("twitterbot", "X"),
            ("facebookexternalhit", "Facebook"))
APPLE = "Apple apps"
CRAWLER = re.compile(r"bot\b|bot/|crawl|spider|slurp|archiver|bingpreview", re.I)
#: crawlers, and programs that aren't a person looking at the page
AUTOMATED = re.compile(CRAWLER.pattern + r"|curl|wget|python|httpx|go-http|java/|okhttp|headless|lighthouse|pingdom"
                       r"|uptime|monitor|scan|axios|node-fetch|libwww|scrapy|http-?client|feed|postman|insomnia", re.I)
SOURCES = {"lnkd.in": "linkedin.com", "com.linkedin.android": "linkedin.com", "t.co": "x.com",
           "twitter.com": "x.com", "org.telegram.messenger": "telegram.org", "t.me": "telegram.org",
           "web.telegram.org": "telegram.org", "com.google.android.gm": "mail.google.com"}
SOURCE_NAMES = {"": "Direct, or not told", "linkedin.com": "LinkedIn", "github.com": "GitHub", "x.com": "X",
                "telegram.org": "Telegram", "google.com": "Google", "mail.google.com": "Gmail",
                SITE_HOST: "pendarsolutions.ir (other pages)"}
COUNTRIES = {"IR": "Iran", "DE": "Germany", "US": "United States", "NL": "Netherlands", "GB": "United Kingdom",
             "FR": "France", "TR": "Turkey", "AE": "United Arab Emirates", "CA": "Canada", "FI": "Finland",
             "AT": "Austria", "SE": "Sweden", "T1": "Tor", "": "Not recorded"}
DEVICES = {"phone": "Phone", "desktop": "Computer", "tablet": "Tablet"}

# nginx's stock "main" line (what the container wrote before this log existed), and the JSON line site.conf writes now
MAIN_LINE = re.compile(r'^(\S+) - \S+ \[([^\]]+)\] "(\S+) (\S+) [^"]*" (\d{3}) \d+ "([^"]*)" "([^"]*)" "([^"]*)"$')


# --------------------------------------------------------------------------- reading the log


def parse(line: str) -> dict[str, Any] | None:
    """One request, or None for anything else in the log (nginx's own notices, garbage sent by scanners)."""
    line = line.strip()
    try:
        if line.startswith("{"):
            j = json.loads(line)
            return {"time": datetime.fromisoformat(j["t"]), "ip": j["ip"], "cc": j.get("cc", ""), "method": j["m"],
                    "uri": j["u"], "status": int(j["s"]), "referer": j.get("r", ""), "ua": j.get("a", "")}
        m = MAIN_LINE.match(line)
        if m is None:
            return None
        _, when, method, uri, status, referer, ua, forwarded = m.groups()
        return {"time": datetime.strptime(when, "%d/%b/%Y:%H:%M:%S %z"), "ip": "" if forwarded == "-" else forwarded,
                "cc": "", "method": method, "uri": uri, "status": int(status),
                "referer": "" if referer == "-" else referer, "ua": "" if ua == "-" else ua}
    except (ValueError, KeyError, TypeError):
        return None


def short_address(ip: str) -> str:
    """An address without its last part (a /24, or a /48 for IPv6), as site.conf already writes it."""
    ip = ip.split(",")[0].strip()
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + "::"
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".0" if len(parts) == 4 else ip


def preview_app(ua: str) -> str | None:
    low = ua.lower()
    if "facebookexternalhit" in low and "twitterbot" in low:
        return APPLE  # Apple's link previews name both: Messages, and other apps on an iPhone or a Mac
    return next((name for needle, name in PREVIEWS if needle in low), None)


def device(ua: str) -> str:
    if re.search(r"ipad|tablet", ua, re.I):
        return "tablet"
    return "phone" if re.search(r"mobile|iphone|android", ua, re.I) else "desktop"


def source(referer: str) -> str | None:
    """Where a visit came from: a site's name, "" when the browser didn't say, None for a move within the site."""
    if not referer:
        return ""
    parts = urlsplit(referer)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host == SITE_HOST:
        return None if parts.path.startswith(BASE) else SITE_HOST
    host = SOURCES.get(host, host)
    if host.endswith(".linkedin.com"):
        host = "linkedin.com"
    if re.fullmatch(r"(www\.)?google\.[a-z.]{2,6}", host):
        host = "google.com"
    return host[:60] if re.fullmatch(r"[a-z0-9.-]+", host) else ""


def size_asked(query: str) -> str | None:
    """The server size named in a link to the price page, such as "4-8-100", when someone opened a shared one."""
    q = parse_qs(query)
    try:
        cpu, ram, disk = (int(q[k][0]) for k in ("cpu", "ram", "disk"))
    except (KeyError, ValueError):
        return None
    return f"{cpu}-{ram}-{disk}" if 0 < cpu <= 512 and 0 < ram <= 4096 and 0 < disk <= 100_000 else None


# --------------------------------------------------------------------------- counting


def new_state() -> dict[str, Any]:
    return {"version": 1, "days": {}, "open": {}, "github": {}}


def record(state: dict[str, Any], hit: dict[str, Any]) -> None:
    """Add one request to its day. Days still open keep a line per visitor, so the same person is counted once."""
    if not hit["ip"] or hit["method"] != "GET" or hit["status"] not in (200, 304):
        return  # the health check never comes through the edge; a refused sign-in to the panel is a 401
    parts = urlsplit(hit["uri"])
    path, ua = parts.path, hit["ua"]
    day = state["open"].setdefault(hit["time"].astimezone(TEHRAN).date().isoformat(),
                                   {"key": secrets.token_hex(16), "visitors": {}, "previews": {}, "data": 0,
                                    "automated": 0})
    who = hashlib.sha256(f"{day['key']}|{short_address(hit['ip'])}|{ua}".encode()).hexdigest()[:16]

    if path.startswith(BASE + "stats/"):
        day["visitors"].setdefault(who, {})["owner"] = True
        return
    if path == BASE + "prices.json":
        # the price page fetches it too, and names itself as the referrer; anything else read the open data directly
        if source(hit["referer"]) is not None and not CRAWLER.search(ua):
            day["data"] += 1
        return
    page = PAGES.get(path)
    if page is None:
        return
    app = preview_app(ua)
    if app:
        day["previews"][app] = day["previews"].get(app, 0) + 1
        return
    if not ua or AUTOMATED.search(ua):
        day["automated"] += 1
        return
    v = day["visitors"].setdefault(who, {})
    arriving = not v.get("views")  # their first page of the day
    v.setdefault("views", {})
    v["views"][page] = v["views"].get(page, 0) + 1
    src = source(hit["referer"])
    if src is not None and not v.get("src"):
        v["src"] = src  # the first place they came from that day; "" until a referrer names one
    v.setdefault("cc", hit["cc"].upper() if re.fullmatch(r"[A-Za-z0-9]{2}", hit["cc"]) else "")
    v.setdefault("dev", device(ua))
    # The price page writes the size on screen into its address, so a reload names a size too. Only a visit that
    # starts on one came by a link to that comparison (or by coming back to it).
    asked = size_asked(parts.query) if page == "prices" and arriving else None
    if asked:
        v["sizes"] = [asked]


def _count(into: dict[str, int], key: str, n: int = 1) -> None:
    into[key] = into.get(key, 0) + n


def summary(day: dict[str, Any]) -> dict[str, Any]:
    """A day's counts, with nothing in them about any one visitor."""
    out: dict[str, Any] = {"visitors": 0, "views": {}, "sources": {}, "countries": {}, "devices": {}, "sizes": {},
                           "shared": 0, "previews": dict(day["previews"]), "data": day["data"],
                           "automated": day["automated"], "own": 0}
    for v in day["visitors"].values():
        if v.get("owner"):
            out["own"] += sum(v.get("views", {}).values())
            continue
        if not v.get("views"):
            continue
        out["visitors"] += 1
        for page, n in v["views"].items():
            _count(out["views"], page, n)
        _count(out["sources"], v.get("src") or "")
        _count(out["countries"], v.get("cc", ""))
        _count(out["devices"], v.get("dev", "desktop"))
        for size in v.get("sizes", []):
            _count(out["sizes"], size)
        out["shared"] += 1 if v.get("sizes") else 0
    return out


def _nothing() -> dict[str, Any]:
    """The counts of a day nobody came."""
    return summary({"visitors": {}, "previews": {}, "data": 0, "automated": 0})


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    for k, v in b.items():
        if isinstance(v, dict):
            for kk, n in v.items():
                _count(a.setdefault(k, {}), kk, n)
        else:
            a[k] = a.get(k, 0) + v
    return a


def close_days(state: dict[str, Any], today: date) -> None:
    """Days that are over keep their counts only: the per-visitor lines and the day's key are dropped."""
    for iso in sorted(state["open"]):
        if date.fromisoformat(iso) < today:
            done = summary(state["open"].pop(iso))
            state["days"][iso] = _merge(state["days"][iso], done) if iso in state["days"] else done


def ingest(state: dict[str, Any], lines: Iterable[str]) -> int:
    requests = 0
    for line in lines:
        hit = parse(line)
        if hit is not None:
            record(state, hit)
            requests += 1
    return requests


def days_between(state: dict[str, Any], first: date, last: date) -> list[tuple[date, dict[str, Any]]]:
    """Every day from first to last with its counts (zeros for a day nobody came), open days included."""
    out = []
    for i in range((last - first).days + 1):
        d = first + timedelta(days=i)
        iso = d.isoformat()
        counts = _merge(_nothing(), state["days"].get(iso, {}))
        if iso in state["open"]:
            _merge(counts, summary(state["open"][iso]))
        out.append((d, counts))
    return out


# --------------------------------------------------------------------------- GitHub


def _github(path: str, token: str | None) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sarfeh-stats"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(f"https://api.github.com{path}", headers=headers),
                                timeout=20) as r:
        return json.load(r)


def read_github(state: dict[str, Any], now: datetime, token: str | None = None, fetch=_github) -> None:
    """Stars, forks, and how often each release's installer was downloaded; at most once an hour. With a token that
    may read the repository's traffic, also who looked at the repository (GitHub keeps only two weeks of that)."""
    gh = state.setdefault("github", {})
    last = gh.get("read_at")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=55):
        return
    try:
        repo = fetch(f"/repos/{REPO}", token)
        releases = fetch(f"/repos/{REPO}/releases?per_page=30", token)
        rows = []
        for r in releases:
            assets = {a["name"]: a["download_count"] for a in r.get("assets", [])}
            rows.append({"tag": r["tag_name"], "installer": assets.get("install.sh", 0),
                         "bundle": sum(n for name, n in assets.items() if name.endswith(".tar.gz"))})
        today = {"stars": repo["stargazers_count"], "forks": repo["forks_count"], "issues": repo["open_issues_count"],
                 "installer": sum(r["installer"] for r in rows), "bundle": sum(r["bundle"] for r in rows)}
        gh.update(now=today, releases=rows, read_at=now.isoformat(), error=None)
        gh.setdefault("by_day", {})[now.astimezone(TEHRAN).date().isoformat()] = today
        if token:
            for kind in ("views", "clones"):
                for row in fetch(f"/repos/{REPO}/traffic/{kind}", token).get(kind, []):
                    gh.setdefault("traffic", {}).setdefault(row["timestamp"][:10], {}).update(
                        {kind: row["count"], f"{kind}_unique": row["uniques"]})
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, TypeError) as exc:
        gh["error"] = f"{type(exc).__name__}: {exc}"[:200]


# --------------------------------------------------------------------------- the panel

RANGES = (("week.html", "Last 7 days", 7), ("index.html", "Last 30 days", 30), ("all.html", "All time", None))

STYLE = """
:root { color-scheme: light; --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --series:#2a78d6; --series-hover:#1c5cab; --ring:rgba(11,11,11,.10); }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec;
  --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --axis:#383835; --series:#3987e5; --series-hover:#6da7ec;
  --ring:rgba(255,255,255,.10); } }
* { box-sizing: border-box; }
body { margin:0; background:var(--page); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1040px; margin:0 auto; padding:24px 16px 56px; }
h1 { font-size:20px; margin:0; } h2 { font-size:15px; margin:0 0 12px; }
p { margin:0; } a { color:inherit; }
.top { display:flex; flex-wrap:wrap; gap:8px 24px; align-items:baseline; justify-content:space-between; }
.muted { color:var(--ink2); font-size:13px; }
.ranges { display:flex; gap:4px; margin:16px 0 20px; }
.ranges a { padding:6px 12px; border-radius:6px; text-decoration:none; color:var(--ink2); }
.ranges a:hover { background:var(--ring); }
.ranges a[aria-current] { background:var(--ink); color:var(--page); font-weight:600; }
.card { background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:16px; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-top:12px; }
.lead { display:grid; gap:12px; grid-template-columns:minmax(220px,1fr) 2fr; }
.hero b { display:block; font-size:56px; line-height:1.1; font-weight:600; }
.tiles { display:grid; gap:12px; grid-template-columns:repeat(3,minmax(0,1fr)); }
.tile b { display:block; font-size:24px; font-weight:600; line-height:1.3; }
.tile span, .hero span { color:var(--ink2); font-size:13px; }
.scroll { overflow-x:auto; } svg { display:block; min-width:520px; width:100%; height:auto; }
svg text { fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
svg text.value { fill:var(--ink); font-weight:600; }
.bar { fill:var(--series); } .col:hover .bar, .col:focus-within .bar { fill:var(--series-hover); }
.hit { fill:transparent; outline:none; } .hit:focus-visible { stroke:var(--ink); stroke-width:1; }
.rank { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
.rank li { display:grid; grid-template-columns:minmax(0,11em) 1fr; gap:12px; align-items:center; }
.rank .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rank .track { display:flex; align-items:center; gap:8px; min-width:0; }
.rank .fill { height:12px; min-width:2px; background:var(--series); border-radius:0 4px 4px 0; }
.rank .val { font-variant-numeric:tabular-nums; font-weight:600; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--grid); white-space:nowrap; }
th:first-child, td:first-child { text-align:left; white-space:normal; }
th { color:var(--ink2); font-weight:600; font-size:13px; }
details { margin-top:12px; } summary { cursor:pointer; color:var(--ink2); font-size:13px; }
.notes { margin-top:12px; } .notes li { margin:4px 0; color:var(--ink2); font-size:13px; }
#tip { position:fixed; z-index:9; pointer-events:none; background:var(--ink); color:var(--page); padding:8px 10px;
  border-radius:6px; font-size:13px; line-height:1.4; } #tip b { display:block; font-size:15px; }
@media (max-width:700px) { .lead { grid-template-columns:1fr; } .hero b { font-size:48px; }
  .tiles { grid-template-columns:repeat(2,minmax(0,1fr)); } }
"""

# The hover layer: the same figures are in the table under each chart, so this only saves a click.
SCRIPT = """
(function () {
  var tip = document.createElement("div");
  tip.id = "tip"; tip.hidden = true; document.body.appendChild(tip);
  function show(el, x, y) {
    var parts = el.getAttribute("data-tip").split("|");
    tip.textContent = "";
    parts.forEach(function (text, i) {
      var line = document.createElement(i === 1 ? "b" : "div");
      line.textContent = text; tip.appendChild(line);
    });
    tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(x - w / 2, window.innerWidth - w - 8)) + "px";
    tip.style.top = (y - h - 12 < 8 ? y + 16 : y - h - 12) + "px";
  }
  function hide() { tip.hidden = true; }
  document.querySelectorAll("[data-tip]").forEach(function (el) {
    el.addEventListener("pointermove", function (e) { show(el, e.clientX, e.clientY); });
    el.addEventListener("pointerleave", hide);
    el.addEventListener("focus", function () { var r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top + 24); });
    el.addEventListener("blur", hide);
  });
  // on a narrow screen the chart scrolls sideways: start at its newest day
  document.querySelectorAll(".scroll").forEach(function (el) {
    if (el.querySelector("svg")) el.scrollLeft = el.scrollWidth;
  });
})();
"""

esc = html.escape


def n(value: int) -> str:
    return f"{value:,}"


def plural(value: int, one: str, many: str | None = None) -> str:
    return f"{n(value)} {one if value == 1 else many or one + 's'}"


def day_name(d: date, weekday: bool = True) -> str:
    """Sat 21 Nov. Written out, because strftime's day without a leading zero differs between systems."""
    return f"{d:%a} {d.day} {d:%b}" if weekday else f"{d.day} {d:%b}"


def _ceiling(top: int) -> tuple[int, int]:
    """A round axis top at or above the largest value, and the step between its gridlines."""
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10_000, 25_000, 50_000, 100_000):
        if step * 4 >= max(top, 1):
            return step * 4, step
    return top, max(top // 4, 1)


def column_chart(days: list[tuple[date, dict[str, Any]]]) -> str:
    """Visitors per day: one thin column a day on a single baseline, and the two columns worth naming labelled."""
    width, height, left, right, top, bottom = 760, 230, 40, 12, 18, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    base = top + plot_h
    values = [c["visitors"] for _, c in days]
    axis_top, step = _ceiling(max(values, default=0))
    band = plot_w / max(len(days), 1)
    bar_w = max(2.0, min(24.0, band - 2))  # never thicker than 24, and always a gap between neighbours
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Visitors per day">']
    for tick in range(0, axis_top + 1, step):
        y = base - plot_h * tick / axis_top
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" '
                   f'stroke="var(--{"axis" if tick == 0 else "grid"})" stroke-width="1"/>'
                   f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{n(tick)}</text>')
    every = max(1, -(-len(days) // 7))
    peak = max(range(len(values)), key=values.__getitem__) if any(values) else None
    for i, (d, c) in enumerate(days):
        x = left + band * i + (band - bar_w) / 2
        h = plot_h * c["visitors"] / axis_top
        r = min(4.0, bar_w / 2, h)
        y = base - h
        tip = f'{day_name(d)}|{plural(c["visitors"], "visitor")}|{plural(sum(c["views"].values()), "page view")}'
        mark = (f'<path class="bar" d="M{x:.1f},{base} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base} Z"/>') if h else ""
        out.append(f'<g class="col">{mark}<rect class="hit" x="{left + band * i:.1f}" y="{top}" width="{band:.1f}" '
                   f'height="{plot_h}" tabindex="0" data-tip="{esc(tip)}"><title>{esc(tip.replace("|", ", "))}</title>'
                   f'</rect></g>')
        if i % every == 0 or i == len(days) - 1:
            out.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" text-anchor="middle">'
                       f'{day_name(d, weekday=False)}</text>')
        if c["visitors"] and i in (peak, len(days) - 1):
            out.append(f'<text class="value" x="{x + bar_w / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle">'
                       f'{n(c["visitors"])}</text>')
    return "".join(out) + "</svg>"


def ranked(rows: dict[str, int], names: dict[str, str] | None = None, limit: int = 8) -> str:
    """One bar per row on a shared baseline, largest first, the figure at the bar's tip; the tail folds into Other."""
    names = names or {}
    order = sorted(rows.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(order) > limit:
        order = order[:limit - 1] + [("\0other", sum(v for _, v in order[limit - 1:]))]
    if not order:
        return '<p class="muted">Nothing yet.</p>'
    most = max(v for _, v in order)
    items = []
    for key, value in order:
        name = "Other" if key == "\0other" else names.get(key, key)
        items.append(f'<li><span class="name" title="{esc(name)}">{esc(name)}</span><span class="track">'
                     f'<span class="fill" style="width:calc((100% - 5ch) * {value / most:.4f})"></span>'
                     f'<span class="val">{n(value)}</span></span></li>')
    return f'<ol class="rank">{"".join(items)}</ol>'


def _table(head: list[str], rows: list[list[str]]) -> str:
    cells = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (f'<div class="scroll"><table><thead><tr>{"".join(f"<th>{h}</th>" for h in head)}</tr></thead>'
            f'<tbody>{cells}</tbody></table></div>')


def page(state: dict[str, Any], now: datetime, label: str, span: int | None) -> str:
    today = now.astimezone(TEHRAN).date()
    known = sorted([*state["days"], *state["open"]])
    started = date.fromisoformat(known[0]) if known else today
    first = started if span is None else today - timedelta(days=span - 1)
    days = days_between(state, first, today)
    total = _nothing()
    for _, c in days:
        _merge(total, c)
    views = sum(total["views"].values())
    here = days[-1][1]
    gh = state.get("github", {})
    now_gh = gh.get("now")
    before = gh.get("by_day", {}).get((first - timedelta(days=1)).isoformat())

    def since(key: str) -> str:
        if not now_gh:
            return ""
        return f"{now_gh[key] - before[key]:+,} in this period" if before else "since the first release"

    tiles = [("Page views", n(views), f"{n(sum(here['views'].values()))} today"),
             ("Arrived on a comparison", n(total["shared"]), "by a link that names a server size"),
             ("Link pasted somewhere", n(sum(total["previews"].values())), "posts and chats that drew a preview"),
             ("Price data read directly", n(total["data"]), "prices.json, not through the page")]
    if now_gh:
        tiles += [("GitHub stars", n(now_gh["stars"]), since("stars")),
                  ("Installer downloads", n(now_gh["installer"]), since("installer"))]
    tile_html = "".join(f'<div class="card tile"><span>{esc(a)}</span><b>{b}</b><span>{esc(c)}</span></div>'
                        for a, b, c in tiles)
    daily = _table(["Day", "Visitors", "Price page", "Home page", "On a comparison", "Data reads", "Link previews",
                    "Automated", "Your own"],
                   [[day_name(d), n(c["visitors"]), n(c["views"].get("prices", 0)), n(c["views"].get("home", 0)),
                     n(c["shared"]), n(c["data"]), n(sum(c["previews"].values())), n(c["automated"]), n(c["own"])]
                    for d, c in reversed(days)])
    sizes = sorted(total["sizes"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    size_rows = [["{} vCPU, {} GB memory, {} GB disk".format(*s.split("-")), n(v)] for s, v in sizes]
    cards = [
        ("Where visitors came from", ranked(total["sources"], SOURCE_NAMES)),
        ("Countries", ranked(total["countries"], COUNTRIES)),
        ("Devices", ranked(total["devices"], DEVICES)),
        ("Pages viewed", ranked(total["views"], PAGE_NAMES)),
        ("Link pasted into", ranked(total["previews"]) + '<p class="muted" style="margin-top:12px">When someone '
         "pastes the link into a post or a chat, that app fetches the page once to draw its preview. Apple apps are "
         "iMessage and other apps on an iPhone or a Mac.</p>"),
        ("Comparisons people arrived on", (_table(["Server size", "Visitors"], size_rows) if size_rows
                                           else '<p class="muted">Nobody yet.</p>')
         + '<p class="muted" style="margin-top:12px">A visit that starts on a link naming a server size: a comparison '
         "someone shared, or one the visitor came back to.</p>"),
    ]
    if total["countries"].get(""):
        cards[1] = (cards[1][0], cards[1][1] + '<p class="muted" style="margin-top:12px">The country was not '
                    "recorded for the first days of counting.</p>")
    if gh.get("releases"):
        rows = [[esc(r["tag"]), n(r["installer"]), n(r["bundle"])] for r in gh["releases"]]
        note = (f'<p class="muted" style="margin-top:12px">{plural(now_gh["forks"], "fork")}, '
                f'{plural(now_gh["issues"], "open issue")}. A download of install.sh is someone starting an install; '
                "what happens on their server afterwards is never reported back.</p>")
        cards.append(("GitHub releases", _table(["Release", "Installer", "Offline bundle"], rows) + note))
    traffic = {k: v for k, v in gh.get("traffic", {}).items() if first.isoformat() <= k <= today.isoformat()}
    if traffic:
        rows = [[day_name(date.fromisoformat(k)), n(v.get("views_unique", 0)), n(v.get("views", 0)),
                 n(v.get("clones_unique", 0))] for k, v in sorted(traffic.items(), reverse=True)]
        cards.append(("GitHub repository", _table(["Day", "People", "Views", "People who cloned"], rows)))
    card_html = "".join(f'<section class="card"><h2>{esc(t)}</h2>{body}</section>' for t, body in cards)
    current = ' aria-current="page"'
    links = "".join(f'<a href="{f}"{current if s == span else ""}>{esc(text)}</a>' for f, text, s in RANGES)
    updated = now.astimezone(TEHRAN)
    notes = [
        f"Counting started on {started.day} {started:%B %Y}. Days are Tehran days.",
        "A visitor is counted once a day. Someone who comes back another day is counted again, because nothing is "
        "kept that could recognise them.",
        f"Left out: {plural(total['automated'], 'automated request')} (crawlers and scripts), and "
        f"{plural(total['own'], 'page view')} of your own. Opening this panel marks that device and network as you "
        "for the rest of the day.",
        "No cookie is set and no script runs on the public pages. Addresses are logged without their last part, and "
        "the log is emptied every ten minutes.",
    ]
    if gh.get("error"):
        notes.append(f"GitHub could not be read last time ({esc(gh['error'])}); the figures are from the read before.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="color-scheme" content="light dark">
<title>Sarfeh visits</title><style>{STYLE}</style></head>
<body><main>
<div class="top"><h1>Sarfeh visits</h1>
<p class="muted">Updated {day_name(updated.date())}, {updated:%H:%M} Tehran time</p></div>
<nav class="ranges" aria-label="Period">{links}</nav>
<div class="lead">
<div class="card hero"><span>Visitors, {esc(label.lower())}</span><b>{n(total["visitors"])}</b>
<span>{plural(here["visitors"], "visitor")} today</span></div>
<div class="tiles">{tile_html}</div>
</div>
<section class="card" style="margin-top:12px"><h2>Visitors per day</h2>
<div class="scroll">{column_chart(days)}</div>
<details><summary>Every day as a table</summary><div class="scroll">{daily}</div></details></section>
<div class="grid">{card_html}</div>
<ul class="notes">{"".join(f"<li>{x}</li>" for x in notes)}</ul>
</main><script src="panel.js" defer></script></body></html>
"""


def write_panel(state: dict[str, Any], now: datetime, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    files = {name: page(state, now, label, span) for name, label, span in RANGES}
    files["panel.js"] = SCRIPT
    for name, text in files.items():
        _write(out / name, text)


# --------------------------------------------------------------------------- running


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def take_log(log: Path, container: str) -> Path | None:
    """Move the log aside and have nginx start a new one. Returns the file to read, or None when nothing was logged."""
    taken = log.with_name(log.name + ".1")
    if taken.exists():  # a run that was cut short left it: finish that one first
        return taken
    if not log.exists() or log.stat().st_size == 0:
        return None
    log.replace(taken)
    done = subprocess.run(["docker", "exec", container, "nginx", "-s", "reopen"], capture_output=True, timeout=60)
    if done.returncode != 0:
        taken.replace(log)  # nginx still writes to it: put it back for the next run
        raise SystemExit(f"nginx didn't reopen its log: {done.stderr.decode(errors='replace').strip()}")
    time.sleep(1)  # requests that were being written as the log changed hands
    return taken


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=Path.home() / "sarfeh", help="holds logs/, state/ and stats/")
    ap.add_argument("--ingest", type=Path, action="append", default=[],
                    help="also count this log file (nginx's stock format works too), once; it is left in place")
    ap.add_argument("--keep-log", action="store_true", help="don't take the live log this run")
    ap.add_argument("--container", default=CONTAINER, help="the nginx container that writes the log")
    ap.add_argument("--no-github", action="store_true")
    args = ap.parse_args()

    state_file = args.dir / "state" / "stats.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else new_state()
    counted = 0
    for extra in args.ingest:
        with extra.open(encoding="utf-8", errors="replace") as f:
            counted += ingest(state, f)
    taken = None if args.keep_log else take_log(args.dir / "logs" / "access.log", args.container)
    if taken is not None:
        with taken.open(encoding="utf-8", errors="replace") as f:
            counted += ingest(state, f)
    now = datetime.now(UTC)
    close_days(state, now.astimezone(TEHRAN).date())
    if not args.no_github:
        token_file = args.dir / "state" / "github-token"
        read_github(state, now, token_file.read_text(encoding="utf-8").strip() if token_file.exists() else None)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    _write(state_file, json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    if taken is not None:
        taken.unlink()  # only once its counts are saved
    write_panel(state, now, args.dir / "stats")
    print(f"counted {counted} requests; {len(state['days'])} days closed, {len(state['open'])} open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
