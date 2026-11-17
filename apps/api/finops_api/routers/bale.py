"""Linking Bale chats to an organization through the shared bot (Settings)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import bot
from ..db import get_db
from ..security import Principal, audit, require

router = APIRouter(prefix="/api/v1/bale", tags=["bale"])


def _guard(fn, *args):
    try:
        return fn(*args)
    except bot.RelayError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("")
def status(db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    available = bot.relay_url() is not None
    linked = bot.link_row(db) is not None
    chats = _guard(bot.linked_chats, db, p.org) if linked else []
    return {"available": available, "registered": linked, "bot_username": bot.bot_username() if available else None,
            "chats": chats}


@router.post("/code")
def code(db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    """A one-time code (15 minutes) that links the Bale chat it is sent from to this organization."""
    out = _guard(bot.new_code, db, p.org)
    audit(db, p, "bale.code", "organization", p.org.id)
    db.commit()
    return {**out, "bot_username": bot.bot_username()}


@router.delete("/chats/{chat_id}")
def unlink(chat_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    _guard(bot.unlink, db, p.org, chat_id)
    audit(db, p, "bale.unlink", "organization", p.org.id, {"chat_id": chat_id})
    db.commit()
    return {"unlinked": chat_id}


@router.post("/test")
def test(db: Session = Depends(get_db), p: Principal = Depends(require("admin"))):
    """This month's report, now, to every chat linked to this organization."""
    sent = _guard(lambda: bot.send(db, bot.month_report(db, p.org), org_id=p.org.id))
    return {"sent": sent}
