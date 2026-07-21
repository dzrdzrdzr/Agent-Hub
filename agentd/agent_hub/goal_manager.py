"""Goal manager with state machine for autonomous closed-loop research."""

import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from .db import Database

logger = logging.getLogger(__name__)

GOAL_STATES = {
    "GOAL_CREATED", "GOAL_PLANNING", "GOAL_EXECUTING",
    "GOAL_WAITING_EVENT", "GOAL_REVIEWING", "GOAL_ITERATING",
    "GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"
}

TRANSITIONS = {
    "GOAL_CREATED": {"GOAL_PLANNING", "GOAL_CANCELLED"},
    "GOAL_PLANNING": {"GOAL_EXECUTING", "GOAL_FAILED", "GOAL_CANCELLED", "GOAL_WAITING_EVENT", "GOAL_COMPLETED"},
    "GOAL_EXECUTING": {"GOAL_WAITING_EVENT", "GOAL_REVIEWING", "GOAL_COMPLETED",
                        "GOAL_FAILED", "GOAL_CANCELLED", "GOAL_PLANNING"},
    "GOAL_WAITING_EVENT": {"GOAL_EXECUTING", "GOAL_REVIEWING", "GOAL_ITERATING",
                            "GOAL_FAILED", "GOAL_CANCELLED", "GOAL_COMPLETED"},
    "GOAL_REVIEWING": {"GOAL_ITERATING", "GOAL_EXECUTING", "GOAL_COMPLETED",
                        "GOAL_FAILED", "GOAL_CANCELLED"},
    "GOAL_ITERATING": {"GOAL_PLANNING", "GOAL_EXECUTING", "GOAL_WAITING_EVENT",
                        "GOAL_REVIEWING", "GOAL_COMPLETED",
                        "GOAL_FAILED", "GOAL_CANCELLED"},
    "GOAL_COMPLETED": set(),
    "GOAL_FAILED": set(),
    "GOAL_CANCELLED": set(),
}

TERMINAL_STATES = {"GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"}
ACTIVE_STATES = GOAL_STATES - TERMINAL_STATES


def valid_transition(old: str, new: str) -> bool:
    return old in TRANSITIONS and new in TRANSITIONS[old]


class GoalManager:
    """Manages goal lifecycle through the goal state machine."""

    def __init__(self, db: Database):
        self.db = db

    def create_goal(self, objective: str, completion_criteria: str = "",
                    stop_conditions: str = "", max_iterations: int = 10,
                    max_failures: int = 3, model_call_budget: int = 100,
                    review_mode: str = "auto") -> Dict[str, Any]:
        goal_id = f"goal-{uuid.uuid4().hex[:12]}"
        goal = self.db.create_goal(
            goal_id=goal_id, objective=objective,
            completion_criteria=completion_criteria,
            stop_conditions=stop_conditions,
            max_iterations=max_iterations,
            max_failures=max_failures,
            model_call_budget=model_call_budget,
            review_mode=review_mode,
        )
        logger.info(f"Goal created: {goal_id}, objective: {objective[:80]}")
        return goal

    def transition(self, goal_id: str, new_state: str,
                   trigger: str = "") -> Dict[str, Any]:
        goal = self.db.get_goal(goal_id)
        if not goal:
            raise ValueError(f"Goal {goal_id} not found")

        old_state = goal["state"]
        if not valid_transition(old_state, new_state):
            raise ValueError(
                f"Invalid goal transition: {old_state} -> {new_state}"
            )

        # Check stop conditions
        if new_state not in TERMINAL_STATES:
            if goal["failure_count"] >= goal["max_failures"]:
                logger.warning(
                    f"Goal {goal_id}: failures={goal['failure_count']} >= "
                    f"max={goal['max_failures']}, forcing GOAL_FAILED"
                )
                new_state = "GOAL_FAILED"
                trigger = f"{trigger}_failure_limit"
            elif goal["iteration_count"] > goal["max_iterations"]:
                logger.warning(
                    f"Goal {goal_id}: iterations={goal['iteration_count']} >= "
                    f"max={goal['max_iterations']}, forcing GOAL_FAILED"
                )
                new_state = "GOAL_FAILED"
                trigger = f"{trigger}_iteration_limit"
            elif goal["accumulated_model_calls"] >= goal["model_call_budget"]:
                logger.warning(
                    f"Goal {goal_id}: model_calls={goal['accumulated_model_calls']} "
                    f">= budget={goal['model_call_budget']}, forcing GOAL_FAILED"
                )
                new_state = "GOAL_FAILED"
                trigger = f"{trigger}_budget_exceeded"

        self.db.update_goal_state(goal_id, new_state)
        goal = self.db.get_goal(goal_id)
        logger.info(f"Goal {goal_id}: {old_state} -> {new_state} [{trigger}]")
        return goal

    def get_goal(self, goal_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_goal(goal_id)

    def get_active_goals(self) -> List[Dict[str, Any]]:
        return self.db.get_active_goals()

    def set_current_task(self, goal_id: str, task_id: str):
        self.db.update_goal_field(goal_id, current_task_id=task_id)

    def set_skip_next_plan(self, goal_id: str):
        """Flag that the next loop iteration should skip planning."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE goals SET skip_next_plan = 1 WHERE id = ?", (goal_id,)
            )

    def set_orchestrator_step(self, goal_id: str, step: str):
        """Persist current orchestrator step for recovery after restart."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE goals SET orchestrator_step = ? WHERE id = ?", (step, goal_id)
            )

    def clear_skip_next_plan(self, goal_id: str):
        """Clear the skip-next-plan flag."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE goals SET skip_next_plan = 0 WHERE id = ?", (goal_id,)
            )

    def set_latest_decision(self, goal_id: str, decision: dict):
        self.db.update_goal_field(
            goal_id, latest_codex_decision=json.dumps(decision, ensure_ascii=False)
        )

    def increment_iteration(self, goal_id: str):
        self.db.increment_goal_iteration(goal_id)

    def increment_failure(self, goal_id: str):
        self.db.increment_goal_failure(goal_id)

    def increment_model_calls(self, goal_id: str, count: int = 1):
        goal = self.db.get_goal(goal_id)
        if goal:
            new_total = goal["accumulated_model_calls"] + count
            self.db.update_goal_field(
                goal_id, accumulated_model_calls=new_total
            )

    def cancel_goal(self, goal_id: str) -> Dict[str, Any]:
        return self.transition(goal_id, "GOAL_CANCELLED", trigger="user_cancelled")

    def complete_goal(self, goal_id: str) -> Dict[str, Any]:
        return self.transition(goal_id, "GOAL_COMPLETED", trigger="criteria_met")

    def fail_goal(self, goal_id: str, reason: str = "") -> Dict[str, Any]:
        self.db.update_goal_field(goal_id, error_summary=reason)
        return self.transition(goal_id, "GOAL_FAILED", trigger=reason or "goal_failed")

    def should_continue(self, goal_id: str) -> bool:
        """Check if goal can continue (within limits, not terminal)."""
        goal = self.db.get_goal(goal_id)
        if not goal:
            return False
        if goal["state"] in TERMINAL_STATES:
            return False
        if goal["failure_count"] >= goal["max_failures"]:
            return False
        if goal["iteration_count"] >= goal["max_iterations"]:
            return False
        if goal["accumulated_model_calls"] >= goal["model_call_budget"]:
            return False
        return True
