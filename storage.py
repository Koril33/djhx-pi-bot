"""SQLite 存储：用户与会话映射（V1 单机单用户规模，不做过度设计）。

表结构：
- users:    telegram 用户
- sessions: 会话记录（每个会话对应 pi 侧的一个 jsonl 会话文件）
"""
import sqlite3
import threading
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    name           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'archived',  -- active | archived
    pi_session_file TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class Storage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    # ---------- users ----------
    def ensure_user(self, user_id: int, username: str | None) -> None:
        self._exec(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username),
        )

    # ---------- sessions ----------
    def create_session(self, user_id: int, name: str) -> str:
        sid = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO sessions (id, user_id, name) VALUES (?, ?, ?)",
            (sid, user_id, name),
        )
        return sid

    def list_sessions(self, user_id: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        )

    def get_session(self, sid: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM sessions WHERE id = ?", (sid,))
        return rows[0] if rows else None

    def get_active_session(self, user_id: int) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM sessions WHERE user_id = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        )
        return rows[0] if rows else None

    def set_active(self, sid: str) -> None:
        self._exec("UPDATE sessions SET status = 'archived', updated_at = datetime('now') WHERE status = 'active'")
        self._exec(
            "UPDATE sessions SET status = 'active', updated_at = datetime('now') WHERE id = ?",
            (sid,),
        )

    def set_pi_session_file(self, sid: str, pi_session_file: str) -> None:
        self._exec(
            "UPDATE sessions SET pi_session_file = ?, updated_at = datetime('now') WHERE id = ?",
            (pi_session_file, sid),
        )

    def rename_session(self, sid: str, name: str) -> None:
        self._exec(
            "UPDATE sessions SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (name, sid),
        )

    def touch(self, sid: str) -> None:
        self._exec("UPDATE sessions SET updated_at = datetime('now') WHERE id = ?", (sid,))
