"""Daemon restart recovery: reconcile SQLite state with OS process table."""

import logging
from typing import List, Dict, Any

from .db import Database
from .process_watcher import verify_process_identity, check_process_alive

logger = logging.getLogger(__name__)


async def run_recovery(db: Database, task_manager=None) -> List[Dict[str, Any]]:
    """Recover task states after daemon restart.

    Algorithm:
    1. Find all tasks in non-terminal states
    2. For each, verify process identity
    3. If verified: resume monitoring
    4. If not verified: check result/log files, mark appropriately
    5. Auto-retry eligible failed tasks
    """
    recovered = []
    import os

    # Get tasks that were running when daemon stopped
    active_states = ["CLINE_STARTING", "CLINE_RUNNING"]
    tasks = []
    for state in active_states:
        tasks.extend(db.get_tasks_by_state(state))

    logger.info(f"Recovery: found {len(tasks)} tasks in active states")

    for task in tasks:
        task_id = task["id"]
        pid = task.get("cline_pid")

        if pid and check_process_alive(pid):
            # Process exists. Verify it's ours.
            is_ours, reason = verify_process_identity({
                "pid": pid,
                "start_time": task.get("cline_start_time"),
                "cmd_hash": task.get("cline_cmd_hash"),
                "cwd": task.get("cline_cwd"),
                "id": task_id,
            })

            if is_ours:
                logger.info(f"Recovery: task {task_id} PID {pid} verified, resuming")
                # If it was STARTING, it's now RUNNING
                if task["state"] == "CLINE_STARTING":
                    db.update_task_state(task_id, "CLINE_RUNNING", trigger="recovery_resume", pid=pid)
                recovered.append({"task_id": task_id, "action": "resumed", "pid": pid})
                continue
            else:
                logger.warning(f"Recovery: task {task_id} PID {pid} NOT ours: {reason}")
                db.update_task_state(task_id, "CLINE_FAILED",
                                      trigger="recovery_pid_mismatch", pid=pid)
                recovered.append({"task_id": task_id, "action": "pid_mismatch", "reason": reason})
        else:
            # Process gone. Check result files.
            result_file = task.get("result_file")
            if result_file and os.path.exists(result_file):
                db.update_task_state(task_id, "CLINE_SUCCEEDED",
                                      trigger="recovery_result_found")
                recovered.append({"task_id": task_id, "action": "result_found"})
            else:
                db.update_task_state(task_id, "CLINE_FAILED",
                                      trigger="recovery_process_gone")
                recovered.append({"task_id": task_id, "action": "process_gone"})

        # Auto-retry if eligible
        task = db.get_task(task_id)
        if task["state"] == "CLINE_FAILED" and task["retry_count"] < task["max_retries"]:
            db.update_task_state(task_id, "CLINE_STARTING", trigger="recovery_retry")
            recovered.append({"task_id": task_id, "action": "auto_retried"})

    logger.info(f"Recovery complete: {len(recovered)} actions")
    return recovered
