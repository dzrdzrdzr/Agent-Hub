"""Async Cline CLI executor. Non-blocking, env injection, log redirect."""

import os
import sys
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

import psutil

from .config import AgentdConfig, resolve_cline_path, cmd_hash, load_cline_extension_config

logger = logging.getLogger(__name__)


class ClineResult:
    """Result of a Cline execution."""
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALLED = "STALLED"
    TIMED_OUT = "TIMED_OUT"




class MockClineExecutor:
    """Mock Cline executor for testing without real Cline/API key."""

    def __init__(self, config, task_manager=None):
        self.config = config
        self.task_manager = task_manager

    async def spawn(self, task):
        import asyncio
        task_id = task["id"]
        run_id = "mock-" + task_id[:8]
        delay = self.config.cline.mock_delay_seconds
        exit_code = self.config.cline.mock_exit_code
        tm = self.task_manager

        max_retries = self.config.cline.max_retries
        for attempt in range(max_retries + 1):
            await asyncio.sleep(delay)
            if tm:
                tm.db.update_task_field(task_id, cline_exit_code=exit_code, cline_pid=99999, run_id=run_id)
                if exit_code == 0:
                    tm.transition(task_id, "CLINE_SUCCEEDED", trigger="mock_exit_zero")
                    return run_id, {"pid": 99999, "run_id": run_id, "mock": True}
                t = tm.get_task(task_id)
                if t["retry_count"] < t["max_retries"]:
                    tm.transition(task_id, "CLINE_FAILED", trigger="mock_exit_nonzero")
                    tm.transition(task_id, "CLINE_STARTING", trigger="mock_retry")
                    continue
        if tm:
            tm.transition(task_id, "CLINE_FAILED", trigger="mock_exit_terminal")
        return run_id, {"pid": 99999, "run_id": run_id, "mock": True}

    async def stop(self, task_id):
        pass

    async def shutdown(self):
        pass

class ClineExecutor:
    """Manages async Cline CLI subprocess execution."""

    def __init__(self, config: AgentdConfig, task_manager=None):
        self.config = config
        self.task_manager = task_manager
        self._running = {}  # task_id -> asyncio.Task
        self._processes = {}  # task_id -> asyncio.subprocess.Process
        self._stall_tasks = {}  # task_id -> asyncio.Task (stall monitor)

    def _build_env(self, task_id: str, run_id: str) -> dict:
        """Build environment with GAUSS ownership vars."""
        env = os.environ.copy()
        env["GAUSS_AGENT_TASK_ID"] = task_id
        env["GAUSS_AGENT_RUN_ID"] = run_id
        for k, v in self.config.cline.env.items():
            env[k] = v
        return env

    def _build_cmd(self, cline_path: str, cwd: str, prompt_file: str,
                   stdout_file: str, stderr_file: str) -> list:
        """Build Cline CLI command list (no shell string)."""
        return [
            cline_path,
            "-p",
            "-c", cwd,
            "--timeout", str(self.config.cline.timeout_seconds),
            "--auto-approve", "true",
        ]

    async def spawn(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Spawn Cline asynchronously. Returns (run_id, process_info).

        Does NOT block. Registers exit callback. Main loop continues.
        """
        task_id = task["id"]
        run_id = uuid.uuid4().hex[:8]
        cwd = task.get("cline_cwd") or os.getcwd()

        # Resolve Cline path
        cline_path = resolve_cline_path(self.config.cline.executable)
        logger.info(f"Cline path: {cline_path} (v{self.config.cline.executable})")

        # Prepare log paths
        logs_dir = os.path.join(cwd, self.config.logs.dir, "cline")
        os.makedirs(logs_dir, exist_ok=True)
        stdout_path = os.path.join(logs_dir, f"{task_id}.stdout.log")
        stderr_path = os.path.join(logs_dir, f"{task_id}.stderr.log")

        # Write prompt to file for stdin redirection
        prompt_file = os.path.join(logs_dir, f"{task_id}.prompt.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(task.get("prompt", ""))

        # Build command and environment
        cmd = self._build_cmd(cline_path, cwd, prompt_file, stdout_path, stderr_path)
        env = self._build_env(task_id, run_id)

        # Open log files
        stdout_f = open(stdout_path, "w", encoding="utf-8")
        stderr_f = open(stderr_path, "w", encoding="utf-8")

        logger.info(f"Spawning Cline: {' '.join(cmd)} (task={task_id}, run={run_id})")

        # Spawn async subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=stdout_f,
            stderr=stderr_f,
            cwd=cwd,
            env=env,
        )

        # Write prompt to stdin, then close it
        if process.stdin:
            with open(prompt_file, "rb") as pf:
                process.stdin.write(pf.read())
            process.stdin.close()

        self._processes[task_id] = process

        # Capture process identity
        try:
            psproc = psutil.Process(process.pid)
            start_time = psproc.create_time()
            ppid = psproc.ppid()
        except Exception:
            start_time = datetime.now(timezone.utc).timestamp()
            ppid = 0

        proc_info = {
            "cline_pid": process.pid,
            "cline_start_time": start_time,
            "cline_cmd_hash": cmd_hash(cmd),
            "cline_cwd": cwd,
            "cline_ppid": ppid,
            "run_id": run_id,
            "log_stdout": stdout_path,
            "log_stderr": stderr_path,
        }

        # Update DB with process info
        if self.task_manager:
            self.task_manager.db.update_task_field(task_id, **proc_info)

        # Register exit handler (non-blocking)
        exit_task = asyncio.create_task(self._wait_exit(task_id, process, stdout_f, stderr_f))
        self._running[task_id] = exit_task

        # Start stall monitor
        stall_task = asyncio.create_task(self._monitor_stall(task_id, stdout_path, stderr_path))
        self._stall_tasks[task_id] = stall_task

        logger.info(f"Cline spawned: PID={process.pid}, task={task_id}, run={run_id}")
        return run_id, proc_info

    async def _wait_exit(self, task_id: str, process, stdout_f, stderr_f):
        """Wait for Cline to exit, then classify result."""
        try:
            exit_code = await process.wait()
        except Exception as e:
            logger.error(f"Error waiting for Cline {task_id}: {e}")
            exit_code = -1
        finally:
            stdout_f.close()
            stderr_f.close()

        # Cancel stall monitor
        stall_task = self._stall_tasks.pop(task_id, None)
        if stall_task:
            stall_task.cancel()

        self._running.pop(task_id, None)
        self._processes.pop(task_id, None)

        # Classify result: exit 0 = SUCCEEDED
        task = self.task_manager.get_task(task_id) if self.task_manager else None
        if exit_code == 0:
            result = ClineResult.SUCCEEDED
        else:
            result = ClineResult.FAILED

        logger.info(f"Cline {task_id}: exit_code={exit_code}, result={result}")

        if self.task_manager:
            try:
                task = self.task_manager.get_task(task_id)
                self.task_manager.db.update_task_field(task_id, cline_exit_code=exit_code)

                if result == ClineResult.SUCCEEDED:
                    self.task_manager.transition(task_id, "CLINE_SUCCEEDED",
                                                  trigger="cline_exit_zero")
                else:
                    task = self.task_manager.get_task(task_id)
                    if task["retry_count"] < task["max_retries"]:
                        # Auto-retry
                        self.task_manager.transition(task_id, "CLINE_FAILED",
                                                      trigger="cline_exit_nonzero")
                        self.task_manager.transition(task_id, "CLINE_STARTING",
                                                      trigger="auto_retry")
                        # Re-spawn and move to RUNNING
                        await self.spawn(task)
                        self.task_manager.transition(task_id, "CLINE_RUNNING",
                                                      trigger="retry_spawned")
                    else:
                        self.task_manager.transition(task_id, "CLINE_FAILED",
                                                      trigger="cline_exit_nonzero_terminal")
            except Exception as e:
                logger.error(f"Error classifying Cline exit {task_id}: {e}")

    async def _monitor_stall(self, task_id: str, stdout_path: str, stderr_path: str):
        """Monitor for stall (no log output within threshold)."""
        threshold = self.config.cline.stall_threshold_seconds
        await asyncio.sleep(threshold)

        # Check if still running
        if task_id not in self._running:
            return

        try:
            mtimes = []
            for p in [stdout_path, stderr_path]:
                if os.path.exists(p):
                    mtimes.append(os.path.getmtime(p))
            if mtimes:
                newest = max(mtimes)
                age = asyncio.get_event_loop().time() - newest
                if age > threshold:
                    logger.warning(f"Cline {task_id} stalled: no output for {age:.0f}s")
                    if self.task_manager:
                        task = self.task_manager.get_task(task_id)
                        if task["retry_count"] < task["max_retries"]:
                            self.task_manager.transition(task_id, "CLINE_STALLED",
                                                          trigger="stall_detected")
                            # Kill the stalled process
                            process = self._processes.get(task_id)
                            if process:
                                try:
                                    process.kill()
                                except Exception:
                                    pass
                            self.task_manager.transition(task_id, "CLINE_STARTING",
                                                          trigger="stall_retry")
                            await self.spawn(task)
                        else:
                            self.task_manager.transition(task_id, "CLINE_STALLED",
                                                          trigger="stall_terminal")
        except Exception as e:
            logger.error(f"Stall monitor error {task_id}: {e}")

    async def stop(self, task_id: str):
        """Stop a running Cline process."""
        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
            except Exception as e:
                logger.error(f"Error stopping Cline {task_id}: {e}")
        self._running.pop(task_id, None)
        self._processes.pop(task_id, None)
        stall = self._stall_tasks.pop(task_id, None)
        if stall:
            stall.cancel()

    async def shutdown(self):
        """Stop all running Cline processes."""
        for task_id in list(self._running.keys()):
            await self.stop(task_id)
