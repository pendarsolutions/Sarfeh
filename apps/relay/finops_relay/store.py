"""The relay's state in one SQLite file: installations, one-time link codes, linked chats, commands in transit."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS installs (
  id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS codes (
  code TEXT PRIMARY KEY, install_id TEXT NOT NULL, org_id TEXT NOT NULL, org_name TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
  chat_id INTEGER NOT NULL, install_id TEXT NOT NULL, org_id TEXT NOT NULL, org_name TEXT NOT NULL,
  who TEXT NOT NULL, linked_at REAL NOT NULL, PRIMARY KEY (chat_id)
);
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT, install_id TEXT NOT NULL, org_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
  text TEXT NOT NULL, who TEXT NOT NULL, created_at REAL NOT NULL, taken_at REAL
);
"""

#: unambiguous letters and digits, so a code read aloud or retyped survives
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_TTL = 15 * 60
COMMAND_TTL = 10 * 60
MAX_CHATS_PER_INSTALL = 50


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ installations

    def register(self, name: str) -> tuple[str, str]:
        install_id, secret = "ins_" + secrets.token_hex(8), secrets.token_urlsafe(32)
        self._q("INSERT INTO installs VALUES (?, ?, ?, ?, NULL)", (install_id, _hash(secret), name[:100], time.time()))
        return install_id, secret

    def authenticate(self, install_id: str, secret: str) -> bool:
        row = self._q("SELECT secret_hash FROM installs WHERE id = ?", (install_id,))
        ok = bool(row) and secrets.compare_digest(row[0]["secret_hash"], _hash(secret))
        if ok:
            self._q("UPDATE installs SET last_seen = ? WHERE id = ?", (time.time(), install_id))
        return ok

    def online(self, install_id: str, within: float = 120) -> bool:
        row = self._q("SELECT last_seen FROM installs WHERE id = ?", (install_id,))
        return bool(row) and row[0]["last_seen"] is not None and time.time() - row[0]["last_seen"] < within

    # ------------------------------------------------------------------ linking

    def new_code(self, install_id: str, org_id: str, org_name: str) -> tuple[str, float]:
        self._q("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        expires = time.time() + CODE_TTL
        self._q("INSERT INTO codes VALUES (?, ?, ?, ?, ?)", (code, install_id, org_id, org_name[:200], expires))
        return code, expires

    def use_code(self, code: str, chat_id: int, who: str) -> sqlite3.Row | None:
        """Link a chat with a code; the code works once. A chat belongs to one organization at a time."""
        rows = self._q("SELECT * FROM codes WHERE code = ? AND expires_at >= ?", (code.strip().upper(), time.time()))
        if not rows:
            return None
        c = rows[0]
        count = self._q("SELECT COUNT(*) AS n FROM chats WHERE install_id = ?", (c["install_id"],))[0]["n"]
        if count >= MAX_CHATS_PER_INSTALL:
            return None
        self._q("DELETE FROM codes WHERE code = ?", (c["code"],))
        self._q("INSERT OR REPLACE INTO chats VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, c["install_id"], c["org_id"], c["org_name"], who[:200], time.time()))
        return c

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
        return rows[0] if rows else None

    def chats(self, install_id: str, org_id: str | None = None) -> list[sqlite3.Row]:
        if org_id is None:
            return self._q("SELECT * FROM chats WHERE install_id = ? ORDER BY linked_at", (install_id,))
        return self._q("SELECT * FROM chats WHERE install_id = ? AND org_id = ? ORDER BY linked_at", (install_id, org_id))

    def unlink(self, chat_id: int, install_id: str | None = None) -> bool:
        if install_id is None:
            return bool(self._q("DELETE FROM chats WHERE chat_id = ? RETURNING chat_id", (chat_id,)))
        return bool(self._q("DELETE FROM chats WHERE chat_id = ? AND install_id = ? RETURNING chat_id",
                            (chat_id, install_id)))

    # ------------------------------------------------------------------ commands in transit

    def enqueue(self, chat: sqlite3.Row, text: str, who: str) -> None:
        self._q("INSERT INTO commands (install_id, org_id, chat_id, text, who, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chat["install_id"], chat["org_id"], chat["chat_id"], text[:1000], who[:200], time.time()))

    def take(self, install_id: str) -> list[dict]:
        """Commands waiting for an installation, each handed out once. Old ones are dropped unanswered."""
        now = time.time()
        self._q("DELETE FROM commands WHERE created_at < ? OR (taken_at IS NOT NULL AND taken_at < ?)",
                (now - COMMAND_TTL, now - COMMAND_TTL))
        rows = self._q("UPDATE commands SET taken_at = ? WHERE install_id = ? AND taken_at IS NULL "
                       "RETURNING id, org_id, chat_id, text, who", (now, install_id))
        return [dict(r) for r in sorted(rows, key=lambda r: r["id"])]
