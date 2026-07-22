#!/usr/bin/env python3
"""Agent Hub command-line client.

Talks to the Agent Hub daemon over its JSON-Lines TCP IPC (default 127.0.0.1:19876).
Stdlib only: works with ANY python3, no packages required.

Usage:
  agent-hub.py ping
  agent-hub.py submit "your prompt here" [--cwd DIR]
  agent-hub.py status
  agent-hub.py cancel TASK_ID
  agent-hub.py delete-goal GOAL_ID
  agent-hub.py log TASK_ID [--lines N]
  agent-hub.py goal "objective" [--cwd DIR] [--max-iterations N]
              [--max-failures N] [--completion-criteria S] [--stop-conditions S]
              [--model-call-budget N]

Exit code 0 on success, 1 on error. Output is human-readable; use --json for
machine-readable output (other agents: parse stdout as JSON, one document).
"""

import argparse
import json
import os
import socket
import sys


def rpc(method, params=None, host="127.0.0.1", port=19876, timeout=15):
    req_id = "cli"
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((host, port))
    try:
        s.sendall((json.dumps({"type": "request", "id": req_id,
                               "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while True:
            while b"\n" not in buf:
                chunk = s.recv(1 << 20)
                if not chunk:
                    raise RuntimeError("connection closed by daemon")
                buf += chunk
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            msg = json.loads(line.decode())
            if msg.get("type") == "push":
                continue  # ignore async events in CLI mode
            if msg.get("type") == "response" and msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result")
    finally:
        s.close()


def fmt_task(t):
    return (f"{t['id']}  {t['state']:<16} exit={t.get('cline_exit_code')} "
            f"retries={t.get('retry_count', 0)}/{t.get('max_retries', 1)} "
            f"created={t.get('created_at', '?')}\n    {(t.get('prompt') or '')[:100]}")


def main():
    ap = argparse.ArgumentParser(prog="agent-hub", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19876)
    ap.add_argument("--json", action="store_true", help="print raw JSON result")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping")

    p_submit = sub.add_parser("submit")
    p_submit.add_argument("prompt")
    p_submit.add_argument("--cwd", default=os.getcwd())

    p_goal = sub.add_parser("goal", aliases=["start-goal"])
    p_goal.add_argument("objective")
    p_goal.add_argument("--cwd", default=os.getcwd())
    p_goal.add_argument("--max-iterations", type=int, default=None)
    p_goal.add_argument("--max-failures", type=int, default=None)
    p_goal.add_argument("--completion-criteria", default="")
    p_goal.add_argument("--stop-conditions", default="")
    p_goal.add_argument("--model-call-budget", type=int, default=None)

    sub.add_parser("status")

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("task_id")

    p_delete_goal = sub.add_parser("delete-goal")
    p_delete_goal.add_argument("goal_id")

    p_log = sub.add_parser("log")
    p_log.add_argument("task_id")
    p_log.add_argument("--lines", type=int, default=80)

    args = ap.parse_args()

    try:
        if args.cmd == "ping":
            res = rpc("ping", host=args.host, port=args.port)
        elif args.cmd == "submit":
            params = {"prompt": args.prompt, "cwd": args.cwd}
            res = rpc("submit_task", params, host=args.host, port=args.port, timeout=30)
        elif args.cmd in ("goal", "start-goal"):
            params = {"objective": args.objective, "cwd": args.cwd}
            if args.max_iterations is not None:
                params["max_iterations"] = args.max_iterations
            if args.max_failures is not None:
                params["max_failures"] = args.max_failures
            if args.completion_criteria:
                params["completion_criteria"] = args.completion_criteria
            if args.stop_conditions:
                params["stop_conditions"] = args.stop_conditions
            if args.model_call_budget is not None:
                params["model_call_budget"] = args.model_call_budget
            res = rpc("start_goal", params, host=args.host, port=args.port, timeout=30)
        elif args.cmd == "status":
            res = rpc("get_status", host=args.host, port=args.port)
        elif args.cmd == "cancel":
            res = rpc("cancel_task", {"task_id": args.task_id}, host=args.host, port=args.port)
        elif args.cmd == "delete-goal":
            res = rpc("delete_goal", {"goal_id": args.goal_id}, host=args.host, port=args.port, timeout=30)
        elif args.cmd == "log":
            res = rpc("get_log_tail", {"task_id": args.task_id, "lines": args.lines},
                      host=args.host, port=args.port)
        else:
            ap.error("unknown command")
            return 2
    except (OSError, RuntimeError) as e:
        print(f"agent-hub: error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.cmd == "ping":
        print(f"OK: daemon responding (version {res.get('version', '?')})")
    elif args.cmd == "submit":
        print(f"submitted: {res['task_id']}  state={res['state']}")
    elif args.cmd in ("goal", "start-goal"):
        print(f"goal started: {res['id']}  state={res['state']}")
    elif args.cmd == "status":
        print(f"active={res['active_count']} total={res['total_count']}")
        for t in res.get("tasks", []):
            print(fmt_task(t))
    elif args.cmd == "cancel":
        print(f"cancelled: {res['task_id']}  state={res['state']}")
    elif args.cmd == "delete-goal":
        print(f"deleted: {res['goal_id']}  tasks_removed={res['deleted_task_count']}")
    elif args.cmd == "log":
        sys.stdout.write(res.get("stdout", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
