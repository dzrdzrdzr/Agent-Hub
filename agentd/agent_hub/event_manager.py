"""Persistent event queue for Agent Hub. Supports wait_for_event, idempotency, versioning.

Events survive client disconnects and daemon restarts.
"""

import uuid
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from .db import Database

logger = logging.getLogger(__name__)

KEY_EVENT_TYPES = {
    "CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED",
    "TRAINING_COMPLETED", "TRAINING_FAILED", "TRAINING_STALLED",
    "RESULT_READY", "CODEX_REVIEW_REQUIRED",
    "GOAL_COMPLETED", "GOAL_FAILED"
}

NON_KEY_EVENT_TYPES = {
    "CLINE_RUNNING", "TRAINING_RUNNING", "GOAL_EXECUTING",
    "HEARTBEAT", "PROGRESS"
}


class EventManager:
    """Persistent event queue with idempotency and versioning."""

    def __init__(self, db: Database):
        self.db = db
        self._waiters = {}  # waiter_id -> asyncio.Event + metadata
        self._state_version = self.db.get_latest_version()

    # ---- Event creation ----

    def emit_event(self, event_type: str, goal_id: str = None,
                   task_id: str = None, payload: dict = None,
                   idempotency_key: str = None) -> Optional[Dict[str, Any]]:
        """Emit an event into the persistent queue."""
        self._state_version += 1
        event_id = f"evt-{uuid.uuid4().hex[:12]}"

        payload_str = json.dumps(payload, ensure_ascii=False) if payload else None

        event = self.db.create_event(
            event_id=event_id,
            event_type=event_type,
            goal_id=goal_id,
            task_id=task_id,
            payload=payload_str,
            idempotency_key=idempotency_key,
            state_version=self._state_version,
        )

        if event is None and idempotency_key:
            logger.debug(f"Event suppressed by idempotency: {idempotency_key}")
            return None

        logger.info(f"Event emitted: {event_id} type={event_type} "
                     f"goal={goal_id} task={task_id}")

        # Wake waiters
        self._notify_waiters(event_type, goal_id, task_id)

        return event

    def emit_task_related_event(self, event_type: str, task: Dict[str, Any],
                                 payload: dict = None) -> Optional[Dict[str, Any]]:
        """Emit event related to a task state change."""
        goal_id = task.get("goal_id")
        task_id = task["id"]
        idemp_key = f"{event_type}:{task_id}:{task.get('state', '')}"

        return self.emit_event(
            event_type=event_type,
            goal_id=goal_id,
            task_id=task_id,
            payload=payload,
            idempotency_key=idemp_key,
        )

    # ---- Event query ----

    def get_unacknowledged(self, goal_id: str = None,
                           event_types: List[str] = None,
                           after_version: int = 0,
                           limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.get_unacknowledged_events(
            goal_id=goal_id,
            event_types=event_types,
            after_version=after_version,
            limit=limit,
        )

    def acknowledge(self, event_id: str, handled_by: str = "") -> bool:
        result = self.db.acknowledge_event(event_id, handled_by)
        if result:
            logger.debug(f"Event acknowledged: {event_id} by {handled_by}")
        return result

    def get_latest_version(self, goal_id: str = None) -> int:
        return self.db.get_latest_version(goal_id)

    # ---- Blocking wait_for_event ----

    async def wait_for_event(self, goal_id: str = None,
                             task_id: str = None,
                             event_types: List[str] = None,
                             after_version: int = 0,
                             timeout: float = None) -> Optional[Dict[str, Any]]:
        """Block until a key event arrives or timeout.

        Returns the event dict, or None on timeout.
        Only returns KEY events (not RUNNING/HEARTBEAT).
        Optional task_id filter for per-task event isolation.
        """
        # First check for any existing unacknowledged events
        types_to_wait = event_types or list(KEY_EVENT_TYPES)
        existing = self.get_unacknowledged(
            goal_id=goal_id,
            event_types=types_to_wait,
            after_version=after_version,
            limit=1,
        )
        if existing:
            e = existing[0]
            if task_id is None or e.get("task_id") == task_id:
                return e

        # No existing events — create a waiter
        waiter_id = f"waiter-{uuid.uuid4().hex[:8]}"
        wake_event = asyncio.Event()

        self._waiters[waiter_id] = {
            "event": wake_event,
            "goal_id": goal_id,
            "task_id": task_id,
            "event_types": set(types_to_wait),
            "result": None,
        }

        try:
            if timeout:
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    return None
            else:
                await wake_event.wait()

            waiter = self._waiters.pop(waiter_id, None)
            return waiter["result"] if waiter else None
        except asyncio.CancelledError:
            self._waiters.pop(waiter_id, None)
            raise

    def _notify_waiters(self, event_type: str, goal_id: str = None,
                         task_id: str = None):
        """Notify waiters matching this event."""
        for waiter_id, waiter in list(self._waiters.items()):
            if waiter["goal_id"] and waiter["goal_id"] != goal_id:
                continue
            if waiter.get("task_id") and task_id and waiter["task_id"] != task_id:
                continue
            if event_type not in waiter["event_types"]:
                continue

            # Get the latest matching event
            event = self.db.get_next_event(
                goal_id=waiter["goal_id"],
                event_types=list(waiter["event_types"]),
            )
            if event:
                waiter["result"] = event
                waiter["event"].set()

    # ---- Recovery ----

    def recover_unacknowledged(self) -> List[Dict[str, Any]]:
        """Get all unacknowledged events for recovery."""
        return self.db.fetch_all(
            "SELECT * FROM events WHERE acknowledged = 0 ORDER BY state_version"
        )

    def cleanup_acknowledged(self, older_than_hours: int = 24):
        """Delete acknowledged events older than N hours."""
        cutoff = datetime.now(timezone.utc)
        self.db.execute(
            "DELETE FROM events WHERE acknowledged = 1 AND acknowledged_at < ?",
            (cutoff.replace(hour=cutoff.hour - older_than_hours).isoformat(),)
        )
        self.db.commit()
