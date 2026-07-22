"""SQLite database management with schema v3: goals, events, training, model_calls."""

import os
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import contextmanager


SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT,
    parent_task_id TEXT,
    task_type TEXT NOT NULL DEFAULT 'cline_exec',
    task_sequence INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'QUEUED',
    cline_exe_path TEXT,
    cline_pid INTEGER,
    cline_exit_code INTEGER,
    cline_cmd_hash TEXT,
    cline_cmdline TEXT,
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
    structured_result TEXT,
    training_requested INTEGER NOT NULL DEFAULT 0,
    training_command TEXT,
    training_cwd TEXT,
    training_env TEXT,
    gpu_requirements TEXT,
    training_pid INTEGER,
    training_log TEXT,
    output_path TEXT,
    metrics_path TEXT,
    training_state TEXT,
    training_exit_code INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    error_summary TEXT,
    review_required INTEGER NOT NULL DEFAULT 0,
    safety_violations TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'GOAL_CREATED',
    current_task_id TEXT,
    task_sequence INTEGER NOT NULL DEFAULT 0,
    completion_criteria TEXT,
    stop_conditions TEXT,
    latest_codex_decision TEXT,
    max_iterations INTEGER NOT NULL DEFAULT 10,
    max_failures INTEGER NOT NULL DEFAULT 3,
    iteration_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    model_call_budget INTEGER NOT NULL DEFAULT 100,
    accumulated_model_calls INTEGER NOT NULL DEFAULT 0,
    review_mode TEXT NOT NULL DEFAULT 'auto',
    pending_event_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    error_summary TEXT,
    workspace_cwd TEXT NOT NULL DEFAULT '',
    skip_next_plan INTEGER NOT NULL DEFAULT 0,
    orchestrator_step TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    goal_id TEXT,
    task_id TEXT,
    event_type TEXT NOT NULL,
    payload TEXT,
    state_version INTEGER NOT NULL DEFAULT 1,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT,
    handled_by TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT,
    task_id TEXT,
    model_role TEXT NOT NULL,
    reason TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL DEFAULT 'started',
    estimated_usage INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    result_summary TEXT
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
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_goals_state ON goals(state);
CREATE INDEX IF NOT EXISTS idx_events_goal ON events(goal_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_unack ON events(acknowledged, created_at);
CREATE INDEX IF NOT EXISTS idx_events_idempotency ON events(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_goal ON model_calls(goal_id);
CREATE INDEX IF NOT EXISTS idx_tasks_training ON tasks(training_state);
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
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            cur = conn.execute("SELECT MAX(version) FROM schema_version")
            row = cur.fetchone()
            current = row[0] if row[0] is not None else 0
            if current < SCHEMA_VERSION:
                self._run_migrations(conn, current, SCHEMA_VERSION)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,)
                )
            elif current == 0:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,)
                )
            conn.commit()

    def _run_migrations(self, conn, from_version: int, to_version: int):
        """Run schema migrations."""
        if from_version < 2:
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)"
                )
            except sqlite3.OperationalError:
                pass
        if from_version < 3:
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN goal_id TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN task_sequence INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN structured_result TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_requested INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_command TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_cwd TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_env TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN gpu_requirements TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_pid INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_log TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN output_path TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN metrics_path TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_state TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN training_exit_code INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN cline_cmdline TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_training ON tasks(training_state)")
        if from_version < 4:
            try:
                conn.execute("ALTER TABLE goals ADD COLUMN skip_next_plan INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE goals ADD COLUMN orchestrator_step TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN safety_violations TEXT")
            except sqlite3.OperationalError:
                pass

        if from_version < 5:
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_idempotency ON events(idempotency_key)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)"
                )
            except sqlite3.OperationalError:
                pass

        if from_version < 6:
            try:
                conn.execute("ALTER TABLE goals ADD COLUMN workspace_cwd TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA busy_timeout=5000")
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

    def get_schema_version(self) -> int:
        row = self.fetch_one("SELECT MAX(version) AS version FROM schema_version")
        return int(row["version"] or 0) if row else 0

    # ---- Task CRUD ----

    def create_task(self, task_id: str, task_type: str = "cline_exec",
                    prompt: str = "", priority: int = 0, max_retries: int = 1,
                    cline_exe_path: str = "", goal_id: str = None,
                    parent_task_id: str = None, task_sequence: int = 0,
                    cline_cwd: str = "") -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO tasks (id, task_type, prompt, priority, max_retries,
                   cline_exe_path, state, created_at, goal_id, parent_task_id, task_sequence,
                   cline_cwd)
                   VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?)""",
                (task_id, task_type, prompt, priority, max_retries,
                 cline_exe_path, now, goal_id, parent_task_id, task_sequence,
                 cline_cwd)
            )
            self._log_transition(conn, task_id, None, "QUEUED", "task_created")
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def get_all_tasks(self, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )

    def get_tasks_by_state(self, state: str) -> List[Dict[str, Any]]:
        return self.fetch_all("SELECT * FROM tasks WHERE state = ?", (state,))

    def get_tasks_by_states(self, states: tuple, limit: int = 200,
                            offset: int = 0) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" * len(states))
        return self.fetch_all(
            f"SELECT * FROM tasks WHERE state IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            list(states) + [limit, offset]
        )

    def get_tasks_by_goal(self, goal_id: str) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM tasks WHERE goal_id = ? ORDER BY task_sequence",
            (goal_id,)
        )

    def count_tasks_by_states(self, states: tuple) -> int:
        placeholders = ",".join("?" * len(states))
        row = self.fetch_one(
            f"SELECT COUNT(*) as cnt FROM tasks WHERE state IN ({placeholders})",
            states
        )
        return row["cnt"] if row else 0

    def update_task_state(self, task_id: str, new_state: str, trigger: str = "",
                           pid: int = None, model_called: bool = False):
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        old_state = task["state"]
        now = datetime.now(timezone.utc).isoformat()
        terminal_states = {"CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED", "CANCELLED",
                           "TRAINING_COMPLETED", "TRAINING_FAILED", "TRAINING_STALLED"}
        completed = now if new_state in terminal_states else None
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, completed_at = ? WHERE id = ?",
                (new_state, completed, task_id)
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

    # ---- Goal CRUD ----

    def create_goal(self, goal_id: str, objective: str, completion_criteria: str = "",
                    stop_conditions: str = "", max_iterations: int = 10,
                    max_failures: int = 3, model_call_budget: int = 100,
                    review_mode: str = "auto",
                    workspace_cwd: str = "") -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO goals (id, objective, state, completion_criteria,
                   stop_conditions, max_iterations, max_failures,
                   model_call_budget, review_mode, created_at, workspace_cwd)
                   VALUES (?, ?, 'GOAL_CREATED', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (goal_id, objective, completion_criteria, stop_conditions,
                 max_iterations, max_failures, model_call_budget, review_mode, now,
                 workspace_cwd)
            )
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> Optional[Dict[str, Any]]:
        return self.fetch_one("SELECT * FROM goals WHERE id = ?", (goal_id,))

    def get_all_goals(self) -> List[Dict[str, Any]]:
        return self.fetch_all("SELECT * FROM goals ORDER BY created_at DESC")

    def get_active_goals(self) -> List[Dict[str, Any]]:
        terminal = ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED")
        placeholders = ",".join("?" * len(terminal))
        return self.fetch_all(
            f"SELECT * FROM goals WHERE state NOT IN ({placeholders}) ORDER BY created_at",
            terminal
        )

    def update_goal_state(self, goal_id: str, new_state: str):
        now = datetime.now(timezone.utc).isoformat()
        terminal = ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED")
        completed = now if new_state in terminal else None
        started = now if new_state == "GOAL_EXECUTING" else None
        with self.transaction() as conn:
            conn.execute(
                "UPDATE goals SET state = ?, completed_at = ?, started_at = COALESCE(?, started_at) WHERE id = ?",
                (new_state, completed, started, goal_id)
            )

    def update_goal_field(self, goal_id: str, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [goal_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE goals SET {sets} WHERE id = ?", values)

    def increment_goal_iteration(self, goal_id: str):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE goals SET iteration_count = iteration_count + 1 WHERE id = ?",
                (goal_id,)
            )

    def increment_goal_failure(self, goal_id: str):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE goals SET failure_count = failure_count + 1 WHERE id = ?",
                (goal_id,)
            )

    # ---- Events ----

    def create_event(self, event_id: str, event_type: str, goal_id: str = None,
                     task_id: str = None, payload: str = None,
                     idempotency_key: str = None,
                     state_version: int = 1) -> Optional[Dict[str, Any]]:
        """Create event. Returns None if idempotency key already exists."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.transaction() as conn:
                if idempotency_key:
                    existing = conn.execute(
                        "SELECT id FROM events WHERE idempotency_key = ?",
                        (idempotency_key,)
                    ).fetchone()
                    if existing:
                        return None
                conn.execute(
                    """INSERT INTO events (event_id, goal_id, task_id, event_type,
                       payload, state_version, idempotency_key, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, goal_id, task_id, event_type, payload,
                     state_version, idempotency_key, now)
                )
            return self.get_event(event_id)
        except sqlite3.IntegrityError:
            return None

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        return self.fetch_one("SELECT * FROM events WHERE event_id = ?", (event_id,))

    def get_unacknowledged_events(self, goal_id: str = None,
                                   task_id: str = None,
                                   event_types: List[str] = None,
                                   after_version: int = 0,
                                   limit: int = 10) -> List[Dict[str, Any]]:
        """Get unacknowledged events, optionally filtered."""
        conditions = ["acknowledged = 0", "state_version > ?"]
        params = [after_version]

        if goal_id:
            conditions.append("goal_id = ?")
            params.append(goal_id)

        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)

        if event_types:
            placeholders = ",".join("?" * len(event_types))
            conditions.append(f"event_type IN ({placeholders})")
            params.extend(event_types)

        where = " AND ".join(conditions)
        return self.fetch_all(
            f"SELECT * FROM events WHERE {where} ORDER BY state_version ASC LIMIT ?",
            params + [limit]
        )

    def acknowledge_event(self, event_id: str, handled_by: str = "") -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE events SET acknowledged = 1, acknowledged_at = ?, handled_by = ? "
                "WHERE event_id = ? AND acknowledged = 0",
                (now, handled_by, event_id)
            )
            return cur.rowcount > 0

    def get_next_event(self, goal_id: str = None,
                       task_id: str = None,
                       event_types: List[str] = None) -> Optional[Dict[str, Any]]:
        results = self.get_unacknowledged_events(
            goal_id=goal_id, task_id=task_id, event_types=event_types, limit=1
        )
        return results[0] if results else None

    def get_latest_version(self, goal_id: str = None) -> int:
        if goal_id:
            row = self.fetch_one(
                "SELECT MAX(state_version) as mv FROM events WHERE goal_id = ?",
                (goal_id,)
            )
        else:
            row = self.fetch_one("SELECT MAX(state_version) as mv FROM events", ())
        return row["mv"] if row and row["mv"] else 0

    # ---- Model Calls ----

    def create_model_call(self, goal_id: str, task_id: str, model_role: str,
                          reason: str = "", estimated_usage: int = 0) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO model_calls (goal_id, task_id, model_role, reason,
                   start_time, status, estimated_usage)
                   VALUES (?, ?, ?, ?, ?, 'started', ?)""",
                (goal_id, task_id, model_role, reason, now, estimated_usage)
            )
            return cur.lastrowid

    def complete_model_call(self, call_id: int, status: str = "completed",
                            result_summary: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE model_calls SET end_time = ?, status = ?, result_summary = ? WHERE id = ?",
                (now, status, result_summary, call_id)
            )

    def get_model_calls(self, goal_id: str = None) -> List[Dict[str, Any]]:
        if goal_id:
            return self.fetch_all(
                "SELECT * FROM model_calls WHERE goal_id = ? ORDER BY start_time",
                (goal_id,)
            )
        return self.fetch_all("SELECT * FROM model_calls ORDER BY start_time DESC LIMIT 100")

    def count_model_calls(self, goal_id: str = None) -> int:
        if goal_id:
            row = self.fetch_one(
                "SELECT COUNT(*) as cnt FROM model_calls WHERE goal_id = ?",
                (goal_id,)
            )
        else:
            row = self.fetch_one("SELECT COUNT(*) as cnt FROM model_calls", ())
        return row["cnt"] if row else 0

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

    # ---- Goal deletion ----

    def delete_goal(self, goal_id: str, logs_dir: str = "") -> Dict[str, Any]:
        """Atomically delete a terminal goal and all linked records.

        Only goals in terminal state (GOAL_COMPLETED, GOAL_FAILED,
        GOAL_CANCELLED) can be deleted.  Active goals are rejected with a
        ValueError.  In one transaction this collects every task linked to
        the goal (including descendants via parent_task_id), deletes their
        state_transitions / task_locks / events / model_calls / tasks,
        deletes the goal's own events and model_calls, then deletes the
        goal itself.

        Returns a structured summary dict:
          {goal_id, deleted_task_count, artifact_paths: [str, ...]}
        where artifact_paths lists log / result files whose real path lies
        inside the configured logs_dir so callers can optionally unlink
        them.  Missing files are silently ignored.
        """
        terminal_states = {"GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"}
        goal = self.get_goal(goal_id)
        if not goal:
            raise ValueError(f"Goal {goal_id} not found")
        if goal["state"] not in terminal_states:
            raise ValueError(
                f"Goal {goal_id} is in state {goal['state']}, not a terminal "
                f"state.  Only terminal goals can be deleted."
            )

        # Resolve the canonical logs directory for safe-path checks.
        _logs_dir = os.path.realpath(os.path.abspath(logs_dir)) if logs_dir else ""

        def _is_safe(p: Optional[str]) -> bool:
            if not p or not _logs_dir:
                return False
            try:
                rp = os.path.realpath(os.path.abspath(p))
                return rp.startswith(_logs_dir + os.sep) or rp == _logs_dir
            except (OSError, ValueError):
                return False

        artifact_paths: List[str] = []
        deleted_task_count = 0

        with self.transaction() as conn:
            # ---- Collect all task ids linked to this goal ----
            # Direct children
            rows = conn.execute(
                "SELECT id FROM tasks WHERE goal_id = ?", (goal_id,)
            ).fetchall()
            task_ids = {r["id"] for r in rows}
            # Expand descendants via parent_task_id (iterative BFS to
            # avoid recursive SQL / CTE that may not be available).
            queue = list(task_ids)
            while queue:
                parent = queue.pop()
                sub = conn.execute(
                    "SELECT id FROM tasks WHERE parent_task_id = ?", (parent,)
                ).fetchall()
                for s in sub:
                    if s["id"] not in task_ids:
                        task_ids.add(s["id"])
                        queue.append(s["id"])

            task_id_list = list(task_ids)

            # Collect artifact paths before deletion.
            if task_id_list:
                placeholders = ",".join("?" * len(task_id_list))
                task_rows = conn.execute(
                    f"SELECT id, state, training_state, log_stdout, log_stderr, "
                    f"training_log, result_file "
                    f"FROM tasks WHERE id IN ({placeholders})",
                    task_id_list,
                ).fetchall()
                live_states = {
                    "CLINE_STARTING", "CLINE_RUNNING",
                    "TRAINING_STARTING", "TRAINING_RUNNING",
                }
                live_tasks = [
                    row["id"] for row in task_rows
                    if row["state"] in live_states
                    or row["training_state"] in live_states
                ]
                if live_tasks:
                    raise ValueError(
                        f"Goal {goal_id} still has running tasks: "
                        f"{', '.join(sorted(live_tasks))}"
                    )

                # Only daemon-owned logs/result packages are eligible.  In
                # particular, output_path and metrics_path may point into the
                # user's project and must never be removed here.
                for row in task_rows:
                    for field in ("log_stdout", "log_stderr", "training_log", "result_file"):
                        val = row[field]
                        if val and _is_safe(val) and os.path.isfile(val):
                            artifact_paths.append(val)

                    prompt_path = os.path.join(
                        _logs_dir, "cline", f"{row['id']}.prompt.txt"
                    ) if _logs_dir else ""
                    if prompt_path and _is_safe(prompt_path) and os.path.isfile(prompt_path):
                        artifact_paths.append(prompt_path)

            # ---- Delete task-scoped records ----
            if task_id_list:
                conn.execute(
                    f"DELETE FROM state_transitions WHERE task_id IN ({placeholders})",
                    task_id_list,
                )
                conn.execute(
                    f"DELETE FROM task_locks WHERE holder_task_id IN ({placeholders})",
                    task_id_list,
                )
                conn.execute(
                    f"DELETE FROM events WHERE task_id IN ({placeholders})",
                    task_id_list,
                )
                conn.execute(
                    f"DELETE FROM model_calls WHERE task_id IN ({placeholders})",
                    task_id_list,
                )
                # Delete the tasks themselves.
                conn.execute(
                    f"DELETE FROM tasks WHERE id IN ({placeholders})",
                    task_id_list,
                )
                deleted_task_count = len(task_id_list)

            # ---- Delete goal-scoped records ----
            conn.execute(
                "DELETE FROM events WHERE goal_id = ?", (goal_id,)
            )
            conn.execute(
                "DELETE FROM model_calls WHERE goal_id = ?", (goal_id,)
            )

            # ---- Delete the goal ----
            conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))

            logging.getLogger(__name__).info(
                "delete_goal %s: %d tasks, %d artifact paths",
                goal_id, deleted_task_count, len(artifact_paths),
            )

        return {
            "goal_id": goal_id,
            "deleted_task_count": deleted_task_count,
            "artifact_paths": artifact_paths,
        }

    # ---- Janitor helpers ----

    def clean_task(self, task_id: str):
        with self.transaction() as conn:
            conn.execute("DELETE FROM state_transitions WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def clean_old_terminal_tasks(self, retention_seconds: float) -> int:
        terminal = ["CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED", "CANCELLED"]
        placeholders = ",".join("?" * len(terminal))
        with self.transaction() as conn:
            cur = conn.execute(
                f"DELETE FROM state_transitions WHERE task_id IN "
                f"(SELECT id FROM tasks WHERE state IN ({placeholders}))",
                terminal
            )
            cur = conn.execute(
                f"DELETE FROM tasks WHERE state IN ({placeholders})",
                terminal
            )
            return cur.rowcount
