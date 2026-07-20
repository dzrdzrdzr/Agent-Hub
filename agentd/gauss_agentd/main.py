"""GAUSS Agent Control Center daemon entry point."""

import os
import sys
import asyncio
import logging
import signal

from .config import load_config, resolve_cline_path, get_cline_version
from .db import Database
from .task_manager import TaskManager
from .cline_executor import ClineExecutor
from .server import IPCServer
from .safety_guard import SafetyGuard
from .budget_tracker import BudgetTracker
from .recovery import run_recovery


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


async def main():
    # Load config
    config_path = os.environ.get("GAUSS_AGENT_CONFIG", "")
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
    logger.info("GAUSS Agent Control Center starting...")
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
    cline_executor = ClineExecutor(config, task_manager=task_manager)

    # Run recovery
    logger.info("Running recovery...")
    recovered = await run_recovery(db, task_manager)
    for r in recovered:
        logger.info(f"  Recovery: {r}")

    # Start IPC server
    server = IPCServer(config, task_manager, cline_executor, budget_tracker, safety_guard)
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
    logger.info("=" * 60)

    try:
        while running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        await cline_executor.shutdown()
        await server.stop()
        db.close()
        logger.info("Daemon stopped.")


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
