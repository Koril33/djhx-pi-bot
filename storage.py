"""SQLite 存储：用户、会话映射及 Telegram webhook inbox。

表结构：
- users:    telegram 用户
- sessions: 会话记录（每个会话对应 pi 侧的一个 jsonl 会话文件）
"""
import os
import sqlite3
import threading
import time
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

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id      INTEGER PRIMARY KEY,
    payload        TEXT,
    state          TEXT NOT NULL DEFAULT 'pending',
    priority       INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    received_at    REAL NOT NULL,
    processed_at   REAL,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_pending
    ON telegram_updates(state, update_id);
"""


class UpdateQueueFull(Exception):
    """Telegram 可重试的更新队列容量已用尽。"""


class Storage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # inbox 暂存 Telegram 消息正文，不能让同机其他用户读取数据库文件。
        if os.name == "posix" and db_path != ":memory:":
            os.chmod(db_path, 0o600)
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

    def create_session_with_file_and_activate(
        self, user_id: int, name: str, pi_session_file: str
    ) -> str:
        """新建并切换会话；数据库发生错误时不留下半写入的会话。"""
        sid = uuid.uuid4().hex[:12]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (id, user_id, name, pi_session_file) VALUES (?, ?, ?, ?)",
                (sid, user_id, name, pi_session_file),
            )
            self._conn.execute(
                "UPDATE sessions SET status = 'archived', updated_at = datetime('now') "
                "WHERE user_id = ? AND status = 'active'",
                (user_id,),
            )
            self._conn.execute(
                "UPDATE sessions SET status = 'active', updated_at = datetime('now') WHERE id = ?",
                (sid,),
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
        with self._lock, self._conn:
            row = self._conn.execute("SELECT user_id FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                raise ValueError(f"会话不存在: {sid}")
            self._conn.execute(
                "UPDATE sessions SET status = 'archived', updated_at = datetime('now') "
                "WHERE user_id = ? AND status = 'active' AND id <> ?",
                (row["user_id"], sid),
            )
            self._conn.execute(
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

    # ---------- Telegram webhook inbox ----------
    # 更新先持久化再 ACK；重启后未处理记录仍可重放。
    def enqueue_update(
        self, update_id: int, payload: str, *, capacity: int = 1000, priority: int = 0
    ) -> bool:
        """返回 False 表示 update_id 已接收；队列满时抛 UpdateQueueFull。"""
        with self._lock, self._conn:
            if self._conn.execute(
                "SELECT 1 FROM telegram_updates WHERE update_id = ?", (update_id,)
            ).fetchone():
                return False
            pending = self._conn.execute(
                "SELECT count(*) FROM telegram_updates WHERE state IN ('pending', 'processing')"
            ).fetchone()[0]
            if pending >= capacity:
                raise UpdateQueueFull(f"Telegram 更新队列已满 ({pending})")
            self._conn.execute(
                "INSERT INTO telegram_updates (update_id, payload, priority, received_at) VALUES (?, ?, ?, ?)",
                (update_id, payload, priority, time.time()),
            )
            return True

    def next_update(self) -> sqlite3.Row | None:
        """诊断用：取最早待处理更新。"""
        rows = self._query(
            "SELECT update_id, payload, attempts FROM telegram_updates "
            "WHERE state = 'pending' ORDER BY update_id LIMIT 1"
        )
        return rows[0] if rows else None

    def fail_interrupted_updates(self) -> int:
        """进程中断后的执行结果不确定，不能自动重放可能有副作用的指令。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE telegram_updates SET state = 'failed', payload = NULL, processed_at = ?, "
                "last_error = '服务重启时更新执行状态不确定；未自动重放' "
                "WHERE state = 'processing'",
                (time.time(),),
            )
            return cur.rowcount

    def claim_updates(self, limit: int, *, priority: int = 0) -> list[sqlite3.Row]:
        if limit <= 0:
            return []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT update_id, payload, attempts, priority FROM telegram_updates "
                "WHERE state = 'pending' AND priority = ? "
                "ORDER BY update_id LIMIT ?",
                (priority, limit),
            ).fetchall()
            self._conn.executemany(
                "UPDATE telegram_updates SET state = 'processing' WHERE update_id = ?",
                ((row["update_id"],) for row in rows),
            )
            return rows

    def complete_update(self, update_id: int) -> None:
        self._exec(
            "UPDATE telegram_updates SET state = 'done', payload = NULL, "
            "processed_at = ?, last_error = NULL WHERE update_id = ?",
            (time.time(), update_id),
        )

    def fail_update(self, update_id: int, error: str) -> None:
        """保留错误供排查；处理器可能已执行外部操作，不能自动重试。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT attempts FROM telegram_updates WHERE update_id = ?", (update_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"更新不存在: {update_id}")
            self._conn.execute(
                "UPDATE telegram_updates SET state = 'failed', payload = NULL, attempts = ?, "
                "last_error = ?, processed_at = ? WHERE update_id = ?",
                (
                    row["attempts"] + 1,
                    error[:500],
                    time.time(),
                    update_id,
                ),
            )

    def update_stats(self) -> dict[str, int | float | None]:
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) FILTER (WHERE state IN ('pending', 'processing')) AS pending, "
                "count(*) FILTER (WHERE state = 'processing') AS processing, "
                "count(*) FILTER (WHERE state = 'failed') AS failed, "
                "min(received_at) FILTER (WHERE state IN ('pending', 'processing')) AS oldest "
                "FROM telegram_updates"
            ).fetchone()
        return {
            "pending": row["pending"],
            "processing": row["processing"],
            "failed": row["failed"],
            "oldest_pending_seconds": (
                round(time.time() - row["oldest"], 1) if row["oldest"] is not None else None
            ),
        }

    def prune_updates(self, *, keep_done_days: int = 7, keep_failed_days: int = 30) -> None:
        self._exec(
            "DELETE FROM telegram_updates WHERE "
            "(state = 'done' AND processed_at < ?) OR "
            "(state = 'failed' AND processed_at < ?)",
            (
                time.time() - keep_done_days * 86400,
                time.time() - keep_failed_days * 86400,
            ),
        )
