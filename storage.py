"""SQLite 存储：用户、会话映射及 Telegram webhook inbox。

表结构：
- users:    telegram 用户
- sessions: 会话记录（每个会话对应 pi 侧的一个 jsonl 会话文件）
"""
import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from costs import PriceResult, normalize_usage
from persona import DEFAULT_PERSONA, FIELDS, validate_persona

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

CREATE TABLE IF NOT EXISTS usage_runs (
    id                 TEXT PRIMARY KEY,
    user_id            INTEGER NOT NULL,
    session_id         TEXT,
    telegram_update_id INTEGER,
    started_at         REAL NOT NULL,
    finished_at        REAL,
    status             TEXT NOT NULL DEFAULT 'running',
    origin             TEXT NOT NULL DEFAULT 'chat',
    memory_context_bytes INTEGER NOT NULL DEFAULT 0, -- 每轮准备的上下文大小，不是累计注入量
    stats_before       TEXT,
    stats_after        TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_runs_user_time ON usage_runs(user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS usage_events (
    run_id             TEXT NOT NULL,
    seq                INTEGER NOT NULL,
    kind               TEXT NOT NULL,
    occurred_at        REAL NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    reasoning_tokens   INTEGER NOT NULL,
    total_tokens       INTEGER NOT NULL,
    estimated_usd      TEXT,
    price_tier         TEXT,
    price_status       TEXT NOT NULL,
    price_snapshot     TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_usage_events_run ON usage_events(run_id);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    session_id         TEXT NOT NULL,
    telegram_update_id INTEGER UNIQUE,
    user_text          TEXT NOT NULL,
    steer_text         TEXT NOT NULL DEFAULT '',
    assistant_text     TEXT NOT NULL DEFAULT '',
    has_image          INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'running',
    interruption_notice_pending INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id, id);

CREATE TABLE IF NOT EXISTS session_summaries (
    session_id      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    summary_text    TEXT NOT NULL DEFAULT '',
    covered_turn_id INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_items (
    id           TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source       TEXT NOT NULL,
    pinned       INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (user_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_items(user_id, pinned DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS session_recovery (
    session_id     TEXT PRIMARY KEY,
    pi_session_file TEXT NOT NULL,
    context_text   TEXT NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS smtp_settings (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL,
    username     TEXT NOT NULL,
    password     TEXT NOT NULL,
    from_address TEXT NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_drafts (
    id           TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    recipient    TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL,
    previewed_at REAL,
    confirmed_at REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_mail_drafts_user_state
    ON mail_drafts(user_id, state, created_at);

CREATE TABLE IF NOT EXISTS personas (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    identity            TEXT NOT NULL DEFAULT '',
    personality         TEXT NOT NULL DEFAULT '',
    communication_style TEXT NOT NULL DEFAULT '',
    behavior_rules      TEXT NOT NULL DEFAULT '',
    background          TEXT NOT NULL DEFAULT '',
    preferences         TEXT NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_persona_selection (
    user_id    INTEGER PRIMARY KEY,
    persona_id TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_model_selection (
    user_id    INTEGER PRIMARY KEY,
    provider   TEXT NOT NULL,
    model_id   TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id      INTEGER PRIMARY KEY,
    payload        TEXT,
    state          TEXT NOT NULL DEFAULT 'pending',
    priority       INTEGER NOT NULL DEFAULT 0,
    user_id        INTEGER,
    session_id     TEXT,
    queueable      INTEGER NOT NULL DEFAULT 0,
    failure_notice_pending INTEGER NOT NULL DEFAULT 0,
    failure_notice_sent_at REAL,
    safe_to_retry  INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    received_at    REAL NOT NULL,
    processed_at   REAL,
    acknowledged_at REAL,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_pending
    ON telegram_updates(state, update_id);
"""


class UpdateQueueFull(Exception):
    """Telegram 可重试的更新队列容量已用尽。"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # inbox 暂存 Telegram 消息正文，不能让同机其他用户读取数据库文件。
        if os.name == "posix" and db_path != ":memory:":
            os.chmod(db_path, 0o600)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(usage_runs)")}
            if "origin" not in columns:
                self._conn.execute("ALTER TABLE usage_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'chat'")
            if "memory_context_bytes" not in columns:
                self._conn.execute("ALTER TABLE usage_runs ADD COLUMN memory_context_bytes INTEGER NOT NULL DEFAULT 0")
            turn_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(conversation_turns)")}
            if "interruption_notice_pending" not in turn_columns:
                self._conn.execute(
                    "ALTER TABLE conversation_turns ADD COLUMN interruption_notice_pending INTEGER NOT NULL DEFAULT 0"
                )
            update_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(telegram_updates)")}
            if "acknowledged_at" not in update_columns:
                self._conn.execute("ALTER TABLE telegram_updates ADD COLUMN acknowledged_at REAL")
            if "safe_to_retry" not in update_columns:
                self._conn.execute("ALTER TABLE telegram_updates ADD COLUMN safe_to_retry INTEGER NOT NULL DEFAULT 0")
            if "user_id" not in update_columns:
                self._conn.execute("ALTER TABLE telegram_updates ADD COLUMN user_id INTEGER")
            if "queueable" not in update_columns:
                self._conn.execute("ALTER TABLE telegram_updates ADD COLUMN queueable INTEGER NOT NULL DEFAULT 0")
            if "session_id" not in update_columns:
                self._conn.execute("ALTER TABLE telegram_updates ADD COLUMN session_id TEXT")
            if "failure_notice_pending" not in update_columns:
                self._conn.execute(
                    "ALTER TABLE telegram_updates ADD COLUMN failure_notice_pending INTEGER NOT NULL DEFAULT 0"
                )
            if "failure_notice_sent_at" not in update_columns:
                self._conn.execute("ALTER TABLE telegram_updates ADD COLUMN failure_notice_sent_at REAL")
            default = validate_persona(DEFAULT_PERSONA)
            self._conn.execute(
                "INSERT OR IGNORE INTO personas (id, name, identity, personality, communication_style, "
                "behavior_rules, background, preferences, enabled, created_at, updated_at) "
                "VALUES ('default', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (default["name"], *(default[key] for key, _ in FIELDS), time.time(), time.time()),
            )
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def get_runtime_settings(self) -> dict[str, object]:
        return {row["key"]: json.loads(row["value_json"])
                for row in self._query("SELECT key, value_json FROM runtime_settings")}

    def save_runtime_settings(self, values: dict[str, object]) -> None:
        with self._lock, self._conn:
            for key, value in values.items():
                self._conn.execute(
                    "INSERT INTO runtime_settings (key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), time.time()),
                )

    def get_smtp_settings(self) -> sqlite3.Row | None:
        rows = self._query("SELECT host, port, username, password, from_address, updated_at "
                           "FROM smtp_settings WHERE id = 1")
        return rows[0] if rows else None

    def save_smtp_settings(self, host: str, port: int, username: str,
                           password: str, from_address: str) -> None:
        self._exec(
            "INSERT INTO smtp_settings (id, host, port, username, password, from_address, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "host=excluded.host, port=excluded.port, username=excluded.username, "
            "password=excluded.password, from_address=excluded.from_address, updated_at=excluded.updated_at",
            (host, port, username, password, from_address, time.time()),
        )

    # ---------- 人格 ----------
    def list_personas(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM personas ORDER BY (id = 'default') DESC, enabled DESC, updated_at DESC")

    def get_persona(self, persona_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM personas WHERE id = ?", (persona_id,))
        return rows[0] if rows else None

    def get_active_persona(self, user_id: int) -> sqlite3.Row | None:
        selection = self._query("SELECT persona_id FROM user_persona_selection WHERE user_id = ?", (user_id,))
        if selection and selection[0]["persona_id"] == "none":
            return None
        rows = self._query(
            "SELECT p.* FROM user_persona_selection AS s JOIN personas AS p ON p.id = s.persona_id "
            "WHERE s.user_id = ? AND p.enabled = 1", (user_id,),
        )
        return rows[0] if rows else self.get_persona("default")

    def create_persona(self, values: dict[str, str]) -> str:
        values = validate_persona(values)
        persona_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._exec(
            "INSERT INTO personas (id, name, identity, personality, communication_style, behavior_rules, "
            "background, preferences, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (persona_id, values["name"], *(values[key] for key, _ in FIELDS), now, now),
        )
        return persona_id

    def update_persona(self, persona_id: str, values: dict[str, str]) -> bool:
        values = validate_persona(values)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE personas SET name = ?, identity = ?, personality = ?, communication_style = ?, "
                "behavior_rules = ?, background = ?, preferences = ?, updated_at = ? WHERE id = ?",
                (values["name"], *(values[key] for key, _ in FIELDS), time.time(), persona_id),
            )
            return cur.rowcount == 1

    def set_persona_enabled(self, persona_id: str, enabled: bool) -> bool:
        if persona_id == "default" and not enabled:
            raise ValueError("默认人格不能停用")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE personas SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), time.time(), persona_id),
            )
            if cur.rowcount and not enabled:
                self._conn.execute("DELETE FROM user_persona_selection WHERE persona_id = ?", (persona_id,))
            return cur.rowcount == 1

    def delete_persona(self, persona_id: str) -> bool:
        if persona_id == "default":
            raise ValueError("默认人格不能删除")
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM personas WHERE id = ?", (persona_id,))
            if cur.rowcount:
                self._conn.execute("DELETE FROM user_persona_selection WHERE persona_id = ?", (persona_id,))
            return cur.rowcount == 1

    def set_active_persona(self, user_id: int, persona_id: str) -> None:
        with self._lock, self._conn:
            if persona_id != "none":
                row = self._conn.execute("SELECT enabled FROM personas WHERE id = ?", (persona_id,)).fetchone()
                if row is None or not row["enabled"]:
                    raise ValueError("人格不存在或已停用")
            self._conn.execute(
                "INSERT INTO user_persona_selection (user_id, persona_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET persona_id = excluded.persona_id, "
                "updated_at = excluded.updated_at",
                (user_id, persona_id, time.time()),
            )

    def list_admin_users(self) -> list[sqlite3.Row]:
        return self._query("SELECT user_id, username FROM users ORDER BY user_id")

    def list_admin_summaries(self, user_id: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT s.id, s.name, s.status, ss.summary_text, ss.covered_turn_id, ss.updated_at "
            "FROM sessions AS s LEFT JOIN session_summaries AS ss ON ss.session_id = s.id "
            "WHERE s.user_id = ? ORDER BY s.updated_at DESC", (user_id,),
        )

    # ---------- users ----------
    def ensure_user(self, user_id: int, username: str | None) -> None:
        self._exec(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username),
        )

    # ---------- model-created mail drafts ----------
    def create_mail_draft(self, user_id: int, recipient: str, subject: str, body: str) -> str:
        draft_id = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO mail_drafts (id, user_id, recipient, subject, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (draft_id, user_id, recipient, subject, body, time.time()),
        )
        return draft_id

    def list_pending_mail_drafts(self, user_id: int, *, unpreviewed_only: bool = False) -> list[sqlite3.Row]:
        condition = " AND previewed_at IS NULL" if unpreviewed_only else ""
        return self._query(
            "SELECT id, recipient, subject, body, created_at FROM mail_drafts "
            "WHERE user_id = ? AND state = 'pending' AND created_at > ?"
            + condition + " ORDER BY created_at LIMIT 10",
            (user_id, time.time() - 86400),
        )

    def get_mail_draft(self, user_id: int, draft_id: str) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM mail_drafts WHERE user_id = ? AND id = ?",
            (user_id, draft_id),
        )
        return rows[0] if rows else None

    def mark_mail_previewed(self, user_id: int, draft_id: str) -> None:
        self._exec(
            "UPDATE mail_drafts SET previewed_at = ? "
            "WHERE user_id = ? AND id = ? AND state = 'pending'",
            (time.time(), user_id, draft_id),
        )

    def claim_mail_draft(self, user_id: int, draft_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE mail_drafts SET state = 'sending', confirmed_at = ? "
                "WHERE user_id = ? AND id = ? AND state = 'pending' AND created_at > ?",
                (time.time(), user_id, draft_id, time.time() - 86400),
            )
            return cursor.rowcount == 1

    def finish_mail_draft(self, user_id: int, draft_id: str, *, sent: bool) -> None:
        self._exec(
            "UPDATE mail_drafts SET state = ?, finished_at = ? "
            "WHERE user_id = ? AND id = ? AND state = 'sending'",
            ("sent" if sent else "failed", time.time(), user_id, draft_id),
        )

    def cancel_mail_draft(self, user_id: int, draft_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE mail_drafts SET state = 'cancelled', finished_at = ? "
                "WHERE user_id = ? AND id = ? AND state = 'pending'",
                (time.time(), user_id, draft_id),
            )
            return cursor.rowcount == 1

    def get_user_model(self, user_id: int) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT provider, model_id FROM user_model_selection WHERE user_id = ?",
            (user_id,),
        )
        return rows[0] if rows else None

    def set_user_model(self, user_id: int, provider: str, model_id: str) -> None:
        if not provider or not model_id:
            raise ValueError("模型 provider 和 id 不能为空")
        self._exec(
            "INSERT INTO user_model_selection (user_id, provider, model_id, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "provider = excluded.provider, model_id = excluded.model_id, updated_at = excluded.updated_at",
            (user_id, provider, model_id, time.time()),
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
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC, created_at DESC, id DESC",
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

    # ---------- 可迁移的会话摘要和长期记忆 ----------
    def start_turn(self, user_id: int, session_id: str, update_id: int | None,
                   user_text: str, has_image: bool) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO conversation_turns "
                "(user_id, session_id, telegram_update_id, user_text, has_image, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, session_id, update_id, user_text, int(has_image), time.time()),
            )
            row = self._conn.execute(
                "SELECT id FROM conversation_turns WHERE telegram_update_id = ?" if update_id is not None
                else "SELECT last_insert_rowid() AS id",
                (update_id,) if update_id is not None else (),
            ).fetchone()
            return row["id"]

    def append_turn_steer(self, turn_id: int, text: str) -> None:
        self._exec("UPDATE conversation_turns SET steer_text = steer_text || ? WHERE id = ?",
                   ("\n" + text, turn_id))

    def finish_turn(self, turn_id: int, assistant_text: str, *, complete: bool) -> None:
        self._exec("UPDATE conversation_turns SET assistant_text = ?, status = ? WHERE id = ?",
                   (assistant_text, "complete" if complete else "incomplete", turn_id))

    def fail_interrupted_turns(self) -> int:
        """进程重启后将失去执行者的回合标记为不完整，保留一次性通知。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE conversation_turns SET status = 'incomplete', interruption_notice_pending = 1 "
                "WHERE status = 'running'"
            )
            return cur.rowcount

    def pending_interruption_notice_count(self, user_id: int) -> int:
        rows = self._query(
            "SELECT count(*) AS count FROM conversation_turns "
            "WHERE user_id = ? AND interruption_notice_pending = 1", (user_id,)
        )
        return rows[0]["count"]

    def acknowledge_interruption_notice(self, user_id: int) -> None:
        self._exec(
            "UPDATE conversation_turns SET interruption_notice_pending = 0 "
            "WHERE user_id = ? AND interruption_notice_pending = 1", (user_id,)
        )

    def get_summary(self, session_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM session_summaries WHERE session_id = ?", (session_id,))
        return rows[0] if rows else None

    def pending_summary_turns(self, session_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND status = 'complete' "
            "AND id > COALESCE((SELECT covered_turn_id FROM session_summaries WHERE session_id = ?), 0) "
            "ORDER BY id", (session_id, session_id),
        )

    def latest_turns(self, session_id: str, after_id: int = 0) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND status = 'complete' "
            "AND id > ? ORDER BY id", (session_id, after_id),
        )

    def apply_summary(self, user_id: int, session_id: str, previous_covered_id: int,
                      covered_id: int, summary: str, candidates: list[str],
                      *, memory_limit: int, turn_retention: int) -> None:
        now = time.time()
        with self._lock, self._conn:
            old = self._conn.execute(
                "SELECT covered_turn_id FROM session_summaries WHERE session_id = ?", (session_id,)
            ).fetchone()
            if (old[0] if old else 0) != previous_covered_id:
                raise ValueError("会话摘要已被其他任务更新")
            self._conn.execute(
                "INSERT INTO session_summaries (session_id, user_id, summary_text, covered_turn_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "summary_text=excluded.summary_text, covered_turn_id=excluded.covered_turn_id, "
                "updated_at=excluded.updated_at",
                (session_id, user_id, summary, covered_id, now),
            )
            count = self._conn.execute(
                "SELECT count(*) FROM memory_items WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            for item in candidates:
                if count >= memory_limit:
                    break
                digest = hashlib.sha256(item.casefold().strip().encode("utf-8")).hexdigest()
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO memory_items "
                    "(id, user_id, text, content_hash, source, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'auto', ?, ?)",
                    (uuid.uuid4().hex[:12], user_id, item, digest, now, now),
                )
                count += cur.rowcount
            self._conn.execute(
                "DELETE FROM conversation_turns WHERE session_id = ? AND id <= ? "
                "AND id NOT IN (SELECT id FROM conversation_turns WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (session_id, covered_id, session_id, turn_retention),
            )

    def clear_summary_if_unchanged(self, session_id: str, updated_at: float) -> bool:
        """确认后仅清空预览时的版本，且同步清除对应恢复上下文。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE session_summaries SET summary_text = '', updated_at = ? "
                "WHERE session_id = ? AND updated_at = ? AND summary_text <> ''",
                (time.time(), session_id, updated_at),
            )
            if cur.rowcount:
                self._conn.execute(
                    "UPDATE session_recovery SET context_text = '' WHERE session_id = ?",
                    (session_id,),
                )
            return cur.rowcount == 1

    def clear_summary(self, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE session_summaries SET summary_text = '', updated_at = ? WHERE session_id = ?",
                               (time.time(), session_id))
            self._conn.execute("UPDATE session_recovery SET context_text = '' WHERE session_id = ?",
                               (session_id,))

    def list_memories(self, user_id: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM memory_items WHERE user_id = ? ORDER BY pinned DESC, updated_at DESC", (user_id,),
        )

    def get_memory(self, user_id: int, memory_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM memory_items WHERE user_id = ? AND id = ?", (user_id, memory_id))
        return rows[0] if rows else None

    def add_memory(self, user_id: int, item: str, *, limit: int, source: str = "manual") -> str:
        digest = hashlib.sha256(item.casefold().strip().encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id FROM memory_items WHERE user_id = ? AND content_hash = ?", (user_id, digest)
            ).fetchone()
            if existing:
                return existing["id"]
            count = self._conn.execute("SELECT count(*) FROM memory_items WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= limit:
                raise ValueError("记忆已达到数量上限，请先删除或编辑已有记忆")
            memory_id = uuid.uuid4().hex[:12]
            self._conn.execute(
                "INSERT INTO memory_items (id, user_id, text, content_hash, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (memory_id, user_id, item, digest, source, now, now),
            )
            return memory_id

    def edit_memory(self, user_id: int, memory_id: str, item: str) -> bool:
        digest = hashlib.sha256(item.casefold().strip().encode("utf-8")).hexdigest()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE memory_items SET text = ?, content_hash = ?, updated_at = ? "
                "WHERE user_id = ? AND id = ?", (item, digest, time.time(), user_id, memory_id),
            )
            return cur.rowcount > 0

    def delete_memory_if_unchanged(self, user_id: int, memory_id: str, updated_at: float) -> bool:
        """确认后仅删除预览时的版本，避免旧按钮删除后来编辑的内容。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM memory_items WHERE user_id = ? AND id = ? AND updated_at = ?",
                (user_id, memory_id, updated_at),
            )
            return cur.rowcount == 1

    def delete_memory(self, user_id: int, memory_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memory_items WHERE user_id = ? AND id = ?", (user_id, memory_id))
            return cur.rowcount > 0

    def pin_memory(self, user_id: int, memory_id: str, pinned: bool) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE memory_items SET pinned = ?, updated_at = ? "
                                     "WHERE user_id = ? AND id = ?",
                                     (int(pinned), time.time(), user_id, memory_id))
            return cur.rowcount > 0

    def set_session_recovery(self, session_id: str, pi_session_file: str, context_text: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET pi_session_file = ? WHERE id = ?",
                               (pi_session_file, session_id))
            self._conn.execute(
                "INSERT INTO session_recovery (session_id, pi_session_file, context_text, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "pi_session_file=excluded.pi_session_file, context_text=excluded.context_text, "
                "updated_at=excluded.updated_at",
                (session_id, pi_session_file, context_text, time.time()),
            )

    def get_session_recovery(self, session_id: str, pi_session_file: str | None) -> str:
        rows = self._query("SELECT context_text FROM session_recovery WHERE session_id = ? "
                           "AND pi_session_file = ?", (session_id, pi_session_file))
        return rows[0]["context_text"] if rows else ""

    # ---------- LLM 用量账本 ----------
    def start_usage_run(
        self, user_id: int, session_id: str | None, telegram_update_id: int | None,
        stats_before: dict | None, *, origin: str = "chat", memory_context_bytes: int = 0,
    ) -> str:
        run_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO usage_runs (id, user_id, session_id, telegram_update_id, started_at, stats_before, "
            "origin, memory_context_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, user_id, session_id, telegram_update_id, time.time(),
             json.dumps(stats_before) if stats_before is not None else None, origin, memory_context_bytes),
        )
        return run_id

    def record_usage_event(self, run_id: str, seq: int, event: dict, price: PriceResult) -> None:
        usage = normalize_usage(event["usage"])
        self._exec(
            "INSERT INTO usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, seq, event["kind"], event["occurred_at"], event["provider"], event["model"],
             usage["input"], usage["cacheRead"], usage["cacheWrite"], usage["output"],
             usage["reasoning"], usage["totalTokens"], price.usd, price.tier,
             price.status, price.snapshot),
        )

    def finish_usage_run(self, run_id: str, status: str, stats_after: dict | None) -> None:
        self._exec(
            "UPDATE usage_runs SET status = ?, finished_at = ?, stats_after = ? WHERE id = ?",
            (status, time.time(), json.dumps(stats_after) if stats_after is not None else None, run_id),
        )

    def get_last_usage_run(self, user_id: int, origin: str | None = None) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM usage_runs WHERE user_id = ? AND (? IS NULL OR origin = ?) "
            "ORDER BY started_at DESC LIMIT 1", (user_id, origin, origin),
        )
        return rows[0] if rows else None

    def get_usage_runs_since(self, user_id: int, since: float) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM usage_runs WHERE user_id = ? AND started_at >= ? ORDER BY started_at DESC",
            (user_id, since),
        )

    def get_usage_events(self, run_id: str) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM usage_events WHERE run_id = ? ORDER BY seq", (run_id,))

    def get_usage_events_since(self, user_id: int, since: float) -> list[sqlite3.Row]:
        return self._query(
            "SELECT e.*, r.origin, r.started_at, r.status AS run_status "
            "FROM usage_events AS e JOIN usage_runs AS r ON r.id = e.run_id "
            "WHERE r.user_id = ? AND r.started_at >= ? ORDER BY r.started_at DESC, e.seq",
            (user_id, since),
        )

    def get_recent_usage_runs(self, user_id: int, limit: int = 8) -> list[sqlite3.Row]:
        return self._query(
            "SELECT id, session_id, started_at, status, origin, memory_context_bytes "
            "FROM usage_runs WHERE user_id = ? ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        )

    # ---------- Telegram webhook inbox ----------
    # 更新先持久化再 ACK；重启后未处理记录仍可重放。
    def enqueue_update(
        self, update_id: int, payload: str, *, capacity: int = 1000, priority: int = 0,
        user_id: int | None = None, session_id: str | None = None,
        queueable: bool = False,
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
                "INSERT INTO telegram_updates (update_id, payload, priority, user_id, session_id, queueable, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (update_id, payload, priority, user_id, session_id, int(queueable), time.time()),
            )
            return True

    def next_update(self) -> sqlite3.Row | None:
        """诊断用：取最早待处理更新。"""
        rows = self._query(
            "SELECT update_id, payload, attempts FROM telegram_updates "
            "WHERE state = 'pending' ORDER BY update_id LIMIT 1"
        )
        return rows[0] if rows else None

    def begin_update(self, update_id: int) -> bool:
        """准备执行用户消息；取消后的消息不得开始。无 inbox 行供单元调用。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT state FROM telegram_updates WHERE update_id = ?", (update_id,)
            ).fetchone()
            if row is None:
                return True
            if row["state"] != "processing":
                return False
            self._conn.execute(
                "UPDATE telegram_updates SET safe_to_retry = 0 WHERE update_id = ?",
                (update_id,),
            )
            return True

    def begin_album_updates(self, update_ids: list[int]) -> bool:
        """整组相册只在全部更新仍可处理时启动，并一起标记为不可安全重放。"""
        if not update_ids or len(set(update_ids)) != len(update_ids):
            return False
        placeholders = ",".join("?" for _ in update_ids)
        with self._lock, self._conn:
            rows = self._conn.execute(
                f"SELECT state FROM telegram_updates WHERE update_id IN ({placeholders})",
                tuple(update_ids),
            ).fetchall()
            if len(rows) != len(update_ids) or any(row["state"] != "processing" for row in rows):
                return False
            self._conn.execute(
                f"UPDATE telegram_updates SET safe_to_retry = 0 WHERE update_id IN ({placeholders})",
                tuple(update_ids),
            )
            return True

    def set_update_session(self, update_id: int, session_id: str) -> None:
        self._exec(
            "UPDATE telegram_updates SET session_id = ? WHERE update_id = ? AND state = 'processing'",
            (session_id, update_id),
        )

    def get_failure_notice(self, update_id: int) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT u.update_id, u.user_id, u.session_id, s.name AS session_name "
            "FROM telegram_updates AS u LEFT JOIN sessions AS s ON s.id = u.session_id "
            "WHERE u.update_id = ? AND u.failure_notice_pending = 1",
            (update_id,),
        )
        return rows[0] if rows else None

    def get_pending_failure_notices(self, user_id: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT u.update_id, u.user_id, u.session_id, s.name AS session_name "
            "FROM telegram_updates AS u LEFT JOIN sessions AS s ON s.id = u.session_id "
            "WHERE u.user_id = ? AND u.failure_notice_pending = 1 "
            "AND u.state IN ('failed', 'acknowledged') ORDER BY u.received_at LIMIT 10",
            (user_id,),
        )

    def mark_failure_notice_sent(self, update_id: int) -> None:
        self._exec(
            "UPDATE telegram_updates SET failure_notice_pending = 0, failure_notice_sent_at = ? "
            "WHERE update_id = ? AND failure_notice_pending = 1",
            (time.time(), update_id),
        )

    def count_user_queue(self, user_id: int) -> int:
        """尚未开始的普通消息数量，含待分发和已分发但仍在等待的更新。"""
        rows = self._query(
            "SELECT count(*) AS n FROM telegram_updates WHERE user_id = ? AND queueable = 1 "
            "AND (state = 'pending' OR (state = 'processing' AND safe_to_retry = 1))",
            (user_id,),
        )
        return rows[0]["n"]

    def cancel_user_queue(self, user_id: int) -> int:
        """取消本用户尚未开始的普通消息；保留去重 ID。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE telegram_updates SET state = 'done', payload = NULL, "
                "safe_to_retry = 0, processed_at = ? WHERE user_id = ? AND queueable = 1 "
                "AND (state = 'pending' OR (state = 'processing' AND safe_to_retry = 1))",
                (time.time(), user_id),
            )
            return cur.rowcount

    def fail_interrupted_updates(self) -> int:
        """进程中断后的执行结果不确定，不能自动重放可能有副作用的指令。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE telegram_updates SET state = 'pending', safe_to_retry = 0 "
                "WHERE state = 'processing' AND safe_to_retry = 1"
            )
            cur = self._conn.execute(
                "UPDATE telegram_updates SET state = 'failed', payload = NULL, processed_at = ?, "
                "failure_notice_pending = CASE WHEN user_id IS NOT NULL THEN 1 ELSE 0 END, "
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
                "UPDATE telegram_updates SET state = 'processing', safe_to_retry = queueable "
                "WHERE update_id = ?",
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
                "failure_notice_pending = CASE WHEN user_id IS NOT NULL THEN 1 ELSE 0 END, "
                "last_error = ?, processed_at = ? WHERE update_id = ?",
                (
                    row["attempts"] + 1,
                    error[:500],
                    time.time(),
                    update_id,
                ),
            )

    def acknowledge_failed_update(self, update_id: int) -> bool:
        """保留记录与去重 ID，仅从待处理故障中移除。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE telegram_updates SET state = 'acknowledged', acknowledged_at = ? "
                "WHERE update_id = ? AND state = 'failed'",
                (time.time(), update_id),
            )
            return cur.rowcount == 1

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

    def get_recent_failed_updates(self, limit: int = 5) -> list[sqlite3.Row]:
        """后台只取故障元数据；不读取 Telegram 原始消息载荷。"""
        return self._query(
            "SELECT update_id, processed_at, attempts, last_error FROM telegram_updates "
            "WHERE state = 'failed' ORDER BY processed_at DESC LIMIT ?", (limit,),
        )

    def prune_updates(self, *, keep_done_days: int = 7, keep_failed_days: int = 30) -> None:
        self._exec(
            "DELETE FROM telegram_updates WHERE "
            "(state = 'done' AND processed_at < ?) OR "
            "(state IN ('failed', 'acknowledged') AND processed_at < ?)",
            (
                time.time() - keep_done_days * 86400,
                time.time() - keep_failed_days * 86400,
            ),
        )
