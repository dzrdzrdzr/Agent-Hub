import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agent_hub.db import Database
from agent_hub.task_manager import TaskManager, is_valid_transition, is_terminal

def test_transitions():
    assert is_valid_transition("QUEUED", "CLINE_STARTING")
    assert is_valid_transition("CLINE_RUNNING", "CLINE_SUCCEEDED")
    assert is_valid_transition("CLINE_RUNNING", "CLINE_FAILED")
    assert not is_valid_transition("QUEUED", "CLINE_RUNNING")
    assert not is_valid_transition("CLINE_SUCCEEDED", "QUEUED")

    assert is_terminal("CLINE_SUCCEEDED")
    assert is_terminal("CANCELLED")
    assert not is_terminal("CLINE_RUNNING")

def test_task_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.sqlite")
        db = Database(db_path)
        tm = TaskManager(db)

        task = tm.create_task(prompt="test")
        assert task["state"] == "QUEUED"

        task = tm.transition(task["id"], "CLINE_STARTING", trigger="test")
        assert task["state"] == "CLINE_STARTING"

        task = tm.transition(task["id"], "CLINE_RUNNING", trigger="spawned")
        assert task["state"] == "CLINE_RUNNING"

        task = tm.transition(task["id"], "CLINE_FAILED", trigger="crash")
        assert task["state"] == "CLINE_FAILED"

        # Retry
        task = tm.transition(task["id"], "CLINE_STARTING", trigger="retry")
        assert task["retry_count"] == 1

        # Second fail (terminal since max_retries=1)
        task = tm.transition(task["id"], "CLINE_RUNNING", trigger="spawned")
        task = tm.transition(task["id"], "CLINE_FAILED", trigger="crash_again")
        assert task["state"] == "CLINE_FAILED"

        db.close()
        import time; time.sleep(0.1)
        print("task manager tests passed")
