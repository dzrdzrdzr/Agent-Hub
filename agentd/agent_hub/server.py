"""IPC server: TCP JSON-Lines. Supports request/response, push, and wait_for_event."""

import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class IPCServer:
    """JSON-Lines IPC server over TCP with blocking wait_for_event support."""

    def __init__(self, config, task_manager, cline_executor, budget_tracker, safety_guard,
                 goal_manager=None, event_manager=None, training_manager=None,
                 orchestrator=None):
        self.config = config
        self.task_manager = task_manager
        self.cline_executor = cline_executor
        self.budget_tracker = budget_tracker
        self.safety_guard = safety_guard
        self.goal_manager = goal_manager
        self.event_manager = event_manager
        self.training_manager = training_manager
        self.orchestrator = orchestrator
        self._server = None
        self._clients = set()

    async def start(self):
        ipc = self.config.ipc
        if ipc.transport == "tcp":
            self._server = await asyncio.start_server(
                self._handle_client, ipc.tcp_host, ipc.tcp_port)
            logger.info(f"IPC server listening on {ipc.tcp_host}:{ipc.tcp_port}")
        else:
            if os.path.exists(ipc.unix_socket):
                os.unlink(ipc.unix_socket)
            self._server = await asyncio.start_unix_server(
                self._handle_client, ipc.unix_socket)
            logger.info(f"IPC server listening on {ipc.unix_socket}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()

    async def _handle_client(self, reader, writer):
        self._clients.add(writer)
        addr = writer.get_extra_info("peername")
        logger.info(f"Client connected: {addr}")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    await self._send_error(writer, None, "invalid_json")
                    continue
                await self._dispatch(writer, msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Client error: {e}")
        finally:
            self._clients.discard(writer)
            writer.close()
            logger.info(f"Client disconnected: {addr}")

    async def _dispatch(self, writer, msg):
        if msg.get("type") != "request":
            return
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params", {})
        handler = self._handlers.get(method)
        if not handler:
            await self._send_error(writer, req_id, f"unknown_method: {method}")
            return
        try:
            result = await handler(params)
            await self._send_response(writer, req_id, result)
        except Exception as e:
            logger.error(f"Handler error {method}: {e}")
            await self._send_error(writer, req_id, str(e))

    async def _send_response(self, writer, req_id, result):
        resp = {"type": "response", "id": req_id, "result": result}
        payload = json.dumps(resp, ensure_ascii=False, default=str) + "\n"
        writer.write(payload.encode("utf-8"))
        await writer.drain()

    async def _send_error(self, writer, req_id, error_msg):
        resp = {"type": "response", "id": req_id, "error": error_msg}
        payload = json.dumps(resp, ensure_ascii=False) + "\n"
        writer.write(payload.encode("utf-8"))
        await writer.drain()

    async def push(self, event, data):
        """Push event to all connected clients concurrently."""
        msg = {"type": "push", "event": event, "data": data}
        payload = json.dumps(msg, ensure_ascii=False, default=str) + "\n"
        payload_bytes = payload.encode("utf-8")
        dead = set()

        async def _push_one(writer):
            try:
                writer.write(payload_bytes)
                await asyncio.wait_for(writer.drain(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                dead.add(writer)

        tasks = [_push_one(w) for w in list(self._clients)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._clients -= dead
        for w in dead:
            try:
                w.close()
            except Exception:
                pass

    @property
    def _handlers(self):
        handlers = {
            "ping": self._h_ping,
            "submit_task": self._h_submit_task,
            "get_status": self._h_get_status,
            "get_task": self._h_get_task,
            "cancel_task": self._h_cancel_task,
            "approve_task": self._h_approve_task,
            "get_log_tail": self._h_get_log_tail,
            "get_budget_status": self._h_get_budget_status,
        }
        if self.goal_manager:
            handlers.update({
                "create_goal": self._h_create_goal,
                "get_goal": self._h_get_goal,
                "list_goals": self._h_list_goals,
                "start_goal": self._h_start_goal,
                "cancel_goal": self._h_cancel_goal,
                "wait_for_event": self._h_wait_for_event,
                "acknowledge_event": self._h_acknowledge_event,
                "get_events": self._h_get_events,
            })
        return handlers

    # ---- Core handlers ----

    async def _h_submit_task(self, params):
        prompt = params["prompt"]
        cwd = params.get("cwd", os.getcwd())
        task_type = params.get("task_type", "cline_exec")
        goal_id = params.get("goal_id")
        parent_task_id = params.get("parent_task_id")
        task_sequence = params.get("task_sequence", 0)

        task = self.task_manager.create_task(
            task_type=task_type, prompt=prompt,
            goal_id=goal_id, parent_task_id=parent_task_id,
            task_sequence=task_sequence,
        )

        # Safety check
        safety_result = self.safety_guard.check(prompt, cwd)
        if not safety_result.allowed:
            self.task_manager.transition(task["id"], "CANCELLED",
                                          trigger="safety_blocked")
            await self.push("state_changed", {"task_id": task["id"], "state": "CANCELLED"})
            return {
                "task_id": task["id"],
                "state": "CANCELLED",
                "rejected": True,
                "reason": safety_result.details,
            }

        if safety_result.requires_approval:
            self.task_manager.transition(task["id"], "WAITING_APPROVAL",
                                          trigger="safety_approval_required")
            logger.warning(f"Task {task['id']} requires approval: {safety_result.details}")
            await self.push("state_changed",
                            {"task_id": task["id"], "state": "WAITING_APPROVAL"})
            return {
                "task_id": task["id"],
                "state": "WAITING_APPROVAL",
                "requires_approval": True,
                "reason": safety_result.details,
                "risk": safety_result.risk.value,
            }

        self.task_manager.transition(task["id"], "CLINE_STARTING", trigger="submit")

        try:
            await self.cline_executor.spawn(task)
        except Exception as e:
            logger.error(f"Spawn failed: {e}")
            self.task_manager.transition(task["id"], "CLINE_FAILED",
                                          trigger="spawn_exception")
            await self.push("state_changed",
                            {"task_id": task["id"], "state": "CLINE_FAILED"})
            return {"task_id": task["id"], "state": "CLINE_FAILED", "error": str(e)}

        # Only transition to CLINE_RUNNING if spawn didn't already advance state
        # (mock executor may have already completed the task synchronously)
        current = self.task_manager.get_task(task["id"])
        if current and current["state"] == "CLINE_STARTING":
            self.task_manager.transition(task["id"], "CLINE_RUNNING", trigger="spawned")
        await self.push("state_changed", {"task_id": task["id"], "state": current["state"] if current else task["state"]})
        return {"task_id": task["id"], "state": current["state"] if current else task["state"]}

    async def _h_get_status(self, params):
        limit = params.get("limit", 200)
        offset = params.get("offset", 0)
        active_only = params.get("active_only", False)

        if active_only:
            tasks = self.task_manager.get_active_tasks_sql(limit=limit, offset=offset)
            total_count = self.task_manager.db.fetch_one(
                "SELECT COUNT(*) as cnt FROM tasks WHERE state IN (?,?,?,?)",
                ("QUEUED", "CLINE_STARTING", "CLINE_RUNNING", "WAITING_APPROVAL")
            )["cnt"]
        else:
            tasks = self.task_manager.get_all_tasks_sql(limit=limit, offset=offset)
            total_count = self.task_manager.db.fetch_one(
                "SELECT COUNT(*) as cnt FROM tasks", ()
            )["cnt"]

        return {
            "tasks": tasks,
            "active_count": self.task_manager.get_active_count(),
            "total_count": total_count,
            "budget": self.budget_tracker.get_status(),
        }

    async def _h_get_task(self, params):
        task = self.task_manager.get_task(params["task_id"])
        return task if task else {"error": "not_found"}

    async def _h_cancel_task(self, params):
        task_id = params["task_id"]
        await self.cline_executor.stop(task_id)
        self.task_manager.cancel_task(task_id)
        await self.push("state_changed", {"task_id": task_id, "state": "CANCELLED"})
        return {"task_id": task_id, "state": "CANCELLED"}

    async def _h_approve_task(self, params):
        task_id = params["task_id"]
        task = self.task_manager.approve_task(task_id)
        try:
            await self.cline_executor.spawn(task)
        except Exception as e:
            current = self.task_manager.get_task(task_id)
            if current and current["state"] not in ("CLINE_SUCCEEDED", "CLINE_FAILED",
                                                      "CLINE_STALLED", "CANCELLED"):
                self.task_manager.transition(task_id, "CLINE_FAILED",
                                              trigger="approve_spawn_exception")
            return {"task_id": task_id, "state": "CLINE_FAILED", "error": str(e)}
        task = self.task_manager.get_task(task_id)
        if task and task["state"] in ("CLINE_STARTING",):
            self.task_manager.transition(task_id, "CLINE_RUNNING", trigger="approved_run")
        return {"task_id": task_id, "state": task["state"]}

    async def _h_get_log_tail(self, params):
        task_id = params["task_id"]
        lines = params.get("lines", 50)
        task = self.task_manager.get_task(task_id)
        if not task:
            return {"error": "not_found"}
        stdout = task.get("log_stdout", "")
        if not stdout or not os.path.exists(stdout):
            return {"stdout": ""}
        try:
            tail = _read_tail_lines(stdout, lines)
            return {"stdout": tail}
        except Exception as e:
            return {"stdout": "", "error": str(e)}

    async def _h_get_budget_status(self, params):
        return self.budget_tracker.get_status()

    async def _h_ping(self, params):
        return {
            "pong": True,
            "version": "0.3.0",
            "workspace": os.getcwd(),
            "features": {
                "goals": self.goal_manager is not None,
                "events": self.event_manager is not None,
                "training": self.training_manager is not None,
                "orchestrator": self.orchestrator is not None,
            }
        }

    # ---- Goal handlers ----

    async def _h_create_goal(self, params):
        objective = params["objective"]
        goal = self.goal_manager.create_goal(
            objective=objective,
            completion_criteria=params.get("completion_criteria", ""),
            stop_conditions=params.get("stop_conditions", ""),
            max_iterations=params.get("max_iterations", 10),
            max_failures=params.get("max_failures", 3),
            model_call_budget=params.get("model_call_budget", 100),
        )
        return goal

    async def _h_get_goal(self, params):
        goal = self.goal_manager.get_goal(params["goal_id"])
        if not goal:
            return {"error": "not_found"}
        # Include related tasks
        tasks = self.task_manager.db.get_tasks_by_goal(params["goal_id"])
        goal["tasks"] = tasks
        return goal

    async def _h_list_goals(self, params):
        include_terminal = params.get("include_terminal", False)
        if include_terminal:
            goals = self.goal_manager.db.get_all_goals()
        else:
            goals = self.goal_manager.get_active_goals()
        return {"goals": goals}

    async def _h_start_goal(self, params):
        objective = params["objective"]
        if not self.orchestrator:
            return {"error": "orchestrator_not_available"}
        goal = await self.orchestrator.start_goal(
            objective=objective,
            completion_criteria=params.get("completion_criteria", ""),
            stop_conditions=params.get("stop_conditions", ""),
        )
        return goal

    async def _h_cancel_goal(self, params):
        goal_id = params["goal_id"]
        if self.orchestrator:
            await self.orchestrator.cancel_goal(goal_id)
        else:
            self.goal_manager.cancel_goal(goal_id)
        goal = self.goal_manager.get_goal(goal_id)
        return goal

    async def _h_wait_for_event(self, params):
        """Block until a key event arrives. This is the critical API for
        Codex to wait without polling."""
        if not self.event_manager:
            return {"error": "event_manager_not_available"}

        goal_id = params.get("goal_id")
        task_id = params.get("task_id")
        event_types = params.get("event_types")
        after_version = params.get("after_version", 0)
        timeout = params.get("timeout")  # seconds, None = indefinite

        event = await self.event_manager.wait_for_event(
            goal_id=goal_id,
            task_id=task_id,
            event_types=event_types,
            after_version=after_version,
            timeout=timeout,
        )

        if event is None:
            return {"event": None, "timeout": True}

        return {"event": event, "timeout": False}

    async def _h_acknowledge_event(self, params):
        event_id = params["event_id"]
        handled_by = params.get("handled_by", "codex")
        ok = self.event_manager.acknowledge(event_id, handled_by)
        return {"acknowledged": ok}

    async def _h_get_events(self, params):
        goal_id = params.get("goal_id")
        task_id = params.get("task_id")
        event_types = params.get("event_types")
        limit = params.get("limit", 50)
        events = self.event_manager.get_unacknowledged(
            goal_id=goal_id, task_id=task_id, event_types=event_types, limit=limit
        )
        return {"events": events}


def _read_tail_lines(filepath: str, n: int) -> str:
    """Read last n lines efficiently using reverse seek."""
    block_size = 8192
    try:
        file_size = os.path.getsize(filepath)
    except OSError:
        return ""
    if file_size == 0:
        return ""
    with open(filepath, "rb") as f:
        if file_size <= block_size * 2:
            f.seek(0)
            lines = f.read().decode("utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
        f.seek(0, os.SEEK_END)
        lines_found = 0
        buffer = b""
        pos = file_size
        while pos > 0 and lines_found <= n:
            read_size = min(block_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            buffer = chunk + buffer
            lines_found = buffer.count(b"\n")
        all_lines = buffer.decode("utf-8", errors="replace").splitlines()
        return "\n".join(all_lines[-n:])
