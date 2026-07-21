"""Cross-platform process identity verification and monitoring."""

import os
import sys
import logging
import signal
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime, timezone

import psutil

logger = logging.getLogger(__name__)


def verify_process_identity(stored: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify that a running PID still belongs to our task.

    Weighted approach (not all-or-nothing):
    - AGENT_HUB_TASK_ID env match → strongest signal, alone suffices
    - cmdline tail match (last N args) → medium signal
    - start_time (tolerance 2s) → medium signal
    - cwd match → weak signal

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

    # ---- Tier 1: AGENT_HUB_TASK_ID environment variable (STRONGEST) ----
    stored_task_id = stored.get("id")
    if stored_task_id:
        try:
            env = proc.environ()
            env_task_id = env.get("AGENT_HUB_TASK_ID", "")
            if env_task_id == stored_task_id:
                return True, "identity_verified_by_env"
        except Exception:
            pass  # Can't read env on some platforms; fall through

    # ---- Tier 2: cmdline tail match ----
    # Instead of full hash (which breaks on shebang scripts like cline),
    # match the last N args. For cline, the important args are the
    # workspace path (-c <dir>) and flags like --auto-approve.
    stored_cmdline = stored.get("cmdline", [])
    if stored_cmdline:
        try:
            actual_cmdline = proc.cmdline()
            if _cmdline_tail_match(stored_cmdline, actual_cmdline, tail=5):
                return True, "identity_verified_by_cmdline"
        except Exception:
            pass

    # ---- Tier 3: start_time + cwd combined ----
    score = 0
    reasons = []

    stored_start = stored.get("start_time")
    if stored_start:
        try:
            actual_start = proc.create_time()
            if abs(actual_start - stored_start) <= 2.0:
                score += 2
            else:
                reasons.append(f"start_time_mismatch: stored={stored_start:.1f} actual={actual_start:.1f}")
        except Exception:
            reasons.append("start_time_unreadable")

    stored_cwd = stored.get("cwd")
    if stored_cwd:
        try:
            actual_cwd = proc.cwd()
            if os.path.normpath(actual_cwd) == os.path.normpath(stored_cwd):
                score += 1
            else:
                reasons.append(f"cwd_mismatch: stored={stored_cwd} actual={actual_cwd}")
        except Exception:
            reasons.append("cwd_unreadable")

    if score >= 2:
        return True, f"identity_verified_by_score({score})"

    if not reasons:
        reasons.append("no_checks_passed")
    return False, "; ".join(reasons)


def _cmdline_tail_match(stored: List[str], actual: List[str], tail: int = 5) -> bool:
    """Check if the tail of actual cmdline matches the tail of stored cmdline.

    This handles cases where the executable is a shebang script (node wrapper)
    and the actual cmdline looks like ['node', '/path/cline', '-p', '-c', 'dir', ...]
    while stored is ['/path/cline', '-p', '-c', 'dir', ...].
    """
    stored_tail = stored[-tail:] if len(stored) >= tail else stored
    actual_tail = actual[-tail:] if len(actual) >= tail else actual
    if len(stored_tail) != len(actual_tail):
        return False
    return all(a == b for a, b in zip(stored_tail, actual_tail))


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


def terminate_process_tree(pid: int, timeout: float = 5.0) -> bool:
    """Terminate a process and all its children using process group or tree.

    On POSIX: sends SIGTERM to the process group, then SIGKILL after timeout.
    On Windows: uses psutil children tree kill.
    Returns True if all processes are gone.
    """
    import time as _time
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True

    # Collect all children
    children: List[psutil.Process] = []
    try:
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return True
    except Exception as e:
        logger.warning(f"Failed to enumerate children of PID {pid}: {e}")

    targets = [parent] + children

    # Try process group signal first (POSIX only)
    if sys.platform != "win32":
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError, PermissionError):
            pass

    # Then individually terminate
    for proc in targets:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue
        except Exception as e:
            logger.warning(f"Failed to terminate child {proc.pid}: {e}")

    # Wait for graceful exit
    gone, alive = psutil.wait_procs(targets, timeout=timeout)

    # Force kill survivors
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logger.warning(f"Failed to kill child {proc.pid}: {e}")

    # Final check
    gone2, alive2 = psutil.wait_procs(alive, timeout=2)
    if alive2:
        logger.warning(f"Processes still alive after kill: {[p.pid for p in alive2]}")
        return False
    return True
