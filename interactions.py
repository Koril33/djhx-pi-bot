"""待确认的转写、图片请求与 Pi 对话框；按用户隔离、过期、原子消费。"""

import json
import sqlite3
import threading
import time
import uuid


class PendingActions:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.execute("""CREATE TABLE IF NOT EXISTS pending_actions (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT, state TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL, expires_at REAL NOT NULL
            )""")
            self._conn.execute("CREATE INDEX IF NOT EXISTS actions_owner ON pending_actions(user_id, state)")
        self.prune()

    def create(self, user_id: int, chat_id: int, kind: str, payload: dict,
               *, ttl: float = 900) -> str:
        value = json.dumps(payload, ensure_ascii=False)
        if len(value.encode()) > 128 * 1024 or ttl <= 0:
            raise ValueError("待确认内容过大或有效期无效")
        ident = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._conn:
            # 有界保留：同用户同类最多八个待确认请求。
            self._conn.execute("UPDATE pending_actions SET state='superseded',payload=NULL "
                               "WHERE user_id=? AND kind=? AND state='pending' AND id NOT IN "
                               "(SELECT id FROM pending_actions WHERE user_id=? AND kind=? "
                               "AND state='pending' ORDER BY created_at DESC LIMIT 7)",
                               (user_id, kind, user_id, kind))
            self._conn.execute("INSERT INTO pending_actions "
                               "(id,user_id,chat_id,kind,payload,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                               (ident, user_id, chat_id, kind, value, now, now + ttl))
        return ident

    def get(self, user_id: int, ident: str, kind: str | None = None) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM pending_actions WHERE id=? AND user_id=? "
                                     "AND state='pending' AND expires_at>?", (ident, user_id, time.time())).fetchone()
            if row is None or (kind is not None and row["kind"] != kind):
                return None
            result = dict(row)
            result["payload"] = json.loads(row["payload"])
            return result

    def claim(self, user_id: int, ident: str, kind: str | None = None) -> dict | None:
        with self._lock, self._conn:
            row = self.get(user_id, ident, kind)
            if row is None:
                return None
            changed = self._conn.execute("UPDATE pending_actions SET state='used',payload=NULL "
                                         "WHERE id=? AND user_id=? AND state='pending'", (ident, user_id))
            return row if changed.rowcount else None

    def cancel(self, user_id: int, ident: str) -> bool:
        with self._lock, self._conn:
            return bool(self._conn.execute("UPDATE pending_actions SET state='cancelled',payload=NULL "
                                           "WHERE id=? AND user_id=? AND state='pending'", (ident, user_id)).rowcount)

    def prune(self) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("UPDATE pending_actions SET state='expired',payload=NULL "
                               "WHERE state='pending' AND expires_at<=?", (now,))
            self._conn.execute("DELETE FROM pending_actions WHERE created_at<? AND state!='pending'",
                               (now - 7 * 86400,))

    def close(self) -> None:
        self._conn.close()
