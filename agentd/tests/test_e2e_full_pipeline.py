"""End-to-end test: full autonomous closed-loop pipeline with training.

Covers:
  plan → execute(Cline mock) → training(real subprocess) → review → complete
  Idempotent training spawn
  Daemon restart recovery (training survives)
  RESULT_READY → review → continue/complete
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
async def test_e2e_full_pipeline_with_training():
    """Full autonomous pipeline: plan → execute → train → review → complete."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        logs_dir = os.path.join(tmp, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        task_manager = TaskManager(db)
        goal_manager = GoalManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            stall_threshold=300, logs_dir=logs_dir,
        )

        # Create a simple training script that writes metrics
        training_script = os.path.join(tmp, "train.py")
        with open(training_script, "w") as f:
            f.write("""import json, os, sys
metrics = {"final_loss": 0.05, "best_loss": 0.03, "final_accuracy": 0.95, "best_accuracy": 0.97}
output_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
os.makedirs(output_dir, exist_ok=True)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump(metrics, f)
print("Training completed. loss=0.05 acc=0.95")
""")

        cfg = make_config(exit_code=0)
        cline_executor = MockClineExecutor(cfg, task_manager=task_manager)

        codex_executor = MockCodexExecutor(plan_sequence=[
            # Iteration 1: plan — include training command
            {
                "verdict": "plan_ready",
                "next_task": {
                    "prompt": "Run training with python3 train.py",
                    "task_type": "cline_exec",
                    "training_command": f"python3 {training_script} {tmp}/results",
                },
                "goal_complete": False,
                "reasoning": "iteration 1 plan with training",
            },
            # Iteration 1: review — approve
            {
                "verdict": "approved",
                "next_task": None,
                "goal_complete": False,
                "reasoning": "training results look good, continue",
            },
            # Iteration 2: plan — goal achieved
            {
                "verdict": "goal_achieved",
                "next_task": None,
                "goal_complete": True,
                "reasoning": "goal achieved after training",
            },
        ])

        orchestrator = GoalOrchestrator(
            db=db, goal_manager=goal_manager, event_manager=event_manager,
            task_manager=task_manager, cline_executor=cline_executor,
            training_manager=training_manager, codex_executor=codex_executor,
            max_iterations=5, max_failures=2,
        )

        # The Cline mock needs structured_result with training_command
        # so that _extract_training_cmd can find it.
        # We monkey-patch the mock spawn to inject training_command into
        # the task's structured_result after spawn.
        original_spawn = cline_executor.spawn
        async def spawn_with_training(task):
            task_id = task["id"]
            result = await original_spawn(task)
            # Inject structured_result with training_command from the plan
            # The next_task from the plan has training_command
            # We need to store it on the Cline task
            train_cmd = f"python3 {training_script} {tmp}/results"
            db.update_task_field(task_id, structured_result=json.dumps({
                "training_command": train_cmd,
                "changed_files": ["train.py"],
                "test_results": "pass",
            }))
            return result
        cline_executor.spawn = spawn_with_training

        goal = await orchestrator.start_goal(
            objective="Train a model to achieve loss < 0.1 and accuracy > 0.9",
            completion_criteria="loss < 0.1, accuracy > 0.9",
            max_iterations=5,
            max_failures=2,
        )

        deadline = time.time() + 60
        while time.time() < deadline:
            goal = goal_manager.get_goal(goal["id"])
            if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                break
            await asyncio.sleep(0.5)

        goal = goal_manager.get_goal(goal["id"])
        assert goal is not None, "Goal should exist"
        assert goal["state"] == "GOAL_COMPLETED", \
            f"Expected GOAL_COMPLETED, got {goal['state']} (error: {goal.get('error_summary', 'none')})"

        # Verify training was triggered
        tasks = db.get_tasks_by_goal(goal["id"])
        cline_tasks = [t for t in tasks if t["task_type"] == "cline_exec"]
        train_tasks = [t for t in tasks if t["task_type"] == "training"]

        assert len(cline_tasks) >= 1, f"Expected >= 1 Cline tasks, got {len(cline_tasks)}"
        assert len(train_tasks) >= 1, f"Expected >= 1 training task, got {len(train_tasks)}"

        # Verify training completed with RESULT_READY
        for tt in train_tasks:
            assert tt.get("training_state") in ("TRAINING_COMPLETED", "RESULT_READY"), \
                f"Training should be completed, got {tt.get('training_state')}"
            assert tt.get("state") in ("TRAINING_COMPLETED", "RESULT_READY"), \
                f"Training task state should be synced, got {tt.get('state')}"

        # Verify model calls
        calls = db.get_model_calls(goal_id=goal["id"])
        assert len(calls) >= 2, f"Expected >= 2 model calls, got {len(calls)}"

        assert goal["failure_count"] == 0, f"Expected 0 failures, got {goal['failure_count']}"

        db.close()


@pytest.mark.asyncio
async def test_training_idempotent_spawn():
    """request_training + spawn called twice shouldn't duplicate training."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        logs_dir = os.path.join(tmp, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        task_manager = TaskManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            stall_threshold=300, logs_dir=logs_dir,
        )

        # Create a training script
        training_script = os.path.join(tmp, "train2.py")
        with open(training_script, "w") as f:
            f.write("""import json, os, sys
output_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
os.makedirs(output_dir, exist_ok=True)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump({"loss": 0.1}, f)
print("Done")
""")

        # Create parent Cline task
        parent = task_manager.create_task(
            task_id="task-parent-001", task_type="cline_exec",
            prompt="run training", goal_id="g1",
        )

        # First request_training
        train1 = await training_manager.request_training(
            parent, f"python3 {training_script} {tmp}/r1",
        )
        assert train1 is not None
        train_id = train1["id"]

        # Second request_training — should return existing (idempotent)
        train2 = await training_manager.request_training(
            parent, f"python3 {training_script} {tmp}/r1",
        )
        assert train2 is not None
        assert train2["id"] == train_id, "Second request should return same training task"

        # Verify training_requested is set on parent
        parent = db.get_task("task-parent-001")
        assert parent["training_requested"] == 1

        # Verify training task has correct goal_id
        train_task = db.get_task(train_id)
        assert train_task["goal_id"] == "g1"
        assert train_task["parent_task_id"] == "task-parent-001"

        db.close()


@pytest.mark.asyncio
async def test_training_daemon_restart_survival():
    """Training process should survive daemon restart and recovery should reattach."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        logs_dir = os.path.join(tmp, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        task_manager = TaskManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            stall_threshold=300, logs_dir=logs_dir,
        )

        # Create a long-running training script
        training_script = os.path.join(tmp, "train_long.py")
        with open(training_script, "w") as f:
            f.write("""import json, os, sys, time
output_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
os.makedirs(output_dir, exist_ok=True)
# Write initial output to show we started
print("Training started...", flush=True)
time.sleep(2)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump({"loss": 0.1, "accuracy": 0.9}, f)
print("Training completed. loss=0.1 acc=0.9", flush=True)
""")

        # Create parent Cline task
        parent = task_manager.create_task(
            task_id="task-parent-002", task_type="cline_exec",
            prompt="run long training", goal_id="g2",
        )

        # Request and spawn training
        train_task = await training_manager.request_training(
            parent, f"python3 {training_script} {tmp}/r2",
        )
        run_id, info = await training_manager.spawn(train_task)

        pid = info.get("pid")
        assert pid is not None, "Training should have a PID"
        assert pid > 0

        # Verify training is running
        train_task = db.get_task(train_task["id"])
        assert train_task["training_state"] == "TRAINING_RUNNING"
        assert train_task["state"] == "TRAINING_RUNNING"

        # Simulate daemon restart: detach monitor
        await training_manager.shutdown()

        # Verify process is still alive (survived detachment)
        import psutil
        assert psutil.pid_exists(pid), "Training should survive daemon shutdown"

        # Simulate recovery reattach
        attached = training_manager.attach_monitor(train_task["id"], pid)
        assert attached, "Should reattach to surviving training process"

        # Wait for training to complete
        deadline = time.time() + 30
        while time.time() < deadline:
            t = db.get_task(train_task["id"])
            if t["training_state"] in ("TRAINING_COMPLETED", "RESULT_READY"):
                break
            await asyncio.sleep(0.5)

        t = db.get_task(train_task["id"])
        assert t["training_state"] in ("TRAINING_COMPLETED", "RESULT_READY"), \
            f"Training should complete after reattach, got {t['training_state']}"
        assert t["state"] in ("TRAINING_COMPLETED", "RESULT_READY"), \
            f"Task state should be synced, got {t['state']}"

        db.close()


@pytest.mark.asyncio
async def test_no_duplicate_training_on_recovery():
    """Daemon restart recovery should NOT spawn a second training process."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        logs_dir = os.path.join(tmp, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        task_manager = TaskManager(db)
        event_manager = EventManager(db)
        training_manager = TrainingManager(
            db, task_manager=task_manager, event_manager=event_manager,
            stall_threshold=300, logs_dir=logs_dir,
        )

        # Create a long training script
        training_script = os.path.join(tmp, "train3.py")
        with open(training_script, "w") as f:
            f.write("""import json, os, sys, time
output_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
os.makedirs(output_dir, exist_ok=True)
print("Training started...", flush=True)
time.sleep(1)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump({"loss": 0.05}, f)
print("Done", flush=True)
""")

        parent = task_manager.create_task(
            task_id="task-parent-003", task_type="cline_exec",
            prompt="run training", goal_id="g3",
        )

        train_task = await training_manager.request_training(
            parent, f"python3 {training_script} {tmp}/r3",
        )

        # First spawn
        run_id1, info1 = await training_manager.spawn(train_task)
        pid1 = info1.get("pid")

        # Try spawning again (simulates recovery re-spawn attempt)
        run_id2, info2 = await training_manager.spawn(train_task)

        # Should return the same running process, not a new one
        pid2 = info2.get("pid")
        assert pid2 == pid1, \
            f"Second spawn should not create new process: {pid1} vs {pid2}"

        # Clean up — cancel monitor and wait for process exit
        await training_manager.shutdown()
        import psutil
        try:
            proc = psutil.Process(pid1)
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass

        db.close()


@pytest.mark.asyncio
async def test_training_extract_cmd_from_structured_result():
    """_extract_training_cmd should find training_command in structured_result."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite")
        db = Database(db_path)
        tm = TaskManager(db)
        em = EventManager(db)
        gm = GoalManager(db)

        # Create a minimal orchestrator just to test _extract_training_cmd
        orchestrator = GoalOrchestrator(
            db=db, goal_manager=gm, event_manager=em,
            task_manager=tm, max_iterations=1, max_failures=1,
        )

        # Test 1: structured_result with training_command
        task1 = {"structured_result": json.dumps({"training_command": "python3 train.py --epochs 10"})}
        cmd = orchestrator._extract_training_cmd(task1)
        assert cmd == "python3 train.py --epochs 10"

        # Test 2: no training_command
        task2 = {"structured_result": json.dumps({"changed_files": ["a.py"]})}
        cmd = orchestrator._extract_training_cmd(task2)
        assert cmd is None

        # Test 3: empty structured_result
        task3 = {"structured_result": ""}
        cmd = orchestrator._extract_training_cmd(task3)
        assert cmd is None

        # Test 4: from log file
        log_path = os.path.join(tmp, "test.log")
        with open(log_path, "w") as f:
            f.write('Some output\n```json\n{"training_command": "python3 train.py"}\n```\nMore output\n')
        task4 = {"log_stdout": log_path, "structured_result": ""}
        cmd = orchestrator._extract_training_cmd(task4)
        assert cmd == "python3 train.py"

        db.close()


if __name__ == "__main__":
    async def main():
        await test_training_extract_cmd_from_structured_result()
        print("PASS: test_training_extract_cmd_from_structured_result")
        await test_training_idempotent_spawn()
        print("PASS: test_training_idempotent_spawn")
        await test_no_duplicate_training_on_recovery()
        print("PASS: test_no_duplicate_training_on_recovery")
        await test_training_daemon_restart_survival()
        print("PASS: test_training_daemon_restart_survival")
        await test_e2e_full_pipeline_with_training()
        print("PASS: test_e2e_full_pipeline_with_training")
        print("\nAll full pipeline tests passed!")

    asyncio.run(main())
