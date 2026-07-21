"""Budget tracker for model call counting."""

import logging
from datetime import datetime, timezone, date
from typing import Optional

from .db import Database

logger = logging.getLogger(__name__)


class BudgetTracker:
    """Tracks Cline/Codex call counts with configurable limits."""

    def __init__(self, db: Database):
        self.db = db
        self._cache = {}  # key -> current count

    def record_call(self, caller: str, task_id: str = "", operation: str = "") -> int:
        """Record a model call. Returns new daily total."""
        today = date.today().isoformat()
        key = f"budget:{caller}:{today}"

        current = int(self.db.get_kv(key) or "0")
        current += 1
        self.db.set_kv(key, str(current))
        self._cache[key] = current

        logger.debug(f"Budget: {caller} call #{current} today (task={task_id})")
        return current

    def get_daily_count(self, caller: str) -> int:
        today = date.today().isoformat()
        key = f"budget:{caller}:{today}"
        if key in self._cache:
            return self._cache[key]
        return int(self.db.get_kv(key) or "0")

    def check_limit(self, caller: str, daily_limit: int) -> bool:
        """Check if caller is under daily limit. Returns True if allowed."""
        if daily_limit <= 0:
            return True  # No limit set
        return self.get_daily_count(caller) < daily_limit

    def get_status(self) -> dict:
        """Return full budget status for all callers."""
        today = date.today().isoformat()
        status = {}
        for caller in ["cline", "codex"]:
            key = f"budget:{caller}:{today}"
            status[caller] = {
                "daily": int(self.db.get_kv(key) or "0"),
                "date": today,
            }
        return status
