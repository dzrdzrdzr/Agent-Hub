"""Task manager with state machine engine."""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from .db import Database

logger = logging.getLogger(__name__)

# Phase 1 valid states
VALID_STATES = {
    "QUEUED", "CLINE_STARTING", "CLINE_RUNNING",
    "CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED",
    "WAITING_APPROVAL", "CANCELLED"
}

# Valid state transitions
TRANSITIONS = {
    "QUEUED": {"CLINE_STARTING", "CANCELLED"},
    "CLINE_STARTING": {"CLINE_RUNNING", "CLINE_FAILED", "CANCELLED"},
    "CLINE_RUNNING": {"CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED", "WAITING_APPROVAL", "CANCELLED"},
    "CLINE_FAILED": {"CLINE_STARTING", "CANCELLED"},
    "CLINE_STALLED": {"CLINE_STARTING", "CANCELLED"},
    "CLINE_SUCCEEDED": set(),  # terminal
    "WAITING_APPROVAL": {"CLINE_STARTING", "CANCELLED"},
    "CANCELLED": set(),  # terminal
}

# States that are terminal
TERMINAL_STATES = {"CLINE_SUCCEEDED", "CANCELLED", "CLINE_FAILED", "CLINE_STALLED"}

# States considered "active" for monitoring
ACTIVE_STATES = {"QUEUED", "CLINE_STARTING", "CLINE_RUNNING", "WAITING_APPROVAL"}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def is_valid_transition(old_state: str, new_state: str) -> bool:
    if old_state not in TRANSITIONS:
        return False
    return new_state in TRANSITIONS[old_state]


class TaskManager:
    """Manages task lifecycle through the state machine."""

    def __init__(self, db: Database):
        self.db = db
        self._callbacks = {}  # event_type -> list of async callbacks

    def on(self, event: str, callback):
        """Register callback for state change events."""
        self._callbacks.setdefault(event, []).append(callback)

    async def _emit(self, event: str, task: Dict[str, Any]):
        for cb in self._callbacks.get(event, []):
            try:
                await cb(task)
            except Exception as e:
                logger.error(f"Callback error for {event}: {e}")

    def create_task(self, task_type: str = "cline_exec", prompt: str = "",
                    priority: int = 0, max_retries: int = 1,
                    cline_exe_path: str = "") -> Dict[str, Any]:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        task = self.db.create_task(
            task_id=task_id, task_type=task_type, prompt=prompt,
            priority=priority, max_retries=max_retries,
            cline_exe_path=cline_exe_path
        )
        logger.info(f"Task created: {task_id}")
        return task

    def transition(self, task_id: str, new_state: str, trigger: str = "",
                   pid: int = None, model_called: bool = False) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        old_state = task["state"]
        if not is_valid_transition(old_state, new_state):
            raise ValueError(
                f"Invalid transition: {old_state} -> {new_state} for task {task_id}"
            )

        # Retry logic: CLINE_FAILED/STALLED -> CLINE_STARTING only if under max
        if old_state in ("CLINE_FAILED", "CLINE_STALLED") and new_state == "CLINE_STARTING":
            if task["retry_count"] >= task["max_retries"]:
                raise ValueError(
                    f"Cannot retry {task_id}: retry_count={task['retry_count']} >= max={task['max_retries']}"
                )
            self.db.increment_retry(task_id)

        self.db.update_task_state(task_id, new_state, trigger=trigger,
                                   pid=pid, model_called=model_called)
        task = self.db.get_task(task_id)
        logger.info(f"Task {task_id}: {old_state} -> {new_state} [{trigger}]")
        return task

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_task(task_id)

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        return self.db.get_all_tasks()

    def get_all_tasks_sql(self, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        """Get tasks with SQL-level pagination."""
        return self.db.get_all_tasks(limit=limit, offset=offset)

    def get_active_tasks(self) -> List[Dict[str, Any]]:
        """Get active tasks using SQL-side filter (efficient)."""
        states = tuple(ACTIVE_STATES)
        return self.db.get_tasks_by_states(states)

    def get_active_tasks_sql(self, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        """Get active tasks with SQL-level filter and pagination."""
        states = tuple(ACTIVE_STATES)
        return self.db.get_tasks_by_states(states, limit=limit, offset=offset)

    def get_active_count(self) -> int:
        """Count active tasks efficiently."""
        states = tuple(ACTIVE_STATES)
        return self.db.count_tasks_by_states(states)

    def get_recoverable_tasks(self) -> List[Dict[str, Any]]:
        """Tasks that need recovery after daemon restart."""
        return (self.db.get_tasks_by_state("CLINE_STARTING") +
                self.db.get_tasks_by_state("CLINE_RUNNING"))

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        return self.transition(task_id, "CANCELLED", trigger="user_cancelled")

    def approve_task(self, task_id: str) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if task["state"] != "WAITING_APPROVAL":
            raise ValueError(f"Task {task_id} not in WAITING_APPROVAL state")
        return self.transition(task_id, "CLINE_STARTING", trigger="user_approved")
