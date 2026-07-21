"""Async Cline CLI executor. Non-blocking, env injection, log redirect."""

import os
import sys
import uuid
import asyncio
import logging
import re
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

import json
import psutil
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .config import AgentdConfig, resolve_cline_path, cmd_hash, load_cline_extension_config
from .process_watcher import terminate_process_tree

logger = logging.getLogger(__name__)


class ClineResult:
    """Result of a Cline execution."""
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALLED = "STALLED"
    TIMED_OUT = "TIMED_OUT"


def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


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

        # Transition to RUNNING if currently STARTING (mimics real executor)
        if tm:
            t = tm.get_task(task_id)
            if t and t["state"] == "CLINE_STARTING":
                tm.transition(task_id, "CLINE_RUNNING", trigger="mock_spawned")

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

    def attach_monitor(self, task_id, pid, stdout_path, stderr_path):
        """No-op for mock."""
        pass


class ClineExecutor:
    """Manages async Cline CLI subprocess execution."""

    def __init__(self, config: AgentdConfig, task_manager=None,
                 safety_guard=None):
        self.config = config
        self.safety_guard = safety_guard
        self.task_manager = task_manager
        self._event_manager = None  # set by main._wire_events
        self._running = {}       # task_id -> asyncio.Task (_wait_exit)
        self._processes = {}     # task_id -> subprocess.Popen
        self._stall_tasks = {}   # task_id -> asyncio.Task (stall monitor)
        self._locks = {}         # task_id -> asyncio.Lock (per-task serialization)
        self._pids = {}          # task_id -> int (for recovery reattach)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cline-spawn")

    def _get_lock(self, task_id: str) -> asyncio.Lock:
        if task_id not in self._locks:
            self._locks[task_id] = asyncio.Lock()
        return self._locks[task_id]

    def _build_env(self, task_id: str, run_id: str) -> dict:
        """Build environment with Agent Hub ownership vars."""
        env = os.environ.copy()
        env["AGENT_HUB_TASK_ID"] = task_id
        env["AGENT_HUB_RUN_ID"] = run_id
        for k, v in self.config.cline.env.items():
            env[k] = v
        return env

    def _build_cmd(self, cline_path: str, cwd: str, prompt_file: str,
                   stdout_file: str, stderr_file: str) -> list:
        """Build Cline CLI command list (no shell string)."""
        return [
            cline_path,
            "-c", cwd,
            "--timeout", str(self.config.cline.timeout_seconds),
            "--auto-approve", "true",
        ]

    async def spawn(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Spawn Cline asynchronously. Returns (run_id, process_info).

        Does NOT block. Registers exit callback. Main loop continues.
        Uses per-task lock to prevent race with stop/retry.
        """
        task_id = task["id"]
        lock = self._get_lock(task_id)
        async with lock:
            return await self._spawn_locked(task)

    async def _spawn_locked(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Actual spawn logic, called under per-task lock."""
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

        logger.info(f"Spawning Cline: {' '.join(cmd)} (task={task_id}, run={run_id})")

        # Open log files for stdout/stderr redirect
        stdout_f = open(stdout_path, "w")
        stderr_f = open(stderr_path, "w")

        try:
            # Use subprocess.Popen in thread to avoid asyncio pipe/SIGCHLD issues
            kwargs: Dict[str, Any] = dict(
                stdin=subprocess.PIPE,
                stdout=stdout_f,
                stderr=stderr_f,
                cwd=cwd,
                env=env,
            )
            if sys.platform != "win32":
                kwargs["preexec_fn"] = os.setpgrp
            loop = asyncio.get_event_loop()
            process = await loop.run_in_executor(
                self._executor, lambda: subprocess.Popen(cmd, **kwargs))
        except FileNotFoundError as e:
            stdout_f.close(); stderr_f.close()
            logger.error(f"Cline executable not found for task {task_id}: {e}")
            if self.task_manager:
                self.task_manager.db.update_task_field(task_id, error_summary=f"spawn_failed: {e}")
                self.task_manager.transition(task_id, "CLINE_FAILED", trigger="spawn_exec_not_found")
            raise
        except Exception as e:
            stdout_f.close(); stderr_f.close()
            logger.error(f"Failed to spawn Cline for task {task_id}: {e}")
            if self.task_manager:
                self.task_manager.db.update_task_field(task_id, error_summary=f"spawn_failed: {e}")
                self.task_manager.transition(task_id, "CLINE_FAILED", trigger="spawn_exception")
            raise

        # Write prompt to stdin, then close it
        if process.stdin:
            with open(prompt_file, "rb") as pf:
                process.stdin.write(pf.read())
            process.stdin.close()

        self._processes[task_id] = process
        self._pids[task_id] = process.pid

        # Wait for exit, close files, and strip ANSI from logs
        async def _close_files():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, process.wait)
            stdout_f.close()
            stderr_f.close()
            # Strip ANSI escape codes from log files (Cline outputs rich terminal codes)
            def _filter_ansi():
                for log_path in (stdout_path, stderr_path):
                    try:
                        with open(log_path, 'r', encoding='utf-8', errors='replace') as lf:
                            raw = lf.read()
                        cleaned = _strip_ansi(raw)
                        with open(log_path, 'w', encoding='utf-8') as lf:
                            lf.write(cleaned)
                    except Exception:
                        pass
            await loop.run_in_executor(self._executor, _filter_ansi)
        asyncio.create_task(_close_files())

        # Capture process identity
        try:
            psproc = psutil.Process(process.pid)
            start_time = psproc.create_time()
            ppid = psproc.ppid()
            cmdline = psproc.cmdline()
        except Exception:
            start_time = datetime.now(timezone.utc).timestamp()
            ppid = 0
            cmdline = cmd

        proc_info = {
            "cline_pid": process.pid,
            "cline_start_time": start_time,
            "cline_cmd_hash": cmd_hash(cmd),  # keep for legacy compatibility
            "cline_cmdline": json.dumps(cmdline),  # serialize list to JSON for SQLite
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
        exit_task = asyncio.create_task(self._wait_exit(task_id, process))
        self._running[task_id] = exit_task

        # Start stall monitor (looping)
        stall_task = asyncio.create_task(
            self._monitor_stall_loop(task_id, stdout_path, stderr_path)
        )
        self._stall_tasks[task_id] = stall_task

        logger.info(f"Cline spawned: PID={process.pid}, task={task_id}, run={run_id}")
        return run_id, proc_info

    def attach_monitor(self, task_id: str, pid: int,
                        stdout_path: str, stderr_path: str):
        """Attach external monitoring for a process not spawned by us.

        Used by recovery to resume monitoring of tasks whose processes
        survived a daemon restart. Uses psutil polling since we cannot
        reconnect to the asyncio.subprocess.

        The stall monitor is a looping coroutine; the exit watcher polls
        the PID periodically.
        """
        if task_id in self._running:
            logger.warning(f"Task {task_id} already has a running monitor")
            return

        self._pids[task_id] = pid

        # Exit watcher using psutil polling (since we can't reconnect subprocess)
        async def _poll_exit():
            captured_exit_code = -1  # default: unknown failure
            try:
                while True:
                    if not psutil.pid_exists(pid):
                        captured_exit_code = -1
                        break
                    proc = psutil.Process(pid)
                    try:
                        captured_exit_code = proc.wait(timeout=5)
                        break
                    except psutil.TimeoutExpired:
                        continue
                    except psutil.NoSuchProcess:
                        captured_exit_code = -1
                        break
            except psutil.NoSuchProcess:
                captured_exit_code = -1
            except Exception as e:
                logger.error(f"Recovery exit poll error {task_id}: {e}")

            # Process exited — classify with actual exit code
            lock = self._get_lock(task_id)
            async with lock:
                await self._classify_exit(task_id, captured_exit_code)

        exit_task = asyncio.create_task(_poll_exit())
        self._running[task_id] = exit_task

        # Stall monitor
        stall_task = asyncio.create_task(
            self._monitor_stall_loop(task_id, stdout_path, stderr_path)
        )
        self._stall_tasks[task_id] = stall_task

        logger.info(f"Recovery monitor attached: task={task_id}, pid={pid}")

    async def _wait_exit(self, task_id: str, process):
        """Wait for Cline process to exit, then classify result.

        Uses ThreadPoolExecutor to run subprocess.Popen.wait() in a thread,
        avoiding asyncio's subprocess SIGCHLD issues with shebang scripts.
        """
        exit_code = -1
        try:
            loop = asyncio.get_event_loop()
            exit_code = await loop.run_in_executor(
                self._executor, process.wait)
        except Exception as e:
            logger.error(f"Error waiting for Cline {task_id}: {e}")

        # Hold per-task lock to serialize with stall/retry/stop paths
        lock = self._get_lock(task_id)
        async with lock:
            await self._classify_exit(task_id, exit_code)

    async def _classify_exit(self, task_id: str, exit_code: int):
        """Classify exit under lock. Handles retry logic."""
        # Cancel stall monitor
        stall_task = self._stall_tasks.pop(task_id, None)
        if stall_task:
            stall_task.cancel()
            try:
                await stall_task
            except asyncio.CancelledError:
                pass

        self._running.pop(task_id, None)
        self._processes.pop(task_id, None)
        self._pids.pop(task_id, None)

        if exit_code == 0:
            result = ClineResult.SUCCEEDED
        else:
            result = ClineResult.FAILED

        logger.info(f"Cline {task_id}: exit_code={exit_code}, result={result}")

        # Run post-hoc command audit on Cline log
        if self.safety_guard:
            task = self.task_manager.get_task(task_id) if self.task_manager else None
            if task:
                log_path = task.get("log_stdout", "")
                if log_path and os.path.exists(log_path):
                    try:
                        violations = self.safety_guard.audit_command_log(log_path)
                        if violations:
                            logger.warning(
                                f"Cline {task_id}: safety audit found {len(violations)} violation(s)"
                            )
                            self.task_manager.db.update_task_field(
                                task_id,
                                safety_violations=json.dumps(violations, ensure_ascii=False)
                            )
                    except Exception as e:
                        logger.debug(f"Cline {task_id}: audit skipped ({e})")

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
                        # Re-spawn (under a new lock acquisition in spawn())
                        await self._spawn_locked(task)
                        self.task_manager.transition(task_id, "CLINE_RUNNING",
                                                      trigger="retry_spawned")
                    else:
                        self.task_manager.transition(task_id, "CLINE_FAILED",
                                                      trigger="cline_exit_nonzero_terminal")
            except Exception as e:
                logger.error(f"Error classifying Cline exit {task_id}: {e}")

        # Emit event for orchestrator
        if self._event_manager:
            try:
                task = self.task_manager.get_task(task_id) if self.task_manager else None
                if task:
                    state = task.get("state", "")
                    event_type = {
                        "CLINE_SUCCEEDED": "CLINE_SUCCEEDED",
                        "CLINE_FAILED": "CLINE_FAILED",
                        "CLINE_STALLED": "CLINE_STALLED",
                    }.get(state, state)
                    if event_type:
                        self._event_manager.emit_task_related_event(
                            event_type, task, {"exit_code": exit_code})
            except Exception as e:
                logger.error(f"Error emitting classify event {task_id}: {e}")

    async def _monitor_stall_loop(self, task_id: str, stdout_path: str, stderr_path: str):
        """Looping stall monitor: checks every stall_threshold/2 seconds
        until the task finishes. Triggers retry on first stall detection,
        then exits (a new monitor is started on retry)."""
        threshold = self.config.cline.stall_threshold_seconds
        check_interval = max(30, threshold // 4)

        while task_id in self._running:
            await asyncio.sleep(check_interval)

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
                    age = time.time() - newest
                    if age > threshold:
                        logger.warning(f"Cline {task_id} stalled: no output for {age:.0f}s")
                        # Acquire lock to serialize with _wait_exit
                        lock = self._get_lock(task_id)
                        async with lock:
                            if task_id not in self._running:
                                return
                            if self.task_manager:
                                task = self.task_manager.get_task(task_id)
                                if task["retry_count"] < task["max_retries"]:
                                    self.task_manager.transition(task_id, "CLINE_STALLED",
                                                                  trigger="stall_detected")
                                    # Kill stale process tree
                                    pid = self._pids.get(task_id)
                                    if pid:
                                        terminate_process_tree(pid, timeout=3.0)
                                    self._processes.pop(task_id, None)
                                    self._pids.pop(task_id, None)
                                    # Cancel old monitors
                                    for d in [self._running, self._stall_tasks]:
                                        old = d.pop(task_id, None)
                                        if old:
                                            old.cancel()

                                    self.task_manager.transition(task_id, "CLINE_STARTING",
                                                                  trigger="stall_retry")
                                    # Use _spawn_locked to avoid re-acquiring the same asyncio.Lock
                                    await self._spawn_locked(task)
                                else:
                                    self.task_manager.transition(task_id, "CLINE_STALLED",
                                                                  trigger="stall_terminal")
                                return  # exit loop; new monitor started on retry or terminal
            except Exception as e:
                logger.error(f"Stall monitor error {task_id}: {e}")

    async def stop(self, task_id: str):
        """Stop a running Cline process and its children."""
        lock = self._get_lock(task_id)
        async with lock:
            pid = self._pids.get(task_id)
            process = self._processes.get(task_id)

            if pid:
                terminate_process_tree(pid, timeout=3.0)
            elif process:
                try:
                    terminate_process_tree(process.pid, timeout=3.0)
                except Exception as e:
                    logger.error(f"Error stopping Cline {task_id}: {e}")

            self._running.pop(task_id, None)
            self._processes.pop(task_id, None)
            self._pids.pop(task_id, None)
            stall = self._stall_tasks.pop(task_id, None)
            if stall:
                stall.cancel()
                try:
                    await stall
                except asyncio.CancelledError:
                    pass

    async def shutdown(self):
        """Shut down executor. By default does NOT kill running Cline processes.

        The daemon's main loop controls this via config.cline.kill_on_shutdown.
        When False (default), running processes survive daemon restart.
        """
        if self.config.cline.kill_on_shutdown:
            for task_id in list(self._running.keys()):
                await self.stop(task_id)
        else:
            # Cancel monitors but leave processes running
            logger.info("Shutdown: detaching from %d running tasks", len(self._running))
            for task_id in list(self._stall_tasks.keys()):
                stall = self._stall_tasks.pop(task_id, None)
                if stall:
                    stall.cancel()
            self._running.clear()
            self._processes.clear()
            self._pids.clear()
