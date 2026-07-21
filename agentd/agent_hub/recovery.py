"""Daemon restart recovery: reconcile SQLite state with OS process table.

Handles: goals, tasks (Cline), events, training processes.
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional

from .db import Database
from .process_watcher import verify_process_identity, check_process_alive

logger = logging.getLogger(__name__)


async def run_recovery(db: Database, task_manager=None,
                       cline_executor=None,
                       event_manager=None,
                       training_manager=None,
                       goal_manager=None,
                       orchestrator=None) -> List[Dict[str, Any]]:
    """Recover all states after daemon restart.

    Algorithm:
    1. Find all Cline tasks in non-terminal states → verify/reattach
    2. Find all training tasks in running states → verify/reattach
    3. Find all active goals → resume orchestrator
    4. Find all unacknowledged events → mark as pending
    """
    recovered = []

    # ---- Phase 1: Cline tasks ----
    if task_manager:
        recovered.extend(await _recover_cline_tasks(
            db, task_manager, cline_executor
        ))

    # ---- Phase 2: Training processes ----
    if training_manager and task_manager:
        recovered.extend(await _recover_training(
            db, task_manager, training_manager
        ))

    # ---- Phase 3: Goals ----
    if goal_manager and orchestrator:
        recovered.extend(await _recover_goals(
            db, goal_manager, orchestrator
        ))

    # ---- Phase 4: Events ----
    if event_manager:
        recovered.extend(_recover_events(db, event_manager))

    logger.info(f"Recovery complete: {len(recovered)} actions")
    return recovered


async def _recover_cline_tasks(db, task_manager, cline_executor) -> List[Dict]:
    """Recover Cline tasks in CLINE_STARTING or CLINE_RUNNING states."""
    recovered = []
    active_states = ["CLINE_STARTING", "CLINE_RUNNING"]
    tasks = []
    for state in active_states:
        tasks.extend(db.get_tasks_by_state(state))

    if not tasks:
        return recovered

    logger.info(f"Recovery: found {len(tasks)} Cline tasks in active states")

    for task in tasks:
        task_id = task["id"]
        pid = task.get("cline_pid")

        if pid and check_process_alive(pid):
            is_ours, reason = verify_process_identity({
                "pid": pid,
                "start_time": task.get("cline_start_time"),
                "cmd_hash": task.get("cline_cmd_hash"),
                "cmdline": (json.loads(task.get("cline_cmdline", "[]"))
                         if task.get("cline_cmdline") else []),
                "cwd": task.get("cline_cwd"),
                "id": task_id,
            })

            if is_ours:
                logger.info(f"Recovery: Cline {task_id} PID {pid} verified ({reason})")
                if task["state"] == "CLINE_STARTING":
                    db.update_task_state(task_id, "CLINE_RUNNING",
                                          trigger="recovery_resume", pid=pid)
                if cline_executor:
                    stdout = task.get("log_stdout") or _build_log_path(task, "stdout")
                    stderr = task.get("log_stderr") or _build_log_path(task, "stderr")
                    cline_executor.attach_monitor(task_id, pid, stdout, stderr)
                recovered.append({"task_id": task_id, "action": "resumed", "type": "cline"})
                continue
            else:
                logger.warning(f"Recovery: Cline {task_id} PID {pid} NOT ours: {reason}")
                db.update_task_state(task_id, "CLINE_FAILED",
                                      trigger="recovery_pid_mismatch")
                recovered.append({"task_id": task_id, "action": "pid_mismatch", "type": "cline"})
        else:
            # Process gone
            result_file = task.get("result_file")
            if result_file and os.path.exists(result_file):
                db.update_task_state(task_id, "CLINE_SUCCEEDED",
                                      trigger="recovery_result_found")
                recovered.append({"task_id": task_id, "action": "result_found", "type": "cline"})
            else:
                db.update_task_state(task_id, "CLINE_FAILED",
                                      trigger="recovery_process_gone")
                recovered.append({"task_id": task_id, "action": "process_gone", "type": "cline"})

        # Auto-retry if eligible — use task_manager for proper retry_count tracking
        task = db.get_task(task_id)
        if task["state"] == "CLINE_FAILED" and task["retry_count"] < task["max_retries"]:
            if task_manager:
                try:
                    task_manager.transition(task_id, "CLINE_STARTING",
                                             trigger="recovery_retry")
                except ValueError:
                    pass
            else:
                db.update_task_state(task_id, "CLINE_STARTING", trigger="recovery_retry")
            recovered.append({"task_id": task_id, "action": "auto_retry", "type": "cline"})
            if cline_executor:
                try:
                    await cline_executor.spawn(task)
                    if task_manager:
                        try:
                            task_manager.transition(task_id, "CLINE_RUNNING",
                                                     trigger="recovery_retry_spawned")
                        except ValueError:
                            pass
                    else:
                        db.update_task_state(task_id, "CLINE_RUNNING",
                                              trigger="recovery_retry_spawned")
                except Exception as e:
                    logger.error(f"Recovery spawn failed for {task_id}: {e}")
                    if task_manager:
                        try:
                            task_manager.transition(task_id, "CLINE_FAILED",
                                                     trigger="recovery_spawn_failed")
                        except ValueError:
                            pass
                    else:
                        db.update_task_state(task_id, "CLINE_FAILED",
                                              trigger="recovery_spawn_failed")

    return recovered


async def _recover_training(db, task_manager, training_manager) -> List[Dict]:
    """Recover training processes."""
    recovered = []
    active = db.fetch_all(
        "SELECT * FROM tasks WHERE training_state IN (?,?,?)",
        ("TRAINING_RUNNING", "TRAINING_STARTING", "TRAINING_QUEUED")
    )

    if not active:
        return recovered

    logger.info(f"Recovery: found {len(active)} training tasks")

    for task in active:
        task_id = task["id"]
        pid = task.get("training_pid")

        if pid and check_process_alive(pid):
            logger.info(f"Recovery: training {task_id} PID {pid} alive, reattaching")
            attached = training_manager.attach_monitor(task_id, pid)
            if attached:
                db.update_task_field(task_id, training_state="TRAINING_RUNNING")
            # If attach failed, state is already set to TRAINING_FAILED inside attach_monitor
            recovered.append({"task_id": task_id, "action": "reattached", "type": "training"})
        elif pid:
            logger.info(f"Recovery: training {task_id} PID {pid} dead")
            db.update_task_field(task_id, training_state="TRAINING_FAILED")
            recovered.append({"task_id": task_id, "action": "process_gone", "type": "training"})

    return recovered


async def _recover_goals(db, goal_manager, orchestrator) -> List[Dict]:
    """Resume orchestrator for active goals."""
    recovered = []
    active_goals = goal_manager.get_active_goals()

    if not active_goals:
        return recovered

    logger.info(f"Recovery: found {len(active_goals)} active goals")

    for goal in active_goals:
        goal_id = goal["id"]
        logger.info(f"Recovery: resuming goal {goal_id} (state={goal['state']})")
        await orchestrator.recover()
        recovered.append({"goal_id": goal_id, "action": "resumed", "type": "goal"})

    return recovered


def _recover_events(db, event_manager) -> List[Dict]:
    """Count unacknowledged events for recovery reporting."""
    recovered = []
    unack = event_manager.recover_unacknowledged()

    if unack:
        logger.info(f"Recovery: found {len(unack)} unacknowledged events")
        recovered.append({"count": len(unack), "action": "pending", "type": "events"})

    return recovered



def _build_log_path(task: Dict[str, Any], stream: str) -> str:
    """Build default log path from task metadata."""
    cwd = task.get("cline_cwd") or os.getcwd()
    task_id = task["id"]
    logs_dir = os.path.join(cwd, ".agent-hub", "logs", "cline")
    return os.path.join(logs_dir, f"{task_id}.{stream}.log")
