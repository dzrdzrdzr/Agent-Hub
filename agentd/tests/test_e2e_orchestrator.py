"""End-to-end test: autonomous closed-loop orchestrator with mock components."""

import os
import sys
import json
import time
import tempfile
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_hub.db import Database
from agent_hub.config import ClineConfig, AgentdConfig, SafetyConfig
from agent_hub.task_manager import TaskManager
from agent_hub.goal_manager import GoalManager
from agent_hub.event_manager import EventManager
from agent_hub.training_manager import TrainingManager
from agent_hub.cline_executor import MockClineExecutor
from agent_hub.codex_executor import MockCodexExecutor
from agent_hub.safety_guard import SafetyGuard
from agent_hub.goal_orchestrator import GoalOrchestrator


def make_config(**kw):
    c = ClineConfig(mock=True, mock_exit_code=kw.get("exit_code", 0),
                    mock_delay_seconds=0.05,
                    max_retries=kw.get("max_retries", 1),
                    stall_threshold_seconds=300)
    return AgentdConfig(cline=c)


@pytest.mark.asyncio
async def test_e2e_two_iterations():
    """End-to-end: 2 automatic iterations, goal completes without user input."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager,
            stall_threshold=300, logs_dir=os.path.join(tmp, "logs")
        )

        cfg = make_config(exit_code=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)

        codex_executor = MockCodexExecutor(plan_sequence=[
            # Iteration 1: plan
            {
                "verdict": "plan_ready",
                "next_task": {
                    "prompt": "Write test_hello.py and run pytest",
                    "task_type": "cline_exec",
                    "training_expected": False,
                },
                "goal_complete": False,
                "reasoning": "iteration 1 plan",
            },
            # Iteration 1: review
            {
                "verdict": "approved",
                "next_task": None,
                "goal_complete": False,
                "reasoning": "iteration 1 results good",
            },
            # Iteration 2: plan
            {
                "verdict": "goal_achieved",
                "next_task": None,
                "goal_complete": True,
                "reasoning": "All tests pass, goal complete",
            },
        ])

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=5, max_failures=2,
        )

        goal = await orchestrator.start_goal(
            objective="Create a test_hello.py file that prints hello and passes pytest",
            completion_criteria="pytest passes, test_hello.py exists",
            max_iterations=5,
            max_failures=2,
        )

        deadline = time.time() + 30
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.5)

        goal = goal_manager.get_goal(goal["id"])
        assert goal is not None, "Goal should exist"
        assert goal["state"] == "GOAL_COMPLETED", \
            f"Expected GOAL_COMPLETED, got {goal['state']}"
        assert goal["iteration_count"] >= 2, \
            f"Expected >= 2 iterations, got {goal['iteration_count']}"

        tasks = db.get_tasks_by_goal(goal["id"])
        assert len(tasks) >= 1, f"Expected >= 1 tasks, got {len(tasks)}"

        calls = db.get_model_calls(goal_id=goal["id"])
        assert len(calls) >= 2, f"Expected >= 2 model calls, got {len(calls)}"

        assert goal["failure_count"] == 0, f"Expected 0 failures, got {goal['failure_count']}"

        db.close()


@pytest.mark.asyncio
async def test_wait_for_event():
    """Test that wait_for_event blocks until event arrives."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        event_manager = EventManager(db)

        goal_id = "goal-test-wait"

        async def _emit_later():
            await asyncio.sleep(1)
            event_manager.emit_event(
                "CLINE_SUCCEEDED", goal_id=goal_id, task_id="task-test",
                payload={"exit_code": 0},
            )

        emit_task = asyncio.create_task(_emit_later())
        event = await event_manager.wait_for_event(
            goal_id=goal_id,
            event_types=["CLINE_SUCCEEDED", "CLINE_FAILED"],
            timeout=5,
        )
        await emit_task

        assert event is not None, "Should receive event"
        assert event["event_type"] == "CLINE_SUCCEEDED"
        assert not event["acknowledged"]

        ok = event_manager.acknowledge(event["event_id"], "test")
        assert ok

        event2 = await event_manager.wait_for_event(
            goal_id=goal_id, event_types=["CLINE_SUCCEEDED"], timeout=1,
        )
        assert event2 is None, "Should timeout since event was acknowledged"

        db.close()


@pytest.mark.asyncio
async def test_goal_state_machine():
    """Test goal state transitions."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        gm = GoalManager(db)

        goal = gm.create_goal("Test goal")
        assert goal["state"] == "GOAL_CREATED"

        goal = gm.transition(goal["id"], "GOAL_PLANNING")
        assert goal["state"] == "GOAL_PLANNING"

        goal = gm.transition(goal["id"], "GOAL_EXECUTING")
        assert goal["state"] == "GOAL_EXECUTING"

        goal = gm.transition(goal["id"], "GOAL_WAITING_EVENT")
        assert goal["state"] == "GOAL_WAITING_EVENT"

        try:
            gm.transition(goal["id"], "GOAL_CREATED")
            assert False, "Should have raised"
        except ValueError:
            pass

        goal = gm.transition(goal["id"], "GOAL_COMPLETED")
        assert goal["state"] == "GOAL_COMPLETED"

        db.close()


@pytest.mark.asyncio
async def test_event_idempotency():
    """Test that duplicate events are suppressed."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        em = EventManager(db)

        e1 = em.emit_event("CLINE_RUNNING", task_id="task-1", idempotency_key="key-1")
        assert e1 is not None

        e2 = em.emit_event("CLINE_RUNNING", task_id="task-1", idempotency_key="key-1")
        assert e2 is None, "Duplicate should be None"

        db.close()


@pytest.mark.asyncio
async def test_training_state_flow():
    """Test training state transitions through task states."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        tm = TaskManager(db)

        task = tm.create_task(task_type="training",
                              prompt="Train model for 10 epochs", max_retries=0)
        assert task["state"] == "QUEUED"

        task = tm.transition(task["id"], "TRAINING_STARTING")
        assert task["state"] == "TRAINING_STARTING"

        task = tm.transition(task["id"], "TRAINING_RUNNING")
        assert task["state"] == "TRAINING_RUNNING"

        task = tm.transition(task["id"], "TRAINING_COMPLETED")
        assert task["state"] == "TRAINING_COMPLETED"

        task = tm.transition(task["id"], "RESULT_ANALYZING")
        assert task["state"] == "RESULT_ANALYZING"

        task = tm.transition(task["id"], "RESULT_READY")
        assert task["state"] == "RESULT_READY"

        db.close()


@pytest.mark.asyncio
async def test_orchestrator_fault_injection():
    """Test orchestrator handles max iterations correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, logs_dir=os.path.join(tmp, "logs")
        )

        cfg = make_config(exit_code=1, max_retries=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)

        # Alternating plan/review for 20 iterations
        plan_seq = []
        for _ in range(20):
            plan_seq.append({
                "verdict": "plan_ready",
                "next_task": {"prompt": "Do something", "task_type": "cline_exec"},
                "goal_complete": False, "reasoning": "keep trying",
            })
            plan_seq.append({
                "verdict": "approved",
                "next_task": None,
                "goal_complete": False, "reasoning": "continue",
            })

        codex_executor = MockCodexExecutor(plan_sequence=plan_seq)

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=3, max_failures=10,
        )

        goal = await orchestrator.start_goal(
            objective="Test iteration limit", max_iterations=3, max_failures=10,
        )

        deadline = time.time() + 20
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.5)

        goal = goal_manager.get_goal(goal["id"])
        assert goal["iteration_count"] == goal["max_iterations"], \
            f"Expected {goal['max_iterations']} iterations, got {goal['iteration_count']}"
        assert goal["state"] == "GOAL_FAILED", \
            f"Expected GOAL_FAILED due to iteration limit, got {goal['state']}"


@pytest.mark.asyncio
async def test_safety_guard_blocks_destructive():
    """SafetyGuard blocks destructive commands like rm -rf."""
    with tempfile.TemporaryDirectory() as tmp:
        guard = SafetyGuard(SafetyConfig(), workspace_root=tmp)

        # Should block rm -rf
        result = guard.check("rm -rf /tmp/data", cwd=tmp)
        assert not result.allowed, "rm -rf should be blocked"
        assert result.risk.value == "critical"

        # Should allow safe commands
        result2 = guard.check("python train.py --epochs 10", cwd=tmp)
        assert result2.allowed, "safe command should be allowed"

        # Should flag network in offline mode
        result3 = guard.check("curl https://example.com", cwd=tmp)
        assert result3.allowed, "curl should not be outright blocked"
        assert "network_access" in result3.checks_failed


@pytest.mark.asyncio
async def test_safety_guard_protected_paths():
    """SafetyGuard flags writes to protected paths."""
    with tempfile.TemporaryDirectory() as tmp:
        guard = SafetyGuard(SafetyConfig(
            protected_paths=["data/", "checkpoints/"],
        ), workspace_root=tmp)

        result = guard.check("python train.py --output data/results", cwd=tmp)
        assert result.requires_approval, "protected path should require approval"
        assert any("protected_path_referenced" in c for c in result.checks_failed)


@pytest.mark.asyncio
async def test_safety_guard_outside_workspace():
    """SafetyGuard blocks commands outside workspace."""
    with tempfile.TemporaryDirectory() as tmp:
        guard = SafetyGuard(SafetyConfig(), workspace_root=tmp)

        result = guard.check("ls /etc", cwd="/etc")
        assert not result.allowed, "cwd outside workspace should be blocked"
        assert "cwd_outside_project" in result.checks_failed


@pytest.mark.asyncio
async def test_safety_guard_audit_log():
    """SafetyGuard audit_log detects dangerous patterns in Cline output."""
    with tempfile.TemporaryDirectory() as tmp:
        guard = SafetyGuard(SafetyConfig(), workspace_root=tmp)

        log_path = os.path.join(tmp, "test.log")
        with open(log_path, "w") as f:
            f.write("Running tests...\n")
            f.write("Executing: rm -rf /tmp/old_data\n")
            f.write("Tests passed\n")

        violations = guard.audit_command_log(log_path)
        assert len(violations) > 0, "Should detect rm -rf in log"
        assert any("destructive_pattern" in v for v in violations)

        # Clean log should have no violations
        clean_path = os.path.join(tmp, "clean.log")
        with open(clean_path, "w") as f:
            f.write("Running tests...\n")
            f.write("python train.py --epochs 10\n")
            f.write("Tests passed\n")

        clean_violations = guard.audit_command_log(clean_path)
        assert len(clean_violations) == 0, "Clean log should have no violations"


@pytest.mark.asyncio
async def test_orchestrator_step_persistence():
    """Orchestrator persists current step for recovery."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)

        cfg = make_config(exit_code=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            logs_dir=os.path.join(tmp, "logs")
        )

        codex_executor = MockCodexExecutor(plan_sequence=[
            {
                "verdict": "plan_ready",
                "next_task": {"prompt": "Create a test file", "task_type": "cline_exec",
                              "training_expected": False},
                "goal_complete": False, "reasoning": "plan",
            },
            {
                "verdict": "approved",
                "next_task": None, "goal_complete": False, "reasoning": "good",
            },
            {
                "verdict": "goal_achieved",
                "next_task": None, "goal_complete": True, "reasoning": "done",
            },
        ])

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=5, max_failures=5,
        )

        goal = await orchestrator.start_goal(
            objective="Test step persistence", max_iterations=5, max_failures=5,
        )

        # Wait for completion
        deadline = time.time() + 30
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.5)

        goal = goal_manager.get_goal(goal["id"])
        # After completion, orchestrator_step should be cleared
        assert goal.get("orchestrator_step", "") == "",             f"orchestrator_step should be empty after completion, got {goal.get('orchestrator_step')}"

        # Verify skip_next_plan column exists and defaults to 0
        assert goal.get("skip_next_plan", None) is not None,             "skip_next_plan column should exist"

        db.close()


@pytest.mark.asyncio
async def test_needs_changes_skip_plan():
    """When review returns needs_changes, skip_next_plan flag is set."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)

        cfg = make_config(exit_code=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            logs_dir=os.path.join(tmp, "logs")
        )

        codex_executor = MockCodexExecutor(plan_sequence=[
            {"verdict": "plan_ready",
             "next_task": {"prompt": "Do X", "task_type": "cline_exec"},
             "goal_complete": False, "reasoning": "plan"},
            {"verdict": "needs_changes",
             "next_task": {"prompt": "Fix the bugs in X", "task_type": "cline_exec"},
             "goal_complete": False, "reasoning": "needs fixes"},
            {"verdict": "goal_achieved",
             "next_task": None, "goal_complete": True, "reasoning": "done"},
        ])

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=3, max_failures=5,
        )

        goal = await orchestrator.start_goal(
            objective="Test needs_changes flow", max_iterations=3, max_failures=5,
        )

        deadline = time.time() + 30
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.5)

        goal = goal_manager.get_goal(goal["id"])
        assert goal["state"] == "GOAL_COMPLETED",             f"Expected GOAL_COMPLETED, got {goal['state']}"

        # Should have 2 tasks (original + needs_changes fixup)
        tasks = db.get_tasks_by_goal(goal["id"])
        assert len(tasks) >= 2, f"Expected at least 2 tasks, got {len(tasks)}"

        db.close()


@pytest.mark.asyncio
async def test_blocked_verdict_fails_goal():
    """Blocked review verdict should fail the goal, not complete it."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)

        cfg = make_config(exit_code=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            logs_dir=os.path.join(tmp, "logs")
        )

        # Codex returns blocked after first plan
        codex_executor = MockCodexExecutor(plan_sequence=[
            {"verdict": "plan_ready",
             "next_task": {"prompt": "Do something", "task_type": "cline_exec"},
             "goal_complete": False, "reasoning": "plan"},
            {"verdict": "blocked",
             "next_task": None, "goal_complete": False,
             "reasoning": "Cannot proceed without API key"},
        ])

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=3, max_failures=5,
        )

        goal = await orchestrator.start_goal(
            objective="Test blocked flow", max_iterations=3, max_failures=5,
        )

        deadline = time.time() + 30
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.5)

        goal = goal_manager.get_goal(goal["id"])
        assert goal["state"] == "GOAL_FAILED",             f"Blocked should FAIL, got {goal['state']}"
        assert "blocked" in goal.get("error_summary", "").lower(),             f"Error summary should mention blocked, got {goal.get('error_summary')}"

        db.close()


@pytest.mark.asyncio
async def test_training_events_emitted():
    """TrainingManager emits events on completion/failure."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        task_manager = TaskManager(db)
        event_manager = EventManager(db)

        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            logs_dir=os.path.join(tmp, "logs")
        )

        # Create a training task
        task = task_manager.create_task(
            task_type="training", prompt="echo hello",
        )

        # Store training command and re-read from DB for spawn
        db.update_task_field(task["id"], training_command="echo hello")
        task = db.get_task(task["id"])  # re-read to get training_command

        # Spawn the training (echo completes immediately)
        run_id, info = await training_manager.spawn(task)

        # Wait for completion
        deadline = time.time() + 10
        event = None
        while time.time() < deadline:
            event = await event_manager.wait_for_event(
                event_types=["TRAINING_COMPLETED", "TRAINING_FAILED", "RESULT_READY"],
                timeout=1,
            )
            if event:
                break

        assert event is not None, "Should receive a training event"
        assert event["event_type"] in ("TRAINING_COMPLETED", "RESULT_READY"),             f"Expected TRAINING_COMPLETED or RESULT_READY, got {event['event_type']}"

        db.close()


@pytest.mark.asyncio
async def test_event_version_persistence():
    """EventManager version persists across re-initialization."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)

        em1 = EventManager(db)
        e1 = em1.emit_event("TEST_EVENT", task_id="t1")
        v1 = em1._state_version

        # Simulate restart: create new EventManager on same DB
        em2 = EventManager(db)
        v2 = em2._state_version
        assert v2 >= v1, f"Version should persist: v1={v1}, v2={v2}"

        e2 = em2.emit_event("TEST_EVENT2", task_id="t2")
        v3 = em2._state_version
        assert v3 > v2, "Version should increment"

        db.close()


        db.close()
