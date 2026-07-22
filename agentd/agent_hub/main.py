"""Agent Hub daemon entry point."""

import os
import sys
import asyncio
import logging
import signal

from .config import load_config, resolve_cline_path, get_cline_version
from .db import Database
from .task_manager import TaskManager
from .cline_executor import ClineExecutor, MockClineExecutor
from .server import IPCServer
from .safety_guard import SafetyGuard
from .budget_tracker import BudgetTracker
from .recovery import run_recovery
from .process_watcher import check_process_alive
from .goal_manager import GoalManager
from .event_manager import EventManager
from .training_manager import TrainingManager
from .goal_orchestrator import GoalOrchestrator
from .codex_executor import CodexExecutor, MockCodexExecutor, resolve_codex_path


def setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "agentd.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


async def _watchdog_scan(task_manager, cline_executor, db):
    """Periodic watchdog: reconcile DB active tasks with OS process table.

    Runs every 60s. Detects zombie tasks (CLINE_RUNNING but process gone)
    and tasks that were missed by other paths.
    """
    active_states = {"CLINE_STARTING", "CLINE_RUNNING"}
    all_tasks = db.get_all_tasks()
    for task in all_tasks:
        state = task["state"]
        if state not in active_states:
            continue
        task_id = task["id"]
        pid = task.get("cline_pid")
        if not pid:
            continue

        # Skip tasks already monitored by the executor
        if task_id in cline_executor._running:
            continue

        if not check_process_alive(pid):
            logging.getLogger("agentd").warning(
                f"Watchdog: task {task_id} state={state} but PID {pid} is dead"
            )
            # Use task_manager.transition so retry_count is properly tracked
            try:
                task_manager.transition(task_id, "CLINE_FAILED",
                                         trigger="watchdog_process_gone")
            except ValueError:
                pass
            # Try auto-retry with proper retry_count increment
            t = db.get_task(task_id)
            if t["retry_count"] < t["max_retries"]:
                try:
                    task_manager.transition(task_id, "CLINE_STARTING",
                                             trigger="watchdog_retry")
                except ValueError:
                    pass
                try:
                    await cline_executor.spawn(t)
                    try:
                        task_manager.transition(task_id, "CLINE_RUNNING",
                                                 trigger="watchdog_retry_spawned")
                    except ValueError:
                        pass
                except Exception as e:
                    logging.getLogger("agentd").error(
                        f"Watchdog spawn failed for {task_id}: {e}"
                    )
                    try:
                        task_manager.transition(task_id, "CLINE_FAILED",
                                                 trigger="watchdog_spawn_failed")
                    except ValueError:
                        pass


async def _janitor_scan(db, logs_dir, retention_days=30, max_tasks=1000):
    """Periodic janitor: clean up terminal tasks older than retention_days
    and their log files. Keeps at most max_tasks entries."""
    import time as _time
    import shutil

    logger = logging.getLogger("agentd")
    cutoff = _time.time() - retention_days * 86400

    terminal_states = ["CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED", "CANCELLED"]
    all_tasks = db.get_all_tasks()

    # Count terminal vs active
    terminal_tasks = [t for t in all_tasks if t["state"] in terminal_states]
    active_count = len(all_tasks) - len(terminal_tasks)

    if active_count > max_tasks:
        logger.warning(f"Janitor: {active_count} active tasks exceeds limit {max_tasks}")

    # Clean old terminal tasks
    cleaned = 0
    for task in terminal_tasks:
        created = task.get("created_at", "")
        # Simple ISO parsing
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(created)
            age = _time.time() - dt.timestamp()
        except Exception:
            age = 0

        if age > retention_days * 86400:
            task_id = task["id"]
            # Remove log files
            for field in ["log_stdout", "log_stderr"]:
                log_path = task.get(field, "")
                if log_path and os.path.exists(log_path):
                    try:
                        os.remove(log_path)
                    except OSError:
                        pass
            # Remove prompt file
            prompt_file = os.path.join(logs_dir, "cline",
                                        f"{task_id}.prompt.txt")
            if os.path.exists(prompt_file):
                try:
                    os.remove(prompt_file)
                except OSError:
                    pass
            # Remove DB records
            db.execute("DELETE FROM state_transitions WHERE task_id = ?", (task_id,))
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            db.commit()
            cleaned += 1

    if cleaned > 0:
        logger.info(f"Janitor: cleaned {cleaned} old terminal tasks")


async def main():
    # Load config
    config_path = os.environ.get("AGENT_HUB_CONFIG", "")
    if config_path:
        config = load_config(config_path)
    else:
        config = load_config()

    # Ensure working dir is the project root
    cwd = os.getcwd()
    db_path = os.path.join(cwd, config.database.path)
    logs_dir = os.path.join(cwd, config.logs.dir)

    setup_logging(logs_dir)
    logger = logging.getLogger("agentd")
    logger.info("=" * 60)
    logger.info("Agent Hub daemon starting...")
    logger.info(f"  CWD: {cwd}")
    logger.info(f"  DB: {db_path}")
    logger.info(f"  Logs: {logs_dir}")

    # Resolve Cline path
    try:
        cline_path = resolve_cline_path(config.cline.executable)
        cline_version = get_cline_version(cline_path)
        logger.info(f"  Cline: {cline_path} (version: {cline_version})")
    except FileNotFoundError as e:
        logger.error(f"  Cline: {e}")
        logger.warning("  Cline not found. Mock mode or manual config required.")

    # Initialize components
    db = Database(db_path)
    logger.info("  Database initialized")

    task_manager = TaskManager(db)
    budget_tracker = BudgetTracker(db)
    safety_guard = SafetyGuard(config.safety, workspace_root=cwd)
    if config.cline.mock:
        cline_executor = MockClineExecutor(config, task_manager=task_manager)
        logger.info("  Cline: mock (explicitly enabled)")
    else:
        cline_executor = ClineExecutor(
            config, task_manager=task_manager, safety_guard=safety_guard,
            runtime_root=cwd,
        )
    goal_manager = GoalManager(db)
    event_manager = EventManager(db)
    training_manager = TrainingManager(
        db, task_manager=task_manager,
        event_manager=event_manager,
        stall_threshold=config.cline.stall_threshold_seconds,
        logs_dir=config.logs.dir,
        runtime_root=cwd,
    )

    # Codex executor — mock mode overrides real CLI discovery
    codex_path = resolve_codex_path()
    if config.cline.mock:
        # Mock mode explicitly enabled — always use mock regardless of PATH
        codex_executor = MockCodexExecutor(plan_sequence=[
            {"verdict": "plan_ready",
             "next_task": {"prompt": "mock task: implement feature",
                           "task_type": "cline_exec",
                           "training_expected": False},
             "goal_complete": False,
             "reasoning": "mock plan"},
            {"verdict": "goal_achieved",
             "next_task": None,
             "goal_complete": True,
             "reasoning": "mock: goal achieved after 2 iterations"},
        ])
        logger.info(f"  Codex: mock (cli available: {bool(codex_path)})")
    elif codex_path:
        codex_executor = CodexExecutor(
            codex_path=codex_path,
            timeout=config.cline.timeout_seconds,
            budget_tracker=budget_tracker,
        )
        logger.info(f"  Codex: {codex_path}")
    else:
        codex_executor = None
        logger.error("  Codex: NOT FOUND. Goals requiring planning/review will FAIL. "
                     "Install Codex CLI or set cline.mock=true in config.yaml.")

    # Orchestrator
    orchestrator = GoalOrchestrator(
        db=db, goal_manager=goal_manager, event_manager=event_manager,
        task_manager=task_manager, cline_executor=cline_executor,
        training_manager=training_manager, codex_executor=codex_executor,
        safety_guard=safety_guard,
        event_wait_timeout=config.cline.stall_threshold_seconds,
    )
    logger.info("  Orchestrator initialized")

    # Wire events into task transitions
    _wire_events(task_manager, event_manager, cline_executor)

    # Run recovery (with executor for re-attaching monitors)
    logger.info("Running recovery...")
    recovered = await run_recovery(
        db, task_manager, cline_executor=cline_executor,
        event_manager=event_manager, training_manager=training_manager,
        goal_manager=goal_manager, orchestrator=orchestrator,
    )
    for r in recovered:
        logger.info(f"  Recovery: {r}")

    # Start IPC server
    server = IPCServer(config, task_manager, cline_executor, budget_tracker, safety_guard,
                       goal_manager=goal_manager, event_manager=event_manager,
                       training_manager=training_manager, orchestrator=orchestrator)
    await server.start()
    logger.info(f"  IPC server started ({config.ipc.transport})")

    # Graceful shutdown handler
    loop = asyncio.get_event_loop()
    running = True

    def shutdown():
        nonlocal running
        running = False
        logger.info("Shutdown signal received")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    logger.info("Daemon ready. Waiting for connections...")
    logger.info(f"  kill_on_shutdown: {config.cline.kill_on_shutdown}")
    logger.info("=" * 60)

    # Periodic timers
    watchdog_counter = 0
    janitor_counter = 0

    try:
        while running:
            await asyncio.sleep(1)
            watchdog_counter += 1
            janitor_counter += 1

            # Watchdog: every 60s
            if watchdog_counter >= 60:
                watchdog_counter = 0
                await _watchdog_scan(task_manager, cline_executor, db)

            # Janitor: every 3600s (1 hour)
            if janitor_counter >= 3600:
                janitor_counter = 0
                await _janitor_scan(db, logs_dir,
                                     retention_days=config.cline.retention_days,
                                     max_tasks=config.cline.max_task_history)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        await orchestrator.shutdown()
        await training_manager.shutdown()
        await cline_executor.shutdown()
        await server.stop()
        db.close()
        logger.info("Daemon stopped.")


def _wire_events(task_manager, event_manager, cline_executor):
    """Wire event emission into task state transitions.

    Spawn events are emitted here; classify events are emitted directly
    within ClineExecutor._classify_exit via self._event_manager.
    """
    cline_executor._event_manager = event_manager

    original_spawn = cline_executor.spawn

    async def spawn_with_events(task):
        result = await original_spawn(task)
        event_manager.emit_task_related_event(
            "CLINE_RUNNING", task, {"spawned": True}
        )
        return result

    cline_executor.spawn = spawn_with_events


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
