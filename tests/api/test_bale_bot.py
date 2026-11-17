"""The shared Bale bot end to end: the relay (with a fake Bale), an installation linking a chat, commands answered
on the installation, and alerts reaching linked chats only."""

import itertools
import json

import pytest
from fastapi.testclient import TestClient

_n = itertools.count()
V = "/api/v1"
RELAY = "http://relay.test"


class FakeBale:
    """Records what the bot would have sent."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text))

    async def call(self, method, **params):
        return {}

    def to(self, chat_id):
        return [t for c, t in self.sent if c == chat_id]


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.delenv("FINOPS_RELAY_BOT_TOKEN", raising=False)
    from finops_relay import main

    with TestClient(main.app) as c:
        main.state.bale = FakeBale()
        main.state.username = "finops_agent_bot"
        yield c, main


@pytest.fixture
def linked(client, relay, monkeypatch):
    """An organization whose installation talks to the test relay."""
    c, main = relay

    class Route:  # the installation's httpx calls, sent to the relay app in-process
        HTTPError = __import__("httpx").HTTPError
        Timeout = __import__("httpx").Timeout

        @staticmethod
        def request(method, url, **kw):
            kw.pop("timeout", None)
            r = c.request(method, url.removeprefix(RELAY), **kw)
            return r

        @classmethod
        def post(cls, url, **kw):
            return cls.request("POST", url, **kw)

        @classmethod
        def get(cls, url, **kw):
            return cls.request("GET", url, **kw)

    from finops_api import bot
    from finops_api.config import get_settings

    monkeypatch.setattr(bot, "httpx", Route)
    from finops_api.db import SessionLocal
    from finops_api.orm import RelayLink

    with SessionLocal() as db:  # each test's relay starts empty; so does this installation's registration
        db.query(RelayLink).delete()
        db.commit()
    monkeypatch.setattr(get_settings(), "relay_url", RELAY)
    r = client.post(f"{V}/auth/register", json={"email": f"bale{next(_n)}@example.ir", "password": "owner-password-1",
                                                "name": "Owner", "organization_name": "Bale team"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return h, main


def _update(text, chat_id=777, uid=next(_n)):
    return {"update_id": uid, "message": {"text": text, "chat": {"id": chat_id}, "from": {"first_name": "Sara"}}}


def _deliver(main, text, chat_id=777):
    import asyncio

    asyncio.run(main.handle_update(main.state, _update(text, chat_id)))


def test_a_chat_links_with_a_one_time_code_and_gets_answers(client, linked, api_env):
    h, main = linked
    status = client.get(f"{V}/bale", headers=h).json()
    assert status["available"] and not status["registered"] and status["chats"] == []

    code = client.post(f"{V}/bale/code", headers=h).json()
    assert len(code["code"]) == 8 and code["bot_username"] == "finops_agent_bot"
    _deliver(main, f"/start {code['code']}")
    assert "Bale team" in main.state.bale.to(777)[-1]
    _deliver(main, f"/start {code['code']}", chat_id=888)  # a code works once
    assert "expired" in main.state.bale.to(888)[-1]
    assert [c["chat_id"] for c in client.get(f"{V}/bale", headers=h).json()["chats"]] == [777]

    client.post(f"{V}/inventories/demo", headers=h)
    _deliver(main, "/report")
    from finops_api.bot import poll_once
    from finops_api.db import SessionLocal

    with SessionLocal() as db:
        answered = poll_once(db, timeout=1)
    assert answered == 2  # the welcome after linking, and /report
    report = main.state.bale.to(777)[-1]
    assert "گزارش" in report and "۲۳٬۶۹۰٬۰۰۰ تومان" in report  # the sample's payments this month, in Persian


def test_strangers_get_help_and_installs_reach_only_their_chats(client, linked):
    h, main = linked
    _deliver(main, "/report", chat_id=999)
    assert "/start" in main.state.bale.to(999)[-1]
    from finops_api.bot import link_row
    from finops_api.db import SessionLocal

    client.post(f"{V}/bale/code", headers=h)
    with SessionLocal() as db:
        row = link_row(db)
        auth = {"Authorization": f"Bearer {row.install_id}:{row.secret}"}
    from finops_relay.main import app

    c = TestClient(app)  # without restarting the relay the fixture runs
    assert c.post("/v1/send", headers=auth, json={"chat_id": 999, "text": "spam"}).status_code == 403
    assert c.get("/v1/poll", headers={"Authorization": "Bearer nobody:nothing"}).status_code == 401


def test_alerts_go_to_the_organizations_linked_chats(client, linked):
    h, main = linked
    code = client.post(f"{V}/bale/code", headers=h).json()["code"]
    _deliver(main, f"/start {code}", chat_id=4242)
    from finops_api.alerts import notify
    from finops_api.db import SessionLocal

    org_id = client.get(f"{V}/settings", headers=h).json()["organization"]["id"]
    notify("Budget", "80% used", org_id=org_id)
    assert main.state.bale.to(4242)[-1] == "Budget\n\n80% used"
    assert client.delete(f"{V}/bale/chats/4242", headers=h).status_code == 200
    with SessionLocal():
        notify("Budget", "100% used", org_id=org_id)
    assert all("100%" not in t for t in main.state.bale.to(4242))


def test_the_monthly_report_goes_out_once_on_the_first(client, linked, monkeypatch):
    from datetime import UTC, datetime

    from finops_api.bot import monthly_reports
    from finops_api.db import SessionLocal

    h, main = linked
    client.post(f"{V}/inventories/demo", headers=h)
    sent = []
    monkeypatch.setattr("finops_api.alerts.notify", lambda s, b, org_id=None: sent.append((s, b, org_id)))
    first_of_azar = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)  # 1 Azar 1405
    with SessionLocal() as db:
        assert monthly_reports(db, now=datetime(2026, 11, 21, 6, 0, tzinfo=UTC)) == 0  # not the 1st
        monthly_reports(db, now=first_of_azar)
        monthly_reports(db, now=first_of_azar)
    org_id = client.get(f"{V}/settings", headers=h).json()["organization"]["id"]
    mine = [s for s in sent if s[2] == org_id]
    assert len(mine) == 1 and "آبان ۱۴۰۵" in mine[0][0]
    assert json.dumps(mine[0][1], ensure_ascii=False).count("تومان") >= 2
