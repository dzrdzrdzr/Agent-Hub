"""Focused regression tests for Goal IPC, push, terminal, and review evidence fixes."""

import json
import os
import sys
import tempfile
import asyncio
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_hub.db import Database
from agent_hub.cline_executor import MockClineExecutor
from agent_hub.codex_executor import MockCodexExecutor
from agent_hub.config import AgentdConfig, ClineConfig
from agent_hub.event_manager import EventManager
from agent_hub.goal_manager import GoalManager
from agent_hub.goal_orchestrator import GoalOrchestrator
from agent_hub.result_packager import ResultPackager
from agent_hub.server import IPCServer
from agent_hub.task_manager import TaskManager
from agent_hub.training_manager import TrainingManager


class _FakeDb:
    def fetch_one(self, sql, params=()):
        assert "COUNT(*)" in sql
        return {"cnt": 2}

    def fetch_all(self, sql, params=()):
        return [{"id": "task-1", "goal_id": params[0]}]


class _FakeGoalManager:
    def __init__(self):
        self.db = _FakeDb()

    def get_active_goals(self):
        return [{"id": "goal-1", "state": "GOAL_EXECUTING"}]

    def get_goal(self, goal_id):
        return {"id": goal_id, "state": "GOAL_REVIEWING", "objective": "test"}


class _FakeOrchestrator:
    def __init__(self):
        self.kwargs = None
        self.push_callback = None

    def set_push_callback(self, callback):
        self.push_callback = callback

    async def start_goal(self, **kwargs):
        self.kwargs = kwargs
        return {"id": "goal-1", "state": "GOAL_PLANNING", **kwargs}


@pytest.mark.asyncio
async def test_start_goal_forwards_limits_and_list_goals_counts_tasks():
    orchestrator = _FakeOrchestrator()
    server = IPCServer(None, None, None, None, None,
                       goal_manager=_FakeGoalManager(), orchestrator=orchestrator)

    result = await server._h_start_goal({
        "objective": "ship it",
        "completion_criteria": "tests pass",
        "stop_conditions": "blocked",
        "max_iterations": 4,
        "max_failures": 2,
        "model_call_budget": 7,
        "cwd": os.getcwd(),
    })

    assert result["state"] == "GOAL_PLANNING"
    assert orchestrator.kwargs["completion_criteria"] == "tests pass"
    assert orchestrator.kwargs["stop_conditions"] == "blocked"
    assert orchestrator.kwargs["max_iterations"] == 4
    assert orchestrator.kwargs["max_failures"] == 2
    assert orchestrator.kwargs["model_call_budget"] == 7
    assert orchestrator.push_callback is not None

    listed = await server._h_list_goals({"include_terminal": False})
    assert listed["goals"][0]["task_count"] == 2

    with pytest.raises(ValueError, match="max_iterations"):
        await server._h_start_goal({
            "objective": "bad", "max_iterations": 0, "cwd": os.getcwd(),
        })


@pytest.mark.asyncio
async def test_goal_push_callback_uses_latest_persisted_state():
    manager = _FakeGoalManager()
    orchestrator = GoalOrchestrator(
        db=None, goal_manager=manager, event_manager=None,
    )
    pushed = []

    async def capture(event, data):
        pushed.append((event, data))

    orchestrator.set_push_callback(capture)
    await orchestrator._push_goal_state("goal-1")
    assert pushed == [("goal_state_changed", {
        "goal_id": "goal-1", "state": "GOAL_REVIEWING", "objective": "test",
    })]


def test_terminal_goal_operations_clear_orchestrator_step():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "state.sqlite"))
        manager = GoalManager(db)
        goal = manager.create_goal("terminal cleanup")
        manager.transition(goal["id"], "GOAL_PLANNING")
        manager.set_orchestrator_step(goal["id"], "reviewing")
        manager.complete_goal(goal["id"])
        assert manager.get_goal(goal["id"])["orchestrator_step"] == ""
        db.close()


@pytest.mark.asyncio
async def test_review_package_contains_bounded_ansi_stripped_cline_output():
    with tempfile.TemporaryDirectory() as tmp:
        stdout_path = os.path.join(tmp, "stdout.log")
        stderr_path = os.path.join(tmp, "stderr.log")
        marker = "GOAL_WORKFLOW_OK"
        with open(stdout_path, "w", encoding="utf-8") as stream:
            stream.write("x" * 20000 + "\n\x1b[32m" + marker + "\x1b[0m\n")
        with open(stderr_path, "w", encoding="utf-8") as stream:
            stream.write("warning\n")

        package = await ResultPackager(baseline_dir=tmp).build_package(
            {"id": "goal-1", "objective": "review evidence"},
            {"id": "task-1", "log_stdout": stdout_path,
             "log_stderr": stderr_path, "state": "CLINE_SUCCEEDED"},
        )

        stdout_tail = package["cli_output"]["stdout"]
        assert marker in stdout_tail
        assert "\x1b[" not in stdout_tail
        assert len(stdout_tail.encode("utf-8")) <= 10240
        json.dumps(package)


@pytest.mark.asyncio
async def test_review_decision_is_latest_and_all_major_states_are_pushed():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "state.sqlite"))
        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        config = AgentdConfig(cline=ClineConfig(
            mock=True, mock_exit_code=0, mock_delay_seconds=0.01,
            max_retries=1, stall_threshold_seconds=30,
        ))
        cline_executor = MockClineExecutor(config, task_manager=task_manager)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            logs_dir=os.path.join(tmp, "logs"),
        )
        codex_executor = MockCodexExecutor(plan_sequence=[
            {"verdict": "plan_ready", "goal_complete": False,
             "next_task": {"prompt": "emit marker", "task_type": "cline_exec"},
             "reasoning": "plan"},
            {"verdict": "goal_achieved", "goal_complete": True,
             "next_task": None, "reasoning": "review verified output"},
        ])
        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=2, max_failures=2,
        )
        pushed_states = []

        async def capture(event, data):
            if event == "goal_state_changed":
                pushed_states.append(data["state"])

        goal = await orchestrator.start_goal(
            "finish after reviewed output", completion_criteria="review says achieved",
            max_iterations=2, max_failures=2, model_call_budget=4,
            push_callback=capture,
        )
        assert goal["state"] == "GOAL_PLANNING"
        assert goal["max_iterations"] == 2
        assert goal["max_failures"] == 2
        assert goal["model_call_budget"] == 4

        deadline = time.time() + 10
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.05)

        assert goal["state"] == "GOAL_COMPLETED"
        assert json.loads(goal["latest_codex_decision"])["verdict"] == "goal_achieved"
        assert goal["orchestrator_step"] == ""
        assert {"GOAL_PLANNING", "GOAL_EXECUTING", "GOAL_WAITING_EVENT",
                "GOAL_REVIEWING", "GOAL_COMPLETED"}.issubset(set(pushed_states))
        await orchestrator.shutdown()
        await training_manager.shutdown()
        db.close()


@pytest.mark.asyncio
async def test_needs_changes_at_iteration_limit_does_not_leave_queued_fixup():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "state.sqlite"))
        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        config = AgentdConfig(cline=ClineConfig(
            mock=True, mock_exit_code=0, mock_delay_seconds=0.01,
            max_retries=1, stall_threshold_seconds=30,
        ))
        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager,
            cline_executor=MockClineExecutor(config, task_manager=task_manager),
            codex_executor=MockCodexExecutor(plan_sequence=[
                {"verdict": "plan_ready", "goal_complete": False,
                 "next_task": {"prompt": "first task", "task_type": "cline_exec"},
                 "reasoning": "plan"},
                {"verdict": "needs_changes", "goal_complete": False,
                 "next_task": {"prompt": "fixup", "task_type": "cline_exec"},
                 "reasoning": "needs another iteration"},
            ]),
            max_iterations=1, max_failures=1,
        )
        goal = await orchestrator.start_goal(
            "one round only", max_iterations=1, max_failures=1,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.05)

        assert goal["state"] == "GOAL_FAILED"
        assert goal["error_summary"] == "max_iterations_reached"
        tasks = db.get_tasks_by_goal(goal["id"])
        assert len(tasks) == 1
        assert all(task["state"] != "QUEUED" for task in tasks)
        await orchestrator.shutdown()
        db.close()
