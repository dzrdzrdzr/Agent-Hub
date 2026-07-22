"""Tests for safely deleting terminal goals."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_hub.config import AgentdConfig, LogsConfig
from agent_hub.db import Database
from agent_hub.goal_manager import GoalManager
from agent_hub.server import IPCServer


def make_goal(db: Database, goal_id: str, state: str = "GOAL_COMPLETED"):
    db.create_goal(goal_id, f"objective for {goal_id}")
    db.update_goal_state(goal_id, state)


def test_delete_terminal_goal_cascades_and_preserves_project_outputs(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    logs_dir = tmp_path / "logs"
    cline_dir = logs_dir / "cline"
    cline_dir.mkdir(parents=True)
    outside_dir = tmp_path / "project-output"
    outside_dir.mkdir()

    make_goal(db, "goal-delete")
    db.create_task("task-parent", goal_id="goal-delete", prompt="parent")
    # Descendants are part of the goal even if an old row lacks goal_id.
    db.create_task("task-child", parent_task_id="task-parent", prompt="child")

    stdout_path = cline_dir / "task-parent.stdout.log"
    stderr_path = cline_dir / "task-parent.stderr.log"
    result_path = logs_dir / "task-parent.result.json"
    prompt_path = cline_dir / "task-parent.prompt.txt"
    output_path = outside_dir / "valuable-output.bin"
    metrics_path = outside_dir / "metrics.json"
    for path in (stdout_path, stderr_path, result_path, prompt_path,
                 output_path, metrics_path):
        path.write_text("data", encoding="utf-8")

    db.update_task_field(
        "task-parent",
        log_stdout=str(stdout_path),
        log_stderr=str(stderr_path),
        result_file=str(result_path),
        output_path=str(output_path),
        metrics_path=str(metrics_path),
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO events (event_id, goal_id, task_id, event_type) "
            "VALUES (?, ?, ?, ?)",
            ("event-delete", "goal-delete", "task-parent", "TEST"),
        )
        conn.execute(
            "INSERT INTO model_calls "
            "(goal_id, task_id, model_role, start_time, status) "
            "VALUES (?, ?, ?, datetime('now'), ?)",
            ("goal-delete", "task-child", "planner", "started"),
        )
        conn.execute(
            "INSERT INTO task_locks (lock_name, holder_task_id) VALUES (?, ?)",
            ("delete-test-lock", "task-child"),
        )

    summary = db.delete_goal("goal-delete", logs_dir=str(logs_dir))

    assert summary["goal_id"] == "goal-delete"
    assert summary["deleted_task_count"] == 2
    assert set(summary["artifact_paths"]) == {
        str(stdout_path), str(stderr_path), str(result_path), str(prompt_path),
    }
    assert str(output_path) not in summary["artifact_paths"]
    assert str(metrics_path) not in summary["artifact_paths"]
    assert db.get_goal("goal-delete") is None
    assert db.get_task("task-parent") is None
    assert db.get_task("task-child") is None
    for table in ("events", "model_calls", "state_transitions", "task_locks"):
        assert db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    # The DB layer only returns safe cleanup candidates; the IPC layer unlinks.
    assert output_path.exists()
    assert metrics_path.exists()
    db.close()


@pytest.mark.parametrize("state", ["GOAL_CREATED", "GOAL_EXECUTING"])
def test_delete_rejects_active_goal(tmp_path, state):
    db = Database(str(tmp_path / "state.sqlite"))
    db.create_goal("goal-active", "still active")
    if state != "GOAL_CREATED":
        db.update_goal_state("goal-active", state)
    manager = GoalManager(db)

    with pytest.raises(ValueError, match="only terminal goals"):
        manager.delete_goal("goal-active")
    assert db.get_goal("goal-active") is not None
    db.close()


def test_delete_rejects_missing_goal_and_running_task(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    with pytest.raises(ValueError, match="not found"):
        db.delete_goal("goal-missing")

    make_goal(db, "goal-running", "GOAL_CANCELLED")
    db.create_task("task-running", goal_id="goal-running")
    db.update_task_state("task-running", "CLINE_RUNNING")
    with pytest.raises(ValueError, match="still has running tasks"):
        db.delete_goal("goal-running")
    assert db.get_goal("goal-running") is not None
    assert db.get_task("task-running") is not None
    db.close()


def test_symlink_outside_logs_is_not_cleanup_candidate(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("keep", encoding="utf-8")
    symlink = logs_dir / "looks-safe.log"
    symlink.symlink_to(outside)

    make_goal(db, "goal-symlink")
    db.create_task("task-symlink", goal_id="goal-symlink")
    db.update_task_field("task-symlink", log_stdout=str(symlink))
    summary = db.delete_goal("goal-symlink", logs_dir=str(logs_dir))

    assert str(symlink) not in summary["artifact_paths"]
    assert outside.exists()
    db.close()


@pytest.mark.asyncio
async def test_ipc_delete_unlinks_hub_logs_and_pushes_event(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    artifact = logs_dir / "task-ipc.stdout.log"
    artifact.write_text("log", encoding="utf-8")
    project_output = tmp_path / "project-result.txt"
    project_output.write_text("keep", encoding="utf-8")

    make_goal(db, "goal-ipc", "GOAL_FAILED")
    db.create_task("task-ipc", goal_id="goal-ipc")
    db.update_task_field(
        "task-ipc", log_stdout=str(artifact), output_path=str(project_output)
    )
    manager = GoalManager(db)
    config = AgentdConfig(logs=LogsConfig(dir=str(logs_dir)))
    server = IPCServer(config, None, None, None, None, goal_manager=manager)
    pushed = []

    async def capture_push(event, data):
        pushed.append((event, data))

    server.push = capture_push
    result = await server._h_delete_goal({"goal_id": "goal-ipc"})

    assert result == {"goal_id": "goal-ipc", "deleted_task_count": 1}
    assert not artifact.exists()
    assert project_output.exists()
    assert pushed == [(
        "goal_deleted",
        {"goal_id": "goal-ipc", "deleted_task_count": 1},
    )]
    db.close()


@pytest.mark.asyncio
async def test_ipc_delete_requires_goal_id(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    server = IPCServer(
        AgentdConfig(), None, None, None, None, goal_manager=GoalManager(db)
    )
    with pytest.raises(ValueError, match="goal_id is required"):
        await server._h_delete_goal({})
    db.close()


@pytest.mark.asyncio
async def test_ipc_delete_rejects_active_orchestration(tmp_path):
    db = Database(str(tmp_path / "state.sqlite"))
    make_goal(db, "goal-orchestrating", "GOAL_CANCELLED")

    class ActiveOrchestrator:
        _active_orchestrations = {"goal-orchestrating": object()}

    server = IPCServer(
        AgentdConfig(), None, None, None, None,
        goal_manager=GoalManager(db), orchestrator=ActiveOrchestrator(),
    )
    with pytest.raises(ValueError, match="active orchestration"):
        await server._h_delete_goal({"goal_id": "goal-orchestrating"})
    assert db.get_goal("goal-orchestrating") is not None
    db.close()
