#!/usr/bin/env python3
"""Mock training script for Agent Hub integration test.

Phase 1 (~60s): Output progress every 5s to demonstrate liveness.
Phase 2: Launch background mock training process, record its PID.
Phase 3: Exit normally, leaving background training running.
"""

import os
import sys
import time
import json
import subprocess
from datetime import datetime, timezone

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".agent-hub", "test_results")
TRAINING_LOG = os.path.join(RESULTS_DIR, "training.log")
STATE_FILE = os.path.join(RESULTS_DIR, "test_state.json")
RESULT_FILE = os.path.join(RESULTS_DIR, "test_result.json")

os.makedirs(RESULTS_DIR, exist_ok=True)


def write_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def run_background_training(duration=180):
    """Simulate a long-running training process.
    Writes periodic output to demonstrate it's alive
    even after the parent (Cline) exits.
    """
    pid = os.getpid()
    total_steps = 6  # one step every 30s for 180s

    with open(TRAINING_LOG, "w") as f:
        f.write(f"[{timestamp()}] Training started. PID={pid}\n")
        f.write(f"[{timestamp()}] Total steps: {total_steps}, step_interval: 30s\n")
        f.flush()

        for step in range(1, total_steps + 1):
            time.sleep(30)
            loss = round(1.0 / (step + 0.1), 4)
            acc = round(0.5 + step * 0.08, 4)
            f.write(f"[{timestamp()}] Step {step}/{total_steps} | loss={loss:.4f} acc={acc:.4f}\n")
            f.flush()

    # Training complete - write result
    result = {
        "status": "training_complete",
        "completed_at": timestamp(),
        "pid": pid,
        "final_loss": 0.1428,
        "final_accuracy": 0.98,
        "total_steps": total_steps,
        "training_log": TRAINING_LOG,
    }
    with open(RESULT_FILE, "w") as f:
        json.dump(result, f, indent=2, default=str)

    with open(TRAINING_LOG, "a") as f:
        f.write(f"[{timestamp()}] Training complete. Result saved to {RESULT_FILE}\n")
        f.flush()


def main():
    print(f"[{timestamp()}] Mock training harness starting...")
    print(f"[{timestamp()}] Results dir: {RESULTS_DIR}")
    print(f"[{timestamp()}] State file: {STATE_FILE}")
    print(f"[{timestamp()}] Training log: {TRAINING_LOG}")

    # ---- Phase 1: Progress output (~60s) ----
    print(f"\n[{timestamp()}] === PHASE 1: Progress output (60s) ===")
    for i in range(1, 13):
        pct = i * 100 / 12
        bar = "#" * i + "-" * (12 - i)
        print(f"[{timestamp()}] Progress: [{bar}] {pct:.0f}% ({i * 5}s elapsed)")
        sys.stdout.flush()
        time.sleep(5)

    # ---- Phase 2: Launch background training ----
    print(f"\n[{timestamp()}] === PHASE 2: Launching background training ===")

    # Fork background training process (survives parent exit)
    child_pid = os.fork()
    if child_pid == 0:
        # Child: run training
        os.setsid()
        run_background_training(duration=180)
        sys.exit(0)

    # Parent: record and continue
    print(f"[{timestamp()}] Background training launched:")
    print(f"   Training PID: {child_pid}")
    print(f"   Training log: {TRAINING_LOG}")
    print(f"   Result path:  {RESULT_FILE}")
    print(f"   State file:   {STATE_FILE}")

    state = {
        "test_started_at": timestamp(),
        "cline_completed_at": None,
        "training_pid": child_pid,
        "training_log": TRAINING_LOG,
        "result_file": RESULT_FILE,
        "state_file": STATE_FILE,
        "status": "training_running",
    }
    write_state(state)

    # ---- Phase 3: Cline exits, training continues ----
    print(f"\n[{timestamp()}] === PHASE 3: Cline exiting, training continues ===")
    print(f"[{timestamp()}] Task complete. Training PID {child_pid} still running in background.")
    print(f"[{timestamp()}] Monitor with: tail -f {TRAINING_LOG}")
    print(f"[{timestamp()}] Check result:  cat {RESULT_FILE}")
    sys.stdout.flush()

    state["cline_completed_at"] = timestamp()
    write_state(state)

    print(f"[{timestamp()}] State file updated: cline_completed_at set.")
    sys.stdout.flush()

    time.sleep(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
