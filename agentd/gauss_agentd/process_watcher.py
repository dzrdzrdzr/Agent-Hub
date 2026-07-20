"""Cross-platform process identity verification and monitoring."""

import os
import sys
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

import psutil

logger = logging.getLogger(__name__)


def verify_process_identity(stored: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify that a running PID still belongs to our task.

    Checks PID, start_time, cmd_hash, cwd, and GAUSS_AGENT_TASK_ID env var.
    Returns (is_ours, reason).
    """
    pid = stored.get("pid")
    if not pid:
        return False, "no_pid_stored"

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False, "pid_not_found"
    except psutil.AccessDenied:
        return False, "access_denied"

    checks = []

    # Check start time
    stored_start = stored.get("start_time")
    if stored_start:
        try:
            actual_start = proc.create_time()
            if abs(actual_start - stored_start) > 2.0:
                checks.append(f"start_time_mismatch: stored={stored_start:.1f} actual={actual_start:.1f}")
        except Exception:
            checks.append("start_time_unreadable")

    # Check command hash
    stored_hash = stored.get("cmd_hash")
    if stored_hash:
        try:
            from .config import cmd_hash
            actual_cmd = proc.cmdline()
            actual_hash = cmd_hash(actual_cmd)
            if actual_hash != stored_hash:
                checks.append(f"cmd_hash_mismatch")
        except Exception:
            checks.append("cmd_unreadable")

    # Check CWD
    stored_cwd = stored.get("cwd")
    if stored_cwd:
        try:
            actual_cwd = proc.cwd()
            if os.path.normpath(actual_cwd) != os.path.normpath(stored_cwd):
                checks.append(f"cwd_mismatch: stored={stored_cwd} actual={actual_cwd}")
        except Exception:
            checks.append("cwd_unreadable")

    # Check GAUSS_AGENT_TASK_ID in environment
    stored_task_id = stored.get("id")
    if stored_task_id:
        try:
            env = proc.environ()
            env_task_id = env.get("GAUSS_AGENT_TASK_ID", "")
            if env_task_id != stored_task_id:
                checks.append(f"task_id_mismatch: expected={stored_task_id} got={env_task_id}")
        except Exception:
            # Can't read env on some platforms
            pass

    if checks:
        return False, "; ".join(checks)
    return True, "identity_verified"


def check_process_alive(pid: int) -> bool:
    """Simple PID alive check."""
    try:
        proc = psutil.Process(pid)
        return proc.is_running()
    except psutil.NoSuchProcess:
        return False


def get_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Capture full process identity for a given PID."""
    try:
        proc = psutil.Process(pid)
        return {
            "pid": pid,
            "start_time": proc.create_time(),
            "cmdline": proc.cmdline(),
            "cwd": proc.cwd(),
            "ppid": proc.ppid(),
            "exe": proc.exe(),
            "status": proc.status(),
        }
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return {"pid": pid, "start_time": 0, "cmdline": [], "cwd": "", "ppid": 0, "exe": "", "status": "denied"}


def check_log_freshness(stdout_path: str, stderr_path: str) -> Tuple[float, float]:
    """Return age in seconds since last output to stdout and stderr."""
    now = datetime.now(timezone.utc).timestamp()
    stdout_age = now - os.path.getmtime(stdout_path) if os.path.exists(stdout_path) else float("inf")
    stderr_age = now - os.path.getmtime(stderr_path) if os.path.exists(stderr_path) else float("inf")
    return stdout_age, stderr_age


def safe_terminate(pid: int, force: bool = False) -> bool:
    """Safely terminate a process after identity verification. Returns success."""
    try:
        proc = psutil.Process(pid)
        if force:
            proc.kill()
        else:
            proc.terminate()
        return True
    except psutil.NoSuchProcess:
        return True  # already dead
    except Exception as e:
        logger.warning(f"Failed to terminate PID {pid}: {e}")
        return False
