"""Sarfeh's central service, which Pendar runs for every installation: the price service (prices.py), and the shared
Bale bot's relay with the loop that reads the bot's messages (only when it has a bot token).

    FINOPS_HUB_PUBLIC_KEY=...  FINOPS_HUB_TOKEN_SECRET=...  FINOPS_RELAY_BOT_TOKEN=...  uvicorn finops_relay.main:app

For the relay, installations authenticate with their id and secret (Authorization: Bearer <id>:<secret>). They may
only send to chats linked to them, so the relay can't be used to message anyone else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import defaultdict
from collections.abc import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import prices
from .limits import Limiter
from .store import CODE_TTL, Store

log = logging.getLogger("finops.relay")
BALE_API = os.environ.get("FINOPS_RELAY_BALE_API", "https://tapi.bale.ai")
MAX_TEXT = 4000

HELP = ("سلام! این ربات گزارش‌های صرفه را می‌فرستد.\n"
        "برای وصل شدن، در تنظیمات داشبورد خودتان «گرفتن کد اتصال» را بزنید و کد را این‌طور بفرستید:\n"
        "/start کد\n\n"
        "Hello! This bot sends Sarfeh reports. To connect, choose “Get a link code” in your dashboard's "
        "Settings and send: /start CODE")


class Bale:
    def __init__(self, token: str):
        self.base = f"{BALE_API}/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(70))

    async def call(self, method: str, **params) -> dict:
        r = await self.client.post(f"{self.base}/{method}", json=params)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"bale {method}: {data.get('description')}")
        return data.get("result")

    async def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        params: dict = {"chat_id": chat_id, "text": text[:MAX_TEXT]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        await self.call("sendMessage", **params)


class State:
    def __init__(self) -> None:
        self.store = Store(os.environ.get("FINOPS_RELAY_DB", "/data/relay.db"))
        token = os.environ.get("FINOPS_RELAY_BOT_TOKEN", "")
        self.bale = Bale(token) if token else None
        self.waiting: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.register_limit = Limiter(10, 3600)
        self.send_limit = Limiter(60, 60)
        self.username: str | None = None


state: State | None = None


def st() -> State:
    assert state is not None
    return state


def who_of(msg: dict) -> str:
    u = msg.get("from") or {}
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or "?"
    return name


async def handle_update(s: State, update: dict) -> None:
    """One Bale update: link a chat, or pass a linked chat's message or button press to its installation."""
    msg = update.get("message")
    text, chat_id, who = None, None, "?"
    if msg and isinstance(msg.get("text"), str):
        text, chat_id, who = msg["text"].strip(), msg["chat"]["id"], who_of(msg)
    elif cb := update.get("callback_query"):
        text, who = cb.get("data") or "", who_of(cb)
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        with contextlib.suppress(Exception):
            await s.bale.call("answerCallbackQuery", callback_query_id=cb["id"])
    if not text or chat_id is None:
        return
    word, _, rest = text.partition(" ")
    word = word.split("@")[0].lower()
    if word in ("/start", "/link") and rest.strip():
        linked = s.store.use_code(rest.strip(), chat_id, who)
        if linked is None:
            await s.bale.send(chat_id, "این کد درست نیست یا منقضی شده است. یک کد تازه بگیرید.\n"
                                       "That code is wrong or has expired. Get a new one.")
            return
        await s.bale.send(chat_id, f"وصل شد به «{linked['org_name']}». برای دیدن فرمان‌ها /help را بفرستید.\n"
                                   f"Linked to “{linked['org_name']}”. Send /help for the commands.")
        s.store.enqueue(s.store.chat(chat_id), "/linked", who)
        s.waiting[linked["install_id"]].set()
        return
    chat = s.store.chat(chat_id)
    if chat is None:
        await s.bale.send(chat_id, HELP)
        return
    if word == "/unlink":
        s.store.unlink(chat_id)
        await s.bale.send(chat_id, "این گفتگو دیگر به داشبورد وصل نیست.\nThis chat is no longer linked.")
        return
    if not s.store.online(chat["install_id"]):
        await s.bale.send(chat_id, "داشبورد شما الان در دسترس نیست؛ کمی بعد دوباره امتحان کنید.\n"
                                   "Your dashboard can't be reached right now; try again later.")
        return
    s.store.enqueue(chat, text, who)
    s.waiting[chat["install_id"]].set()


async def bale_loop(s: State) -> None:
    offset = 0
    while True:
        try:
            updates = await s.bale.call("getUpdates", offset=offset, timeout=50)
            for u in updates or []:
                offset = max(offset, u["update_id"] + 1)
                try:
                    await handle_update(s, u)
                except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                    log.exception("update failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Bale unreachable: wait and try again
            log.warning("getUpdates failed: %s", type(exc).__name__)
            await asyncio.sleep(5)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global state
    state = State()
    app.state.hub = prices.Hub.from_env()
    if state.bale:
        with contextlib.suppress(Exception):
            state.username = (await state.bale.call("getMe")).get("username")
    task = asyncio.create_task(bale_loop(state)) if state.bale else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="Sarfeh service", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(prices.router)


def install(authorization: str = Header(default="")) -> str:
    token = authorization.removeprefix("Bearer ").strip()
    install_id, _, secret = token.partition(":")
    if not install_id or not secret or not st().store.authenticate(install_id, secret):
        raise HTTPException(401, "unknown installation")
    return install_id


@app.get("/health")
def health(request: Request):
    meta = request.app.state.hub.meta
    return {"status": "ok", "bot": st().bale is not None, "bot_username": st().username,
            "prices": meta["generated_at"] if meta else None}


class RegisterIn(BaseModel):
    name: str = Field(default="FinOps agent", max_length=100)


@app.post("/v1/installs")
def register(body: RegisterIn, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if not st().register_limit.allow(ip):
        raise HTTPException(429, "too many registrations from this address")
    install_id, secret = st().store.register(body.name)
    return {"install_id": install_id, "secret": secret}


class CodeIn(BaseModel):
    org_id: str = Field(max_length=64)
    org_name: str = Field(max_length=200)


@app.post("/v1/codes")
def new_code(body: CodeIn, install_id: str = Depends(install)):
    code, expires = st().store.new_code(install_id, body.org_id, body.org_name)
    return {"code": code, "expires_in": CODE_TTL, "expires_at": expires}


@app.get("/v1/chats")
def chats(org_id: str | None = None, install_id: str = Depends(install)):
    return [{"chat_id": c["chat_id"], "org_id": c["org_id"], "who": c["who"], "linked_at": c["linked_at"]}
            for c in st().store.chats(install_id, org_id)]


@app.delete("/v1/chats/{chat_id}")
async def unlink(chat_id: int, install_id: str = Depends(install)):
    if not st().store.unlink(chat_id, install_id):
        raise HTTPException(404, "not linked to this installation")
    if st().bale:
        with contextlib.suppress(Exception):
            await st().bale.send(chat_id, "این گفتگو از داشبورد جدا شد.\nThis chat was unlinked from the dashboard.")
    return {"unlinked": chat_id}


@app.get("/v1/poll")
async def poll(timeout: int = 25, install_id: str = Depends(install)):
    """Commands for this installation, waiting up to `timeout` seconds for one to arrive."""
    s = st()
    commands = s.store.take(install_id)
    if not commands:
        ev = s.waiting[install_id]
        ev.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout=max(1, min(timeout, 50)))
        commands = s.store.take(install_id)
    return {"commands": commands}


class SendIn(BaseModel):
    chat_id: int | None = None
    #: all chats of this organization when no chat_id is given (alerts, the monthly report)
    org_id: str | None = None
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    buttons: list[list[dict]] | None = None


@app.post("/v1/send")
async def send(body: SendIn, install_id: str = Depends(install)):
    s = st()
    if s.bale is None:
        raise HTTPException(503, "the relay has no bot configured")
    if body.chat_id is not None:
        chat = s.store.chat(body.chat_id)
        if chat is None or chat["install_id"] != install_id:
            raise HTTPException(403, "that chat isn't linked to this installation")
        targets = [body.chat_id]
    else:
        targets = [c["chat_id"] for c in s.store.chats(install_id, body.org_id)]
    sent = 0
    for chat_id in targets:
        if not s.send_limit.allow(install_id):
            raise HTTPException(429, "sending too fast")
        try:
            await s.bale.send(chat_id, body.text, body.buttons)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - a chat that blocked the bot: carry on with the rest
            log.warning("send to %s failed: %s", chat_id, type(exc).__name__)
    return {"sent": sent}
