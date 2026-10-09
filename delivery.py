"""Durable Telegram replies; retrying delivery never repeats model/tool execution."""
import asyncio
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class DeliveryStore:
    """A small outbox in the application's SQLite file, separate from its inbox."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._user_locks: dict[int, asyncio.Lock] = {}
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS reply_deliveries (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL, update_id INTEGER, session_id TEXT,
                    full_text TEXT NOT NULL DEFAULT '',
                    execution_state TEXT NOT NULL DEFAULT 'generating',
                    error_kind TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reply_deliveries_user
                    ON reply_deliveries(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS reply_delivery_chunks (
                    reply_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL, message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    sent_text TEXT, error_kind TEXT,
                    PRIMARY KEY(reply_id, ordinal),
                    FOREIGN KEY(reply_id) REFERENCES reply_deliveries(id) ON DELETE CASCADE
                );
            """)

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    def recover_interrupted(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE reply_deliveries SET execution_state='interrupted', updated_at=? "
                "WHERE execution_state='generating'", (time.time(),),
            )
            self._conn.execute(
                "UPDATE reply_delivery_chunks SET status=CASE WHEN message_id IS NULL "
                "THEN 'uncertain' ELSE 'pending' END WHERE status='sending'"
            )
            return cur.rowcount

    def create(self, user_id: int, chat_id: int, update_id: int | None,
               session_id: str | None) -> str:
        reply_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO reply_deliveries(id,user_id,chat_id,update_id,session_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)", (reply_id, user_id, chat_id, update_id, session_id, now, now),
            )
        return reply_id

    def checkpoint(self, reply_id: str, text: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE reply_deliveries SET full_text=?,updated_at=? WHERE id=?",
                               (text, time.time(), reply_id))

    def finish(self, reply_id: str, text: str, *, complete: bool,
               error_kind: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE reply_deliveries SET full_text=?,execution_state=?,error_kind=?,updated_at=? WHERE id=?",
                (text, "complete" if complete else "incomplete", error_kind, time.time(), reply_id),
            )

    def get(self, user_id: int, reply_id: str | None = None):
        with self._lock:
            if reply_id is not None:
                return self._conn.execute(
                    "SELECT * FROM reply_deliveries WHERE user_id=? AND id=?", (user_id, reply_id),
                ).fetchone()
            return self._conn.execute(
                "SELECT * FROM reply_deliveries WHERE user_id=? AND full_text<>'' "
                "AND execution_state<>'generating' ORDER BY created_at DESC LIMIT 1", (user_id,),
            ).fetchone()

    def chunks(self, reply_id: str):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM reply_delivery_chunks WHERE reply_id=? ORDER BY ordinal", (reply_id,),
            ).fetchall()

    def stage(self, reply_id: str, ordinal: int, text: str, message_id: int | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO reply_delivery_chunks(reply_id,ordinal,text,message_id) VALUES(?,?,?,?) "
                "ON CONFLICT(reply_id,ordinal) DO UPDATE SET text=excluded.text, "
                "message_id=coalesce(excluded.message_id,reply_delivery_chunks.message_id), "
                "status=CASE WHEN reply_delivery_chunks.status='uncertain' "
                "AND reply_delivery_chunks.message_id IS NULL THEN 'uncertain' "
                "WHEN reply_delivery_chunks.sent_text=excluded.text "
                "THEN reply_delivery_chunks.status ELSE 'pending' END",
                (reply_id, ordinal, text, message_id),
            )

    def plan(self, reply_id: str, chunks: list[str]) -> None:
        # Persist the complete render plan before attempting any remaining sends.
        for ordinal, text in enumerate(chunks):
            self.stage(reply_id, ordinal, text)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM reply_delivery_chunks WHERE reply_id=? AND ordinal>=?",
                               (reply_id, len(chunks)))

    def sending(self, reply_id: str, ordinal: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE reply_delivery_chunks SET status='sending',error_kind=NULL "
                               "WHERE reply_id=? AND ordinal=?", (reply_id, ordinal))

    def forget_message(self, reply_id: str, ordinal: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE reply_delivery_chunks SET message_id=NULL,status='pending' "
                               "WHERE reply_id=? AND ordinal=?", (reply_id, ordinal))

    def sent(self, reply_id: str, ordinal: int, message_id: int | None, text: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE reply_delivery_chunks SET message_id=coalesce(?,message_id),status='sent',"
                "sent_text=?,error_kind=NULL WHERE reply_id=? AND ordinal=?",
                (message_id, text, reply_id, ordinal),
            )

    def failed(self, reply_id: str, ordinal: int, *, uncertain: bool, error_kind: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE reply_delivery_chunks SET status=?,error_kind=? "
                               "WHERE reply_id=? AND ordinal=?",
                               ("uncertain" if uncertain else "pending", error_kind, reply_id, ordinal))

    def pending(self, user_id: int, limit: int = 10):
        with self._lock:
            return self._conn.execute(
                "SELECT r.*, (SELECT count(*) FROM reply_delivery_chunks c WHERE c.reply_id=r.id "
                "AND c.status<>'sent') AS pending_chunks FROM reply_deliveries r WHERE r.user_id=? "
                "AND r.full_text<>'' AND r.execution_state<>'generating' AND "
                "(NOT EXISTS(SELECT 1 FROM reply_delivery_chunks c WHERE c.reply_id=r.id) OR "
                "EXISTS(SELECT 1 FROM reply_delivery_chunks c WHERE c.reply_id=r.id AND c.status<>'sent')) "
                "ORDER BY r.created_at DESC LIMIT ?", (user_id, limit),
            ).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
