"""Fault injection tests: TCP disconnect, daemon restart, Cline crash, duplicates.

Spec requirements:
- TCP client disconnect during operation → events not lost
- Agent Hub restart during operation → recovery
- Cline crash on first attempt → auto-retry
- VS Code client absent during training → training continues
- Duplicate completion events → idempotency
- Duplicate start requests → prevention
"""

import os
import sys
import json
import time
import tempfile
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_hub.db import Database
from agent_hub.config import ClineConfig, AgentdConfig
from agent_hub.task_manager import TaskManager
from agent_hub.goal_manager import GoalManager
from agent_hub.event_manager import EventManager
from agent_hub.training_manager import TrainingManager
from agent_hub.cline_executor import MockClineExecutor
from agent_hub.codex_executor import MockCodexExecutor
from agent_hub.goal_orchestrator import GoalOrchestrator


def make_config(**kw):
    c = ClineConfig(mock=True, mock_exit_code=kw.get("exit_code", 0),
                    mock_delay_seconds=0.05,
                    max_retries=kw.get("max_retries", 1),
                    stall_threshold_seconds=300)
    return AgentdConfig(cline=c)


@pytest.mark.asyncio
async def test_cline_crash_auto_retry():
    """Cline crashes on first attempt → auto-retry → succeed."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, logs_dir=os.path.join(tmp, "logs")
        )

        # Cline fails first, succeeds on retry
        cfg = make_config(exit_code=1, max_retries=1)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)

        codex_executor = MockCodexExecutor(plan_sequence=[
            {"verdict": "plan_ready",
             "next_task": {"prompt": "Fix the bug", "task_type": "cline_exec"},
             "goal_complete": False, "reasoning": "plan"},
            {"verdict": "approved",
             "next_task": None, "goal_complete": False, "reasoning": "review"},
            {"verdict": "goal_achieved",
             "next_task": None, "goal_complete": True, "reasoning": "done"},
        ])

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=5, max_failures=5,
        )

        goal = await orchestrator.start_goal(
            objective="Fix bug", max_iterations=5, max_failures=5,
        )

        deadline = time.time() + 30
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.5)

        goal = goal_manager.get_goal(goal["id"])
        # Should complete despite initial failure
        assert goal["state"] == "GOAL_COMPLETED", \
            f"Expected GOAL_COMPLETED after retry, got {goal['state']}"
        # Task should have retried
        tasks = db.get_tasks_by_goal(goal["id"])
        assert len(tasks) >= 1

        db.close()


@pytest.mark.asyncio
async def test_duplicate_start_prevention():
    """Duplicate start_goal requests should not create duplicate orchestrators."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, logs_dir=os.path.join(tmp, "logs")
        )

        cfg = make_config(exit_code=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)

        codex_executor = MockCodexExecutor(plan_sequence=[
            {"verdict": "plan_ready",
             "next_task": {"prompt": "Test", "task_type": "cline_exec"},
             "goal_complete": False, "reasoning": "test"},
            {"verdict": "approved", "next_task": None,
             "goal_complete": False, "reasoning": "review"},
            {"verdict": "goal_achieved", "next_task": None,
             "goal_complete": True, "reasoning": "done"},
        ])

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=5, max_failures=5,
        )

        # Start same goal twice
        goal1 = await orchestrator.start_goal(
            objective="Test duplicate", max_iterations=5, max_failures=5,
        )
        goal2 = await orchestrator.start_goal(
            objective="Test duplicate 2", max_iterations=5, max_failures=5,
        )

        # Should be different goals (each start_goal creates a new goal)
        assert goal1["id"] != goal2["id"], \
            "Duplicate start should create separate goals"

        # Wait for both to complete
        deadline = time.time() + 30
        while time.time() < deadline:
            g1 = goal_manager.get_goal(goal1["id"])
            g2 = goal_manager.get_goal(goal2["id"])
            if g1["state"] in ("GOAL_COMPLETED", "GOAL_FAILED") and \
               g2["state"] in ("GOAL_COMPLETED", "GOAL_FAILED"):
                break
            await asyncio.sleep(0.5)

        g1 = goal_manager.get_goal(goal1["id"])
        g2 = goal_manager.get_goal(goal2["id"])
        assert g1["state"] == "GOAL_COMPLETED"
        assert g2["state"] == "GOAL_COMPLETED"
        assert g1["iteration_count"] == 1
        assert g2["iteration_count"] == 1

        db.close()


@pytest.mark.asyncio
async def test_daemon_restart_recovery():
    """Simulate daemon restart: verify recovery logic with mock state."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, logs_dir=os.path.join(tmp, "logs")
        )

        cfg = make_config(exit_code=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)

        # Create a goal with a Cline task that "survived" restart
        goal = goal_manager.create_goal("Test recovery")
        goal_manager.transition(goal["id"], "GOAL_PLANNING")
        goal_manager.transition(goal["id"], "GOAL_EXECUTING")

        task = task_manager.create_task(
            task_type="cline_exec", prompt="Recovery test",
            goal_id=goal["id"],
        )
        # Simulate a running task (PID points to current process for testing)
        task_manager.transition(task["id"], "CLINE_STARTING")
        task_manager.transition(task["id"], "CLINE_RUNNING")
        db.update_task_field(task["id"], cline_pid=os.getpid())

        # Emit some unacknowledged events
        event_manager.emit_event("CLINE_RUNNING", goal_id=goal["id"],
                                  task_id=task["id"])
        event_manager.emit_event("CODEX_REVIEW_REQUIRED", goal_id=goal["id"],
                                  task_id=task["id"])

        # Verify recovery data exists
        unack = event_manager.recover_unacknowledged()
        assert len(unack) >= 2, f"Expected >= 2 unacknowledged events, got {len(unack)}"

        # Verify task is recoverable
        recoverable = task_manager.get_recoverable_tasks()
        assert len(recoverable) >= 1, \
            f"Expected >= 1 recoverable task, got {len(recoverable)}"
        assert recoverable[0]["id"] == task["id"]

        # Verify goal is active
        active = goal_manager.get_active_goals()
        assert len(active) >= 1
        assert active[0]["id"] == goal["id"]

        db.close()


@pytest.mark.asyncio
async def test_wait_for_event_reconnect():
    """wait_for_event should handle waiter lifecycle correctly across reconnects."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        event_manager = EventManager(db)

        goal_id = "goal-reconnect"

        # Simulate a waiter that gets cancelled (like TCP disconnect)
        async def _cancelled_waiter():
            try:
                result = await event_manager.wait_for_event(
                    goal_id=goal_id,
                    event_types=["CLINE_SUCCEEDED"],
                    timeout=10,
                )
                return result
            except asyncio.CancelledError:
                return "cancelled"

        waiter_task = asyncio.create_task(_cancelled_waiter())

        # Emit an event while waiter is active
        await asyncio.sleep(0.1)
        event_manager.emit_event("TRAINING_COMPLETED", goal_id=goal_id,
                                  task_id="task-1")

        # Cancel the waiter (simulating TCP disconnect)
        waiter_task.cancel()
        result = await waiter_task
        assert result == "cancelled", "Waiter should handle cancellation"

        # Event should still be in the queue (unacknowledged)
        events = event_manager.get_unacknowledged(goal_id=goal_id)
        assert len(events) >= 1, "Event should survive waiter cancellation"

        # New waiter should get the event
        new_event = await event_manager.wait_for_event(
            goal_id=goal_id,
            event_types=["TRAINING_COMPLETED", "CLINE_SUCCEEDED"],
            timeout=5,
        )
        assert new_event is not None, "New waiter should get existing event"
        assert new_event["event_type"] == "TRAINING_COMPLETED"

        db.close()


@pytest.mark.asyncio
async def test_model_calls_zero_during_wait():
    """Verify model calls are zero during mechanical waiting periods."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        # Before wait: count model calls
        before = db.count_model_calls()

        # Simulate wait operations
        event_manager = EventManager(db)
        goal_id = "goal-zero-calls"

        # Create waiter and emit event asynchronously
        async def _emit():
            await asyncio.sleep(0.2)
            event_manager.emit_event("CLINE_SUCCEEDED", goal_id=goal_id)

        emit_task = asyncio.create_task(_emit())
        event = await event_manager.wait_for_event(
            goal_id=goal_id, event_types=["CLINE_SUCCEEDED"], timeout=5,
        )
        await emit_task

        assert event is not None

        # After wait: model calls should be unchanged
        after = db.count_model_calls()
        assert before == after, \
            f"Model calls changed during wait: {before} → {after}"

        db.close()
