"""Training process manager: spawn, monitor, recover independent training processes.

Training processes survive Cline exit. Agent Hub owns the full lifecycle.
"""

import os
import sys
import json
import time
import uuid
import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

from .db import Database
from .process_watcher import (
    verify_process_identity, check_process_alive,
    terminate_process_tree, check_log_freshness
)

logger = logging.getLogger(__name__)

TRAINING_STATES = {
    "TRAINING_QUEUED", "TRAINING_STARTING", "TRAINING_RUNNING",
    "TRAINING_COMPLETED", "TRAINING_FAILED", "TRAINING_STALLED",
    "RESULT_ANALYZING", "RESULT_READY"
}

TRAINING_KEY_EVENTS = {
    "TRAINING_COMPLETED", "TRAINING_FAILED", "TRAINING_STALLED",
    "RESULT_READY"
}


class TrainingManager:
    """Manages independent training processes tracked via tasks table."""

    def __init__(self, db: Database, task_manager=None,
                 stall_threshold: int = 300,
                 logs_dir: str = ".agent-hub/logs/"):
        self.db = db
        self.task_manager = task_manager
        self.stall_threshold = stall_threshold
        self.logs_dir = logs_dir
        self._monitors = {}    # task_id -> asyncio.Task
        self._locks = {}       # task_id -> asyncio.Lock
        self._processes = {}   # task_id -> process info

    def _get_lock(self, task_id: str) -> asyncio.Lock:
        if task_id not in self._locks:
            self._locks[task_id] = asyncio.Lock()
        return self._locks[task_id]

    def training_task_id(self, parent_task_id: str) -> str:
        """Generate training task ID from parent Cline task."""
        return f"train-{parent_task_id.split('-', 1)[-1]}"

    async def request_training(self, task: Dict[str, Any],
                               training_cmd: str, training_cwd: str = "",
                               env_vars: dict = None) -> Dict[str, Any]:
        """Request training from a Cline task's structured result."""
        task_id = task["id"]
        training_id = self.training_task_id(task_id)
        cwd = training_cwd or task.get("cline_cwd") or os.getcwd()

        # Create training task linked to parent
        tm = self.task_manager
        if tm:
            training_task = tm.create_task(
                task_id=training_id,
                task_type="training",
                prompt=f"Training: {training_cmd[:200]}",
                goal_id=task.get("goal_id"),
                parent_task_id=task_id,
                task_sequence=task.get("task_sequence", 0),
            )
        else:
            training_task = {"id": training_id, "state": "TRAINING_QUEUED"}

        # Store training config on the Cline task
        env_json = json.dumps(env_vars) if env_vars else None
        self.db.update_task_field(
            task_id,
            training_requested=1,
            training_command=training_cmd,
            training_cwd=cwd,
            training_env=env_json,
        )

        logger.info(f"Training requested for task {task_id}: {training_cmd[:100]}")
        return training_task

    async def spawn(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Spawn training process from task training_command."""
        task_id = task["id"]
        lock = self._get_lock(task_id)
        async with lock:
            return await self._spawn_locked(task)

    async def _spawn_locked(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        task_id = task["id"]
        cmd = task.get("training_command", "")
        cwd = task.get("training_cwd") or task.get("cline_cwd") or os.getcwd()
        env_json = task.get("training_env", "")

        if not cmd:
            raise ValueError(f"No training_command for task {task_id}")

        # Build env
        env = os.environ.copy()
        env["AGENT_HUB_TASK_ID"] = task_id
        env["AGENT_HUB_TRAINING"] = "1"
        if task.get("goal_id"):
            env["AGENT_HUB_GOAL_ID"] = task["goal_id"]
        if env_json:
            try:
                extra = json.loads(env_json)
                env.update(extra)
            except json.JSONDecodeError:
                pass

        # Setup log paths
        run_id = uuid.uuid4().hex[:8]
        logs_dir = os.path.join(cwd, self.logs_dir, "training")
        os.makedirs(logs_dir, exist_ok=True)
        stdout_path = os.path.join(logs_dir, f"{task_id}.stdout.log")
        stderr_path = os.path.join(logs_dir, f"{task_id}.stderr.log")

        # Parse command
        import shlex
        cmd_parts = shlex.split(cmd)

        logger.info(f"Spawning training: {' '.join(cmd_parts[:3])}... (task={task_id}, run={run_id})")

        # Open log files
        stdout_f = open(stdout_path, "w")
        stderr_f = open(stderr_path, "w")

        # Spawn as new process group so it survives parent exit
        if sys.platform != "win32":
            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=stdout_f, stderr=stderr_f,
                cwd=cwd, env=env,
                preexec_fn=os.setsid  # new session → survives Cline exit
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=stdout_f, stderr=stderr_f,
                cwd=cwd, env=env,
                creationflags=0x00000200  # CREATE_NEW_PROCESS_GROUP on Windows
            )

        pid = process.pid

        # Store training state on task
        self.db.update_task_field(
            task_id,
            training_pid=pid,
            training_log=stdout_path,
            output_path=os.path.join(cwd, ".agent-hub", "training_results", f"{task_id}"),
            metrics_path=os.path.join(cwd, ".agent-hub", "training_results", f"{task_id}", "metrics.json"),
            training_state="TRAINING_RUNNING",
        )

        self._processes[task_id] = {
            "pid": pid, "process": process, "run_id": run_id,
            "stdout": stdout_path, "stderr": stderr_path,
            "stdout_f": stdout_f, "stderr_f": stderr_f,
        }

        # Start monitor
        self._monitors[task_id] = asyncio.create_task(
            self._monitor_training(task_id, process, stdout_path, stderr_path, stdout_f, stderr_f)
        )

        logger.info(f"Training spawned: PID={pid}, task={task_id}")
        return run_id, {"pid": pid, "run_id": run_id}

    async def _monitor_training(self, task_id: str, process,
                                 stdout_path: str, stderr_path: str,
                                 stdout_f, stderr_f):
        """Monitor training process until completion."""
        stall_interval = max(30, self.stall_threshold // 4)

        # Start stall detection loop
        stall_task = asyncio.create_task(
            self._monitor_stall(task_id, stdout_path, stderr_path, stall_interval)
        )

        try:
            exit_code = await process.wait()

            # Close log files
            for f in [stdout_f, stderr_f]:
                try:
                    f.close()
                except Exception:
                    pass

            stall_task.cancel()
            try:
                await stall_task
            except asyncio.CancelledError:
                pass

            # Classify result
            if exit_code == 0:
                self.db.update_task_field(
                    task_id, training_state="TRAINING_COMPLETED",
                    training_exit_code=0
                )
                logger.info(f"Training {task_id}: completed (exit=0)")
                # Auto-trigger result analysis
                await self._trigger_result_ready(task_id)
            else:
                self.db.update_task_field(
                    task_id, training_state="TRAINING_FAILED",
                    training_exit_code=exit_code
                )
                logger.warning(f"Training {task_id}: failed (exit={exit_code})")

        except Exception as e:
            logger.error(f"Training monitor error for {task_id}: {e}")
            self.db.update_task_field(
                task_id, training_state="TRAINING_FAILED",
                error_summary=str(e)[:200]
            )
        finally:
            self._monitors.pop(task_id, None)
            self._processes.pop(task_id, None)

    async def _monitor_stall(self, task_id: str, stdout_path: str,
                              stderr_path: str, interval: int):
        """Detect training stall via log inactivity."""
        while task_id in self._monitors:
            await asyncio.sleep(interval)
            if task_id not in self._monitors:
                return

            try:
                mtimes = []
                for p in [stdout_path, stderr_path]:
                    if os.path.exists(p):
                        mtimes.append(os.path.getmtime(p))
                if mtimes:
                    newest = max(mtimes)
                    age = time.time() - newest
                    if age > self.stall_threshold:
                        logger.warning(f"Training {task_id} stalled: no output for {age:.0f}s")
                        self.db.update_task_field(
                            task_id, training_state="TRAINING_STALLED"
                        )
                        # Kill stalled process
                        if task_id in self._processes:
                            pid = self._processes[task_id].get("pid")
                            if pid:
                                terminate_process_tree(pid, timeout=3.0)
                        return
            except Exception as e:
                logger.error(f"Stall monitor error {task_id}: {e}")

    async def _trigger_result_ready(self, task_id: str):
        """After training completes, generate result package."""
        self.db.update_task_field(task_id, training_state="RESULT_ANALYZING")

        # Extract metrics
        task = self.db.get_task(task_id)
        metrics = await self._extract_metrics(task)

        # Write result package
        self.db.update_task_field(
            task_id,
            training_state="RESULT_READY",
            structured_result=json.dumps(metrics, ensure_ascii=False),
        )
        logger.info(f"Training {task_id}: RESULT_READY")

    async def _extract_metrics(self, task: Dict[str, Any]) -> dict:
        """Extract training metrics from log files."""
        task_id = task["id"]
        metrics = {
            "task_id": task_id,
            "goal_id": task.get("goal_id"),
            "training_exit_code": task.get("training_exit_code"),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "metrics": {},
            "errors": [],
            "summary": "",
        }

        log_path = task.get("training_log", "")
        if log_path and os.path.exists(log_path):
            try:
                with open(log_path, "r") as f:
                    content = f.read()

                # Extract loss/acc patterns
                import re
                loss_matches = re.findall(r'loss[=:]?\s*(\d+\.?\d*)', content)
                acc_matches = re.findall(r'acc[=:]?\s*(\d+\.?\d*)', content)

                if loss_matches:
                    final_loss = float(loss_matches[-1])
                    metrics["metrics"]["final_loss"] = final_loss
                if acc_matches:
                    final_acc = float(acc_matches[-1])
                    metrics["metrics"]["final_accuracy"] = final_acc

                # Check for errors
                for line in content.splitlines()[-20:]:
                    if 'error' in line.lower() or 'exception' in line.lower():
                        metrics["errors"].append(line.strip()[:200])

                # Summary
                last_lines = content.strip().splitlines()[-5:]
                metrics["summary"] = "\n".join(last_lines)
            except Exception as e:
                metrics["errors"].append(f"Failed to parse log: {e}")

        # Check for result file
        output_path = task.get("output_path", "")
        if output_path:
            result_file = os.path.join(output_path, "result.json") if os.path.isdir(output_path) \
                else output_path
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r") as f:
                        metrics["result_file"] = json.load(f)
                except Exception:
                    pass

        return metrics

    async def stop(self, task_id: str):
        """Stop a training process."""
        lock = self._get_lock(task_id)
        async with lock:
            proc_info = self._processes.get(task_id)
            if proc_info:
                pid = proc_info.get("pid")
                if pid:
                    terminate_process_tree(pid, timeout=3.0)
            self._monitors.pop(task_id, None)
            self._processes.pop(task_id, None)
            self.db.update_task_field(task_id, training_state="TRAINING_FAILED")

    async def shutdown(self):
        """Shutdown: cancel monitors but leave training processes running."""
        logger.info(f"Training manager shutdown: detaching from {len(self._monitors)} monitors")
        for task_id in list(self._monitors.keys()):
            monitor = self._monitors.pop(task_id, None)
            if monitor:
                monitor.cancel()
        self._processes.clear()

    def attach_monitor(self, task_id: str, pid: int):
        """Re-attach monitor for a surviving training process after restart."""
        task = self.db.get_task(task_id)
        if not task:
            return

        stdout_path = task.get("training_log", "")
        stderr_path = stdout_path.replace(".stdout.", ".stderr.") if stdout_path else ""

        if not stdout_path:
            logger.warning(f"Cannot attach training monitor for {task_id}: no log path")
            return

        async def _recovery_monitor():
            try:
                while check_process_alive(pid):
                    await asyncio.sleep(30)
                exit_code = None
                try:
                    import psutil
                    exit_code = psutil.Process(pid).wait()
                except Exception:
                    pass
                self.db.update_task_field(
                    task_id, training_state="TRAINING_COMPLETED",
                    training_exit_code=exit_code
                )
                await self._trigger_result_ready(task_id)
            except Exception as e:
                logger.error(f"Recovery training monitor error {task_id}: {e}")

        self._monitors[task_id] = asyncio.create_task(_recovery_monitor())
        self._processes[task_id] = {"pid": pid}
        logger.info(f"Re-attached training monitor for {task_id} PID {pid}")

    def get_active_training_tasks(self) -> List[Dict[str, Any]]:
        """Get tasks in training states."""
        return self.db.fetch_all(
            "SELECT * FROM tasks WHERE training_state IN (?,?,?)",
            ("TRAINING_RUNNING", "TRAINING_STARTING", "TRAINING_QUEUED")
        )

    def get_training_tasks_by_goal(self, goal_id: str) -> List[Dict[str, Any]]:
        return self.db.fetch_all(
            "SELECT * FROM tasks WHERE goal_id = ? AND training_pid IS NOT NULL "
            "ORDER BY task_sequence",
            (goal_id,)
        )
