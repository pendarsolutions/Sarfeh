"""Fail if a page of the public website is wider than a phone's screen.

    python scripts/check_page_widths.py site/dist        # after `npm run build` in site/

One element that doesn't wrap (a long badge, an address, a command) makes the whole page scroll sideways on a phone,
and nothing else notices: the build passes and a desktop window looks fine. This opens the built site in a Chromium
browser as a 320px and a 360px phone and compares each page's width with the screen's.

The price page is checked with every price marked old and every condition shown, the widest it can get: its table
is rendered from the catalog as if no provider had been read since (scripts/refresh_prices.py), and its list of
price changes has a provider whose every price moved.
The browser is BROWSER, or the first of Chrome, Chromium and Edge that is installed.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_prices  # noqa: E402

WIDTHS = (320, 360)
PAGES = ("/", "/?lang=en", "/prices/?cpu=2&ram=4&disk=50", "/prices/?cpu=2&ram=4&disk=50&lang=en",
         "/prices/?cpu=16&ram=64&disk=400", "/prices/?view=changes", "/prices/?view=changes&lang=en")
BROWSERS = ("google-chrome", "chromium", "chromium-browser", "microsoft-edge",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
#: the page's width against the screen's, and the elements that stick out (to name the culprit in the report)
MEASURE = """(() => {
  const w = document.documentElement.clientWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width && (r.right > w + 1 || r.left < -1) && !el.children.length)
      out.push(el.tagName.toLowerCase() + '.' + String(el.className).slice(0, 30) + ': ' + (el.innerText || '').slice(0, 50));
  }
  return {screen: w, page: document.documentElement.scrollWidth, culprits: out.slice(0, 5)};
})()"""


def _browser() -> str:
    for name in filter(None, (os.environ.get("BROWSER"), *BROWSERS)):
        found = shutil.which(name) or (name if Path(name).exists() else None)
        if found:
            return found
    raise SystemExit("no Chromium browser found; set BROWSER to one")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # one line per request would bury the report
        pass


class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:  # the browser closing a connection as it exits
        pass


def _serve(directory: Path) -> int:
    handler = functools.partial(_Quiet, directory=str(directory))
    server = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


async def _check(site: int, debug: int) -> list[str]:
    for _ in range(100):
        try:
            targets = httpx.get(f"http://127.0.0.1:{debug}/json", trust_env=False).json()
            page = next(t for t in targets if t["type"] == "page")
            break
        except (httpx.HTTPError, StopIteration):
            await asyncio.sleep(0.2)
    else:
        raise SystemExit("the browser didn't start")
    failures = []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**26) as ws:
        sent = 0

        async def cdp(method: str, **params):
            nonlocal sent
            sent += 1
            await ws.send(json.dumps({"id": sent, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == sent:
                    if "error" in msg:
                        raise SystemExit(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        async def value(expression: str):
            return (await cdp("Runtime.evaluate", returnByValue=True, expression=expression))["result"].get("value")

        await cdp("Page.enable")
        for width in WIDTHS:
            await cdp("Emulation.setDeviceMetricsOverride", width=width, height=800, deviceScaleFactor=2, mobile=True)
            for path in PAGES:
                await cdp("Page.navigate", url=f"http://127.0.0.1:{site}{path}")
                ready = ("!!document.querySelector('.pr-event')" if "view=changes" in path
                         else "!!document.querySelector('.pr-offer')" if path.startswith("/prices")
                         else "!!document.querySelector('.lp')")
                for _ in range(60):
                    await asyncio.sleep(0.25)
                    if await value(ready):
                        break
                else:
                    failures.append(f"{path} at {width}px: the page never rendered")
                    continue
                await asyncio.sleep(0.5)
                seen = await value(MEASURE)
                ok = seen["page"] <= seen["screen"]
                print(f"{'ok  ' if ok else 'WIDE'} {width}px {path}: page {seen['page']}px")
                if not ok:
                    failures.append(f"{path} at {width}px is {seen['page']}px wide: " + " | ".join(seen["culprits"]))
    return failures


def _sample_changes(now: datetime) -> str:
    """A record of changes with long lines: every ArvanCloud rate up by a fifth, read by hand long after the last."""
    seed = refresh_prices.load_seed_snapshot()
    before = list(seed.provider_items("arvancloud"))
    after = [i.model_copy(update={"price_toman": i.price_toman * 6 / 5, "ram_gb": 64}) if i.price_toman else i
             for i in before]
    feed = {"schema": 1, "events": []}
    refresh_prices.price_feed.add(feed, [refresh_prices.price_feed.event(
        "arvancloud", before, after, now, since="2026-10-04", source="file"),
        # and a provider joining the page, with its long plan names
        refresh_prices.price_feed.added("hostiran", list(seed.provider_items("hostiran")), now)])
    names = {pid: {"name_fa": p.name_fa or p.name_en, "name_en": p.name_en} for pid, p in seed.providers.items()}
    return refresh_prices.price_feed.published(feed, names)


def main() -> int:
    if len(sys.argv) != 2 or not (Path(sys.argv[1]) / "index.html").exists():
        raise SystemExit("usage: check_page_widths.py DIR   (the built site, with index.html)")
    with tempfile.TemporaryDirectory() as tmp:
        site_dir = Path(tmp) / "site"
        shutil.copytree(sys.argv[1], site_dir)
        # the widest table: the catalog as first read, so today every price is old
        now = datetime.now(UTC)
        table = json.loads(refresh_prices.render({"providers": {}}, now))
        for size in table["sizes"]:
            for offer in size["offers"]:  # and a long CPU model on each, as a user's report would put there
                offer["cpu_model"] = {"model": "Intel(R) Xeon(R) Platinum 8280 CPU @ 2.70GHz", "hidden": False,
                                      "reported_on": now.date().isoformat()}
        (site_dir / "prices.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        (site_dir / "changes.json").write_text(_sample_changes(now), encoding="utf-8")
        debug = _free_port()
        flags = ["--headless=new", f"--remote-debugging-port={debug}", f"--user-data-dir={Path(tmp) / 'profile'}",
                 "--hide-scrollbars", "--no-first-run", "--disable-gpu"]
        if os.environ.get("CI"):
            flags.append("--no-sandbox")  # hosted runners don't allow the browser's sandbox; these are our own pages
        browser = subprocess.Popen([_browser(), *flags, "about:blank"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            failures = asyncio.run(_check(_serve(site_dir), debug))
        finally:
            browser.terminate()
            try:
                browser.wait(10)  # before the profile folder is removed under it
            except subprocess.TimeoutExpired:
                browser.kill()
    for f in failures:
        print("FAIL", f.encode("ascii", "backslashreplace").decode())
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
