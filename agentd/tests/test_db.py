import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agent_hub.db import Database

def test_db_create_task():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.sqlite")
        db = Database(db_path)
        task = db.create_task("task-001", prompt="test prompt")
        assert task["id"] == "task-001"
        assert task["state"] == "QUEUED"

        tasks = db.get_all_tasks()
        assert len(tasks) == 1

        db.update_task_state("task-001", "CLINE_STARTING", trigger="test")
        task = db.get_task("task-001")
        assert task["state"] == "CLINE_STARTING"

        transitions = db.fetch_all("SELECT * FROM state_transitions WHERE task_id = ?", ("task-001",))
        assert len(transitions) >= 2  # QUEUED created + CLINE_STARTING

        # Lock test
        ok = db.acquire_lock("test_lock", "task-001")
        assert ok
        ok2 = db.acquire_lock("test_lock", "task-002")
        assert not ok2
        db.release_lock("test_lock", "task-001")

        db.close()
        import time; time.sleep(0.1)
        print("db tests passed")
