"""SQLite database management with schema, migrations, and atomic writes."""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import contextmanager


SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL DEFAULT 'cline_exec',
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'QUEUED',
    cline_exe_path TEXT,
    cline_pid INTEGER,
    cline_exit_code INTEGER,
    cline_cmd_hash TEXT,
    cline_start_time REAL,
    cline_cwd TEXT,
    cline_ppid INTEGER,
    run_id TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 1,
    prompt TEXT,
    log_stdout TEXT,
    log_stderr TEXT,
    result_file TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    error_summary TEXT,
    review_required INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    trigger TEXT,
    pid INTEGER,
    log_path TEXT,
    model_called INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_locks (
    lock_name TEXT PRIMARY KEY,
    holder_task_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transitions_task ON state_transitions(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""


class Database:
    """Thread-safe SQLite database manager."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self._local = threading.local()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            cur = conn.execute("SELECT MAX(version) FROM schema_version")
            row = cur.fetchone()
            if row[0] is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def transaction(self):
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        conn = self._get_conn()
        return conn.execute(sql, params)

    def fetch_one(self, sql: str, params=()) -> Optional[Dict[str, Any]]:
        cur = self.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, params=()) -> List[Dict[str, Any]]:
        cur = self.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def commit(self):
        self._get_conn().commit()

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ---- Task CRUD ----

    def create_task(self, task_id: str, task_type: str = "cline_exec",
                    prompt: str = "", priority: int = 0, max_retries: int = 1,
                    cline_exe_path: str = "") -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO tasks (id, task_type, priority, state, prompt,
                   max_retries, cline_exe_path, created_at)
                   VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?)""",
                (task_id, task_type, priority, prompt, max_retries, cline_exe_path, now)
            )
            self._log_transition(conn, task_id, None, "QUEUED", "task_created")
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def get_tasks_by_state(self, state: str) -> List[Dict[str, Any]]:
        return self.fetch_all("SELECT * FROM tasks WHERE state = ?", (state,))

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        return self.fetch_all("SELECT * FROM tasks ORDER BY created_at DESC")

    def update_task_state(self, task_id: str, new_state: str, trigger: str = "",
                           pid: int = None, model_called: bool = False):
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        old_state = task["state"]
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, completed_at = ? WHERE id = ?",
                (new_state, now if new_state in ("CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED", "CANCELLED") else None, task_id)
            )
            self._log_transition(conn, task_id, old_state, new_state, trigger,
                                 pid=pid, model_called=model_called,
                                 retry_count=task.get("retry_count", 0))

    def update_task_field(self, task_id: str, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [task_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", values)

    def increment_retry(self, task_id: str):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET retry_count = retry_count + 1 WHERE id = ?",
                (task_id,)
            )

    def _log_transition(self, conn, task_id: str, old_state: Optional[str],
                        new_state: str, trigger: str = "", pid: int = None,
                        log_path: str = None, model_called: bool = False,
                        retry_count: int = 0):
        conn.execute(
            """INSERT INTO state_transitions
               (task_id, old_state, new_state, trigger, pid, log_path, model_called, retry_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, old_state, new_state, trigger, pid, log_path,
             1 if model_called else 0, retry_count)
        )

    # ---- Lock management ----

    def acquire_lock(self, lock_name: str, task_id: str, ttl_seconds: int = 300) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        from datetime import timedelta
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        try:
            with self.transaction() as conn:
                existing = conn.execute(
                    "SELECT holder_task_id FROM task_locks WHERE lock_name = ? AND expires_at > ?",
                    (lock_name, now)
                ).fetchone()
                if existing:
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO task_locks (lock_name, holder_task_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                    (lock_name, task_id, now, expires)
                )
            return True
        except Exception:
            return False

    def release_lock(self, lock_name: str, task_id: str):
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM task_locks WHERE lock_name = ? AND holder_task_id = ?",
                (lock_name, task_id)
            )

    # ---- Config kv ----

    def set_kv(self, key: str, value: str):
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now)
            )

    def get_kv(self, key: str) -> Optional[str]:
        row = self.fetch_one("SELECT value FROM kv_store WHERE key = ?", (key,))
        return row["value"] if row else None
