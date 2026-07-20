"""IPC server: TCP (Windows) or Unix Domain Socket (Linux). Protocol: JSON-Lines. Supports request/response and push."""

import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger(__name__)


class IPCServer:
    """JSON-Lines IPC server over TCP or Unix Socket."""

    def __init__(self, config, task_manager, cline_executor, budget_tracker, safety_guard):
        self.config = config
        self.task_manager = task_manager
        self.cline_executor = cline_executor
        self.budget_tracker = budget_tracker
        self.safety_guard = safety_guard
        self._server = None
        self._clients = set()
        self._req_id = 0

    async def start(self):
        ipc = self.config.ipc
        if ipc.transport == "tcp":
            self._server = await asyncio.start_server(self._handle_client, ipc.tcp_host, ipc.tcp_port)
            logger.info(f"IPC server listening on {ipc.tcp_host}:{ipc.tcp_port}")
        else:
            if os.path.exists(ipc.unix_socket):
                os.unlink(ipc.unix_socket)
            self._server = await asyncio.start_unix_server(self._handle_client, ipc.unix_socket)
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
        msg = {"type": "push", "event": event, "data": data}
        payload = json.dumps(msg, ensure_ascii=False, default=str) + "\n"
        payload_bytes = payload.encode("utf-8")
        dead = set()
        for writer in list(self._clients):
            try:
                writer.write(payload_bytes)
                await writer.drain()
            except Exception:
                dead.add(writer)
        self._clients -= dead

    @property
    def _handlers(self):
        return {
            "submit_task": self._h_submit_task,
            "get_status": self._h_get_status,
            "get_task": self._h_get_task,
            "cancel_task": self._h_cancel_task,
            "approve_task": self._h_approve_task,
            "get_log_tail": self._h_get_log_tail,
            "get_budget_status": self._h_get_budget_status,
            "ping": self._h_ping,
        }

    async def _h_submit_task(self, params):
        prompt = params.get("prompt", "")
        task_type = params.get("task_type", "cline_exec")
        cwd = params.get("cwd", os.getcwd())
        task = self.task_manager.create_task(task_type=task_type, prompt=prompt)
        self.task_manager.db.update_task_field(task["id"], cline_cwd=cwd)
        self.task_manager.transition(task["id"], "CLINE_STARTING", trigger="submit")
        await self.cline_executor.spawn(task)
        self.task_manager.transition(task["id"], "CLINE_RUNNING", trigger="spawned")
        task = self.task_manager.get_task(task["id"])
        await self.push("state_changed", {"task_id": task["id"], "state": "CLINE_RUNNING"})
        return {"task_id": task["id"], "state": task["state"]}

    async def _h_get_status(self, params):
        tasks = self.task_manager.get_all_tasks()
        return {
            "tasks": tasks,
            "active_count": len(self.task_manager.get_active_tasks()),
            "total_count": len(tasks),
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
        await self.cline_executor.spawn(task)
        self.task_manager.transition(task_id, "CLINE_RUNNING", trigger="approved_run")
        return {"task_id": task_id, "state": "CLINE_RUNNING"}

    async def _h_get_log_tail(self, params):
        task_id = params["task_id"]
        lines = params.get("lines", 50)
        task = self.task_manager.get_task(task_id)
        if not task:
            return {"error": "not_found"}
        stdout = task.get("log_stdout", "")
        if stdout and os.path.exists(stdout):
            with open(stdout, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                return {"stdout": "".join(all_lines[-lines:])}
        return {"stdout": ""}

    async def _h_get_budget_status(self, params):
        return self.budget_tracker.get_status()

    async def _h_ping(self, params):
        return {"pong": True, "version": "0.1.0"}
