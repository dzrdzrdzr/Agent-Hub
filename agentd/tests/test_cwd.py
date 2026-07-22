"""Focused tests for arbitrary per-task/per-goal working-directory support."""

import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_hub.cline_executor import ClineExecutor, MockClineExecutor
from agent_hub.codex_executor import CodexExecutor, MockCodexExecutor
from agent_hub.config import AgentdConfig, ClineConfig, LogsConfig, SafetyConfig
from agent_hub.db import Database, SCHEMA_VERSION
from agent_hub.event_manager import EventManager
from agent_hub.goal_manager import GoalManager
from agent_hub.goal_orchestrator import GoalOrchestrator
from agent_hub.safety_guard import SafetyGuard
from agent_hub.server import IPCServer, validate_workspace_cwd
from agent_hub.task_manager import TaskManager
from agent_hub.training_manager import TrainingManager


class TestCwdValidation:
    @pytest.mark.parametrize("value", [None, "", "relative/path", "/"])
    def test_reject_invalid_directory(self, value):
        with pytest.raises(ValueError):
            validate_workspace_cwd(value)

    def test_reject_missing_directory(self, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            validate_workspace_cwd(str(tmp_path / "missing"))

    def test_absolute_symlink_is_canonicalized(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        assert validate_workspace_cwd(str(alias)) == str(target.resolve())

    def test_schema_version_is_6(self, tmp_path):
        db = Database(str(tmp_path / "state.sqlite"))
        assert db.get_schema_version() == SCHEMA_VERSION == 6
        db.close()


def test_cline_cwd_persisted_in_task(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    tm = TaskManager(db)
    task = tm.create_task(prompt="test", cline_cwd=str(tmp_path))
    assert tm.get_task(task["id"])["cline_cwd"] == str(tmp_path)
    db.close()


class _RecordingExecutor:
    def __init__(self):
        self.task = None

    async def spawn(self, task):
        self.task = task
        return "run-test", {"pid": 1}


class _HangingCodexExecutor:
    async def plan(self, goal, cwd=None, env=None):
        await asyncio.sleep(30)


@pytest.mark.asyncio
async def test_submit_task_persists_and_spawns_with_canonical_cwd(tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    alias = tmp_path / "project-link"
    alias.symlink_to(target, target_is_directory=True)
    db = Database(str(tmp_path / "state.sqlite"))
    tm = TaskManager(db)
    executor = _RecordingExecutor()
    server = IPCServer(
        None, tm, executor, None,
        SafetyGuard(SafetyConfig(), workspace_root=str(tmp_path)),
    )

    result = await server._h_submit_task({
        "prompt": "inspect files", "cwd": str(alias),
    })
    task = tm.get_task(result["task_id"])
    assert task["cline_cwd"] == str(target.resolve())
    assert executor.task["cline_cwd"] == str(target.resolve())
    db.close()


def test_workspace_cwd_persisted_in_goal(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    gm = GoalManager(db)
    goal = gm.create_goal(objective="test goal", workspace_cwd=str(tmp_path))
    assert gm.get_goal(goal["id"])["workspace_cwd"] == str(tmp_path)
    db.close()


def test_safety_guard_per_request_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    guard = SafetyGuard(SafetyConfig(), workspace_root=str(tmp_path / "default"))
    result = guard.check(
        f"echo hello > {project / 'output.txt'}",
        cwd=str(project), workspace_root=str(project),
    )
    assert result.allowed
    assert not any("redirect_outside_workspace" in c for c in result.checks_failed)


def test_safety_guard_blocks_redirect_outside_boundary(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside.txt"
    project.mkdir()
    guard = SafetyGuard(SafetyConfig(), workspace_root=str(project))
    result = guard.check(
        f"echo hello > {outside}", cwd=str(project), workspace_root=str(project),
    )
    assert not result.allowed
    assert any("redirect_outside_workspace" in c for c in result.checks_failed)


@pytest.mark.asyncio
async def test_goal_cwd_inherited_by_linked_tasks(tmp_path):
    project = tmp_path / "external-project"
    project.mkdir()
    db = Database(str(tmp_path / "state.sqlite"))
    task_manager = TaskManager(db)
    goal_manager = GoalManager(db)
    event_manager = EventManager(db)
    config = AgentdConfig(cline=ClineConfig(
        mock=True, mock_exit_code=0, mock_delay_seconds=0.01,
        max_retries=1, stall_threshold_seconds=30,
    ))
    codex_executor = MockCodexExecutor(plan_sequence=[
        {"verdict": "plan_ready", "goal_complete": False,
         "next_task": {"prompt": "run experiment", "task_type": "cline_exec"},
         "reasoning": "single task plan"},
        {"verdict": "goal_achieved", "goal_complete": True,
         "next_task": None, "reasoning": "done"},
    ])
    orchestrator = GoalOrchestrator(
        db=db, goal_manager=goal_manager, event_manager=event_manager,
        task_manager=task_manager,
        cline_executor=MockClineExecutor(config, task_manager=task_manager),
        codex_executor=codex_executor,
        max_iterations=2, max_failures=2,
    )
    goal = await orchestrator.start_goal(
        "external project test", workspace_cwd=str(project),
    )

    deadline = time.time() + 10
    while time.time() < deadline:
        current = goal_manager.get_goal(goal["id"])
        if current["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
            break
        await asyncio.sleep(0.05)

    tasks = db.get_tasks_by_goal(goal["id"])
    assert tasks
    assert all(t["cline_cwd"] == str(project) for t in tasks)
    assert codex_executor.call_cwds
    assert all(cwd == str(project) for cwd in codex_executor.call_cwds)
    await orchestrator.shutdown()
    db.close()


@pytest.mark.asyncio
async def test_planner_timeout_falls_back_to_goal_objective(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    db = Database(str(tmp_path / "state.sqlite"))
    task_manager = TaskManager(db)
    goal_manager = GoalManager(db)
    orchestrator = GoalOrchestrator(
        db=db,
        goal_manager=goal_manager,
        event_manager=EventManager(db),
        task_manager=task_manager,
        codex_executor=_HangingCodexExecutor(),
        codex_step_timeout=0.01,
    )
    goal = goal_manager.create_goal(
        objective="inspect the project",
        workspace_cwd=str(project),
    )

    await orchestrator._step_plan(goal)

    current = goal_manager.get_goal(goal["id"])
    task = task_manager.get_task(current["current_task_id"])
    assert task["prompt"] == "inspect the project"
    assert task["cline_cwd"] == str(project)
    assert "planner_timeout_fallback" in current["latest_codex_decision"]
    db.close()


@pytest.mark.asyncio
async def test_training_inherits_parent_task_cwd(tmp_path):
    project = tmp_path / "ml-project"
    project.mkdir()
    db = Database(str(tmp_path / "state.sqlite"))
    task_manager = TaskManager(db)
    manager = TrainingManager(
        db, task_manager=task_manager, event_manager=EventManager(db),
        logs_dir=".agent-hub/logs", runtime_root=str(tmp_path),
    )
    parent = task_manager.create_task(
        task_type="cline_exec", prompt="train model", cline_cwd=str(project),
    )
    train_task = await manager.request_training(parent, training_cmd="python3 train.py")
    assert train_task["cline_cwd"] == str(project)
    assert manager.logs_dir == str(tmp_path / ".agent-hub" / "logs")
    await manager.shutdown()
    db.close()


def test_migration_v5_to_v6_adds_workspace_cwd(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    db = Database(db_path)
    conn = db._get_conn()
    conn.execute("ALTER TABLE goals DROP COLUMN workspace_cwd")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (5)")
    conn.commit()
    db.close()

    migrated = Database(db_path)
    columns = [row[1] for row in migrated.execute("PRAGMA table_info(goals)").fetchall()]
    assert "workspace_cwd" in columns
    assert migrated.get_schema_version() == 6
    migrated.close()


def test_cli_default_cwd_is_invoker_cwd(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "agent-hub.py"
    spec = importlib.util.spec_from_file_location("agent_hub_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = {}

    def fake_rpc(method, params=None, **kwargs):
        captured.update(method=method, params=params)
        return {"task_id": "task-test", "state": "CLINE_STARTING"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "rpc", fake_rpc)
    monkeypatch.setattr(sys, "argv", [str(script), "--json", "submit", "check"])
    assert module.main() == 0
    assert captured == {
        "method": "submit_task",
        "params": {"prompt": "check", "cwd": str(tmp_path)},
    }


def test_cline_logs_are_centralized(tmp_path):
    config = AgentdConfig(
        cline=ClineConfig(mock=True), logs=LogsConfig(dir=".agent-hub/logs"),
    )
    executor = ClineExecutor(config, runtime_root=str(tmp_path))
    assert executor._resolve_logs_dir() == str(
        tmp_path / ".agent-hub" / "logs" / "cline"
    )


@pytest.mark.asyncio
async def test_cancelling_codex_stops_owned_process(tmp_path):
    codex = tmp_path / "codex"
    pid_file = tmp_path / "codex.pid"
    codex.write_text(
        "#!/bin/sh\nfor last_arg do :; done\n"
        "echo $$ > \"$last_arg\"\nsleep 30\n"
    )
    codex.chmod(0o755)
    executor = CodexExecutor(str(codex), timeout=60, max_retries=0)

    call = asyncio.create_task(executor.run(str(pid_file), cwd=str(tmp_path)))
    deadline = time.time() + 3
    while not pid_file.exists() and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    pid = int(pid_file.read_text().strip())

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
