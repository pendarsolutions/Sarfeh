"""The shared Bale bot, seen from an installation: linking chats through the relay, answering commands, and the
monthly report (which also goes by email and the installation's own Bale bot, if set up).

Everything a chat sees is worked out here, on the installation; the relay only carries the finished text.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .dates import current_month, fa_number, month_label, shift_month, to_jalali, today_in_iran
from .orm import Organization, QuoteRow, RelayLink
from .spend import budget_status, estimate_inventory, summary

log = logging.getLogger("finops.bot")
TIMEOUT = httpx.Timeout(15, read=60)


class RelayError(RuntimeError):
    pass


# --------------------------------------------------------------------------- relay client


def relay_url() -> str | None:
    url = get_settings().relay_url
    return url.rstrip("/") if url else None


def link_row(db: Session, register: bool = False) -> RelayLink | None:
    """This installation's registration; made on first use (an admin asking for a link code)."""
    url = relay_url()
    if url is None:
        return None
    row = db.get(RelayLink, 1)
    if row is not None and row.relay_url == url:
        return row
    if not register:
        return None
    try:
        r = httpx.post(f"{url}/v1/installs", json={"name": "FinOps agent"}, timeout=TIMEOUT)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise RelayError(f"the Bale relay can't be reached ({type(exc).__name__})") from exc
    data = r.json()
    if row is None:
        row = RelayLink(id=1, relay_url=url, install_id=data["install_id"], secret=data["secret"])
        db.add(row)
    else:
        row.relay_url, row.install_id, row.secret = url, data["install_id"], data["secret"]
    db.commit()
    return row


class RelayForgot(RelayError):
    """The relay doesn't know this installation any more (its data was reset): register again."""


def _call(row: RelayLink, method: str, path: str, **kw: Any) -> Any:
    try:
        r = httpx.request(method, f"{row.relay_url}{path}", headers={"Authorization": f"Bearer {row.install_id}:{row.secret}"},
                          timeout=kw.pop("timeout", TIMEOUT), **kw)
    except httpx.HTTPError as exc:
        raise RelayError(f"the Bale relay can't be reached ({type(exc).__name__})") from exc
    if r.status_code == 401:
        raise RelayForgot("the Bale relay doesn't know this installation; link the chats again")
    if r.status_code >= 400:
        raise RelayError(f"the Bale relay answered {r.status_code}")
    return r.json()


def bot_username() -> str | None:
    url = relay_url()
    if url is None:
        return None
    try:
        return httpx.get(f"{url}/health", timeout=TIMEOUT).json().get("bot_username")
    except (httpx.HTTPError, ValueError):
        return None


def new_code(db: Session, org: Organization) -> dict[str, Any]:
    row = link_row(db, register=True)
    if row is None:
        raise RelayError("no Bale relay is set up for this installation (FINOPS_RELAY_URL)")
    try:
        return _call(row, "POST", "/v1/codes", json={"org_id": org.id, "org_name": org.name})
    except RelayForgot:
        db.delete(row)
        db.commit()
        row = link_row(db, register=True)
        return _call(row, "POST", "/v1/codes", json={"org_id": org.id, "org_name": org.name})


def linked_chats(db: Session, org: Organization) -> list[dict[str, Any]]:
    row = link_row(db)
    return [] if row is None else _call(row, "GET", "/v1/chats", params={"org_id": org.id})


def unlink(db: Session, org: Organization, chat_id: int) -> None:
    row = link_row(db)
    if row is None or chat_id not in {c["chat_id"] for c in linked_chats(db, org)}:
        raise RelayError("that chat isn't linked to this organization")
    _call(row, "DELETE", f"/v1/chats/{chat_id}")


def send(db: Session, text: str, org_id: str | None = None, chat_id: int | None = None) -> int:
    """To one chat, to every chat of an organization, or (both None) every chat linked to this installation."""
    row = link_row(db)
    if row is None:
        return 0
    return _call(row, "POST", "/v1/send", json={"text": text, "org_id": org_id, "chat_id": chat_id})["sent"]


# --------------------------------------------------------------------------- what chats see


def _money(v: Decimal | None, lang: str) -> str:
    if v is None:
        return "—"
    text = format(Decimal(v).quantize(Decimal(1)), ",f")
    return f"{fa_number(text)} تومان" if lang == "fa" else f"{text} Toman"


def _pct(v: Decimal | None, lang: str) -> str:
    if v is None:
        return "—"
    text = format(abs(Decimal(v)), "f")
    return f"{fa_number(text)}٪" if lang == "fa" else f"{text}%"


def _provider_name(pid: str | None, vendor: str | None, lang: str) -> str:
    if pid and (p := snapshot_cache().get(pid)):
        return (p.name_fa or p.name_en) if lang == "fa" else p.name_en
    return vendor or pid or "—"


_providers: dict[str, Any] = {}


def snapshot_cache() -> dict[str, Any]:
    if not _providers:
        from .db import SessionLocal
        from .services import snapshot

        with SessionLocal() as db:
            _providers.update(snapshot(db).providers)
    return _providers


def _open_savings(db: Session, org: Organization) -> Decimal | None:
    from .routers.analysis import _summary_from_rows
    from .services import latest_analysis

    inv = estimate_inventory(db, org)
    if inv is None:
        return None
    rows = latest_analysis(db, inv)
    if not rows:
        return None
    q = db.get(QuoteRow, rows[0].quote_id)
    total = _summary_from_rows(rows, q.total_toman if q else None, db)["avoidable_monthly_toman"]
    return Decimal(total) if total is not None else None


def month_report(db: Session, org: Organization, month: str | None = None, lang: str | None = None,
                 now: datetime | None = None) -> str:
    """One month in a few lines: what was paid and its change, the estimate, budgets, unusual changes, invoices
    waiting, and the open savings. The monthly report on the 1st is this for the month just ended."""
    lang = lang or (org.settings or {}).get("default_language", "fa")
    fa = lang == "fa"
    s = summary(db, org.id, months=13, now=now)
    month = month or s["current_month"]
    m = next((x for x in s["months"] if x["month"] == month), None)
    label = month_label(month, lang)
    lines = [f"📊 {'گزارش' if fa else 'Report'} {label} · {org.name}", ""]
    if m is None or m["paid_toman"] is None:
        lines.append("برای این ماه پرداختی ثبت نشده است." if fa else "No payments recorded for this month.")
    else:
        lines.append(f"{'پرداختی' if fa else 'Paid'}: {_money(m['paid_toman'], lang)}")
        if m["change_toman"] is not None:
            up = not str(m["change_toman"]).startswith("-")
            arrow = "▲" if up else "▼"
            word = ("بیشتر" if up else "کمتر") if fa else ("up" if up else "down")
            lines.append(f"{arrow} {_money(abs(Decimal(m['change_toman'])), lang)} ({_pct(m['change_percent'], lang)}) "
                         f"{word} {'از ماه قبل' if fa else 'on the month before'}")
        if m["estimated_toman"] is not None:
            lines.append(f"{'برآورد' if fa else 'Estimated'}: {_money(m['estimated_toman'], lang)}")
            if m["gap"] and m["gap"]["kind"] in ("paid_more", "paid_less"):
                more = m["gap"]["kind"] == "paid_more"
                lines.append(f"⚠️ {_pct(m['gap']['percent'], lang)} "
                             + (("بیشتر" if more else "کمتر") + " از برآورد" if fa else
                                ("more" if more else "less") + " than estimated"))
        top = sorted([p for p in m["by_provider"] if p["paid_toman"]], key=lambda p: -Decimal(p["paid_toman"]))[:3]
        if top:
            lines += ["", "بیشترین پرداخت:" if fa else "Largest payments:"]
            lines += [f"• {_provider_name(p['provider_id'], p['vendor'], lang)}: {_money(p['paid_toman'], lang)}" for p in top]
        teams = [t for t in m["by_team"] if t["team_id"] or Decimal(t["paid_toman"] or 0) > 0]
        if any(t["team_id"] for t in teams):
            lines += ["", "به تفکیک تیم:" if fa else "By team:"]
            lines += [f"• {t['name'] or ('تخصیص‌نیافته' if fa else 'Unallocated')}: {_money(t['paid_toman'], lang)}"
                      for t in teams]
        if m["movers"]:
            lines += ["", "تغییرهای غیرعادی:" if fa else "Unusual changes:"]
            for x in m["movers"]:
                name = x.get("name") if x["kind"] == "team" else _provider_name(x.get("provider_id"), x.get("vendor"), lang)
                lines.append(f"• {name}: {_pct(x['change_percent'], lang)} "
                             + (("بیشتر" if Decimal(x["change_toman"]) > 0 else "کمتر") if fa else
                                ("up" if Decimal(x["change_toman"]) > 0 else "down")))
    if month == s["current_month"]:
        b = budget_status(db, org, now)
        if b["budgets"]:
            lines += ["", "بودجه‌ها:" if fa else "Budgets:"]
            for x in b["budgets"]:
                name = x["name"] or ("کل سازمان" if fa else "Whole organization")
                mark = "🔴" if x["level"] >= 100 else "🟠" if x["level"] >= 80 or x["projected_over"] else "🟢"
                lines.append(f"{mark} {name}: {_money(x['paid_toman'], lang)} {'از' if fa else 'of'} "
                             f"{_money(x['monthly_toman'], lang)} ({_pct(x['used_percent'], lang)})")
    if s["to_review"]:
        n = len(s["to_review"])
        lines += ["", f"📥 {fa_number(n)} فاکتور منتظر بررسی است." if fa else f"📥 {n} invoice(s) waiting to be checked."]
    savings = _open_savings(db, org)
    if savings:
        lines += ["", f"💡 {'صرفه‌جویی ممکن' if fa else 'Possible saving'}: {_money(savings, lang)} "
                      f"{'در ماه' if fa else 'a month'}"]
    if url := get_settings().public_url:
        lines += ["", f"{url.rstrip('/')}/spend"]
    return "\n".join(lines)


def help_text(lang: str) -> str:
    if lang == "fa":
        return ("فرمان‌ها:\n/report گزارش این ماه\n/last گزارش ماه گذشته\n/budgets بودجه‌ها\n"
                "/review فاکتورهای منتظر بررسی\n/unlink جدا کردن این گفتگو")
    return ("Commands:\n/report this month so far\n/last last month\n/budgets budgets\n"
            "/review invoices waiting to be checked\n/unlink disconnect this chat")


BUTTONS = [[{"text": "📊 /report", "callback_data": "/report"}, {"text": "🗓 /last", "callback_data": "/last"}],
           [{"text": "🎯 /budgets", "callback_data": "/budgets"}, {"text": "📥 /review", "callback_data": "/review"}]]


def answer(db: Session, org: Organization, text: str) -> str:
    """The reply to one command from a linked chat. Read-only: nothing here changes anything."""
    lang = (org.settings or {}).get("default_language", "fa")
    fa = lang == "fa"
    word = text.strip().split(" ")[0].split("@")[0].lower()
    if word == "/report":
        return month_report(db, org, lang=lang)
    if word == "/last":
        return month_report(db, org, month=shift_month(summary(db, org.id, months=1)["current_month"], -1), lang=lang)
    if word == "/budgets":
        b = budget_status(db, org)
        if not b["budgets"]:
            return "بودجه‌ای تعیین نشده است." if fa else "No budgets are set."
        out = [f"🎯 {'بودجه‌های' if fa else 'Budgets,'} {month_label(b['month'], lang)}"]
        for x in b["budgets"]:
            name = x["name"] or ("کل سازمان" if fa else "Whole organization")
            out.append(f"• {name}: {_money(x['paid_toman'], lang)} {'از' if fa else 'of'} {_money(x['monthly_toman'], lang)} "
                       f"({_pct(x['used_percent'], lang)}); {'پیش‌بینی' if fa else 'projected'} "
                       f"{_money(x['projected_toman'], lang)}")
        return "\n".join(out)
    if word == "/review":
        waiting = summary(db, org.id, months=1)["to_review"]
        if not waiting:
            return "فاکتوری منتظر بررسی نیست." if fa else "No invoices are waiting."
        out = ["📥 " + ("فاکتورهای منتظر بررسی:" if fa else "Invoices waiting to be checked:")]
        out += [f"• {_provider_name(e['provider_id'], e['vendor'], lang)} · {month_label(e['month'], lang)} · "
                f"{_money(e['amount_toman'], lang)}" for e in waiting[:10]]
        if url := get_settings().public_url:
            out.append(f"{url.rstrip('/')}/spend")
        return "\n".join(out)
    return help_text(lang)


def poll_once(db: Session, timeout: int = 25) -> int:
    """Answer the commands waiting at the relay. Returns how many were answered."""
    row = link_row(db)
    if row is None:
        return 0
    commands = _call(row, "GET", "/v1/poll", params={"timeout": timeout}, timeout=httpx.Timeout(15, read=timeout + 15))["commands"]
    for c in commands:
        org = db.get(Organization, c["org_id"])
        if org is None:
            continue
        try:
            text = answer(db, org, c["text"])
        except Exception:  # noqa: BLE001 - say something rather than nothing; the details go to the log
            log.exception("bot command failed")
            text = "خطایی پیش آمد؛ دوباره امتحان کنید." if (org.settings or {}).get("default_language", "fa") == "fa" \
                else "Something went wrong; try again."
        _call(row, "POST", "/v1/send", json={"chat_id": c["chat_id"], "text": text, "buttons": BUTTONS})
    return len(commands)


def poll_for(seconds: float = 55) -> int:
    """Keep answering for about `seconds` (the worker runs this every minute)."""
    from .db import SessionLocal

    end, answered = time.monotonic() + seconds, 0
    while (left := end - time.monotonic()) > 5:
        with SessionLocal() as db:
            if link_row(db) is None:
                return answered
            try:
                answered += poll_once(db, timeout=int(min(25, left - 3)))
            except RelayError as exc:
                log.warning("relay poll failed: %s", exc)
                time.sleep(5)
    return answered


# --------------------------------------------------------------------------- the monthly report


def monthly_reports(db: Session, now: datetime | None = None) -> int:
    """On the 1st (Solar Hijri), each organization's report on the month just ended, once: to its linked Bale chats,
    and by email and the installation's own Bale bot where those are set up."""
    from .alerts import notify

    today = today_in_iran(now)
    if to_jalali(today)[2] != 1:
        return 0
    month = shift_month(current_month(now), -1)
    sent = 0
    for org in db.scalars(select(Organization)).all():
        settings = dict(org.settings or {})
        if settings.get("monthly_report_sent") == month:
            continue
        s = summary(db, org.id, months=2, now=now)
        if not any(m["entries"] for m in s["months"]) and estimate_inventory(db, org) is None:
            continue  # nothing to report yet
        lang = settings.get("default_language", "fa")
        text = month_report(db, org, month=month, lang=lang, now=now)
        title, _, body = text.partition("\n")
        notify(title.removeprefix("📊 "), body.strip(), org_id=org.id)
        settings["monthly_report_sent"] = month
        org.settings = settings
        db.commit()
        sent += 1
    return sent
