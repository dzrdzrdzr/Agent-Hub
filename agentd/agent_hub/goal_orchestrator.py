"""Goal orchestrator: autonomous closed-loop research engine.

Implements the full pipeline:
  plan → execute(Cline) → train → review(Codex) → iterate → complete

Persists state at every step. Recovers after restart.
"""

import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from .db import Database
from .goal_manager import GoalManager, TERMINAL_STATES, ACTIVE_STATES
from .event_manager import EventManager, KEY_EVENT_TYPES
from .codex_executor import MockCodexExecutor
from .result_packager import ResultPackager

logger = logging.getLogger(__name__)

ORCHESTRATOR_STEPS = [
    "planning", "executing", "waiting_cline", "training",
    "analyzing", "reviewing", "iterating", "completed"
]


class GoalOrchestrator:
    """Autonomous orchestrator for goal-driven research loops."""

    def __init__(self, db: Database, goal_manager: GoalManager,
                 event_manager: EventManager,
                 task_manager=None, cline_executor=None,
                 training_manager=None, codex_executor=None,
                 max_iterations: int = 10, max_failures: int = 3,
                 event_wait_timeout: int = 60):
        self.db = db
        self.goal_manager = goal_manager
        self.event_manager = event_manager
        self.task_manager = task_manager
        self.cline_executor = cline_executor
        self.training_manager = training_manager
        self.codex_executor = codex_executor
        self.max_iterations = max_iterations
        self.max_failures = max_failures
        self.event_wait_timeout = event_wait_timeout
        self._active_orchestrations = {}  # goal_id -> asyncio.Task
        self._locks = {}  # goal_id -> asyncio.Lock
        self.result_packager = ResultPackager()

    def _get_lock(self, goal_id: str) -> asyncio.Lock:
        if goal_id not in self._locks:
            self._locks[goal_id] = asyncio.Lock()
        return self._locks[goal_id]

    async def start_goal(self, objective: str, completion_criteria: str = "",
                         stop_conditions: str = "",
                         max_iterations: int = None,
                         max_failures: int = None) -> Dict[str, Any]:
        """Start a new goal and begin autonomous execution."""
        goal = self.goal_manager.create_goal(
            objective=objective,
            completion_criteria=completion_criteria,
            stop_conditions=stop_conditions,
            max_iterations=max_iterations or self.max_iterations,
            max_failures=max_failures or self.max_failures,
        )

        self.goal_manager.transition(goal["id"], "GOAL_PLANNING", trigger="orchestrator_start")
        self.event_manager.emit_event("GOAL_CREATED", goal_id=goal["id"])

        # Start the orchestration loop
        self._active_orchestrations[goal["id"]] = asyncio.create_task(
            self._run_loop(goal["id"])
        )

        logger.info(f"Orchestrator started for goal {goal['id']}")
        return goal

    async def _run_loop(self, goal_id: str):
        """Main orchestration loop for a goal."""
        lock = self._get_lock(goal_id)

        closing_event = "GOAL_FAILED"  # default outcome, overridden on success

        try:
            goal = self.goal_manager.get_goal(goal_id)
            if not goal:
                return

            # Set state
            if goal["state"] == "GOAL_PLANNING":
                self.goal_manager.transition(goal_id, "GOAL_EXECUTING",
                                              trigger="orchestrator_loop_start")

            while self.goal_manager.should_continue(goal_id):
                goal = self.goal_manager.get_goal(goal_id)

                # Step 1: Plan (Codex creates the next task)
                await self._step_plan(goal)
                # Check if goal completed during planning
                goal = self.goal_manager.get_goal(goal_id)
                if goal and goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                    if goal["state"] == "GOAL_COMPLETED":
                        closing_event = "GOAL_COMPLETED"
                    break

                # Step 2: Execute (Cline runs the task)
                await self._step_execute(goal_id)

                # Step 3: Wait for Cline completion
                await self._step_wait_cline(goal_id)

                # Step 4: Handle training if requested
                await self._step_training(goal_id)

                # Step 5: Review (Codex reviews results)
                should_continue = await self._step_review(goal_id)
                if not should_continue:
                    goal_post = self.goal_manager.get_goal(goal_id)
                    if goal_post and goal_post["state"] == "GOAL_COMPLETED":
                        closing_event = "GOAL_COMPLETED"
                    break
                # Check goal state after review
                goal = self.goal_manager.get_goal(goal_id)
                if goal and goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
                    if goal["state"] == "GOAL_COMPLETED":
                        closing_event = "GOAL_COMPLETED"
                    break

            # Finalize
            goal = self.goal_manager.get_goal(goal_id)
            if goal and goal["state"] not in TERMINAL_STATES:
                if goal["iteration_count"] >= goal["max_iterations"]:
                    self.goal_manager.fail_goal(goal_id, "max_iterations_reached")
                elif goal["failure_count"] >= goal["max_failures"]:
                    self.goal_manager.fail_goal(goal_id, "max_failures_reached")
                else:
                    self.goal_manager.complete_goal(goal_id)
                    closing_event = "GOAL_COMPLETED"

            self.event_manager.emit_event(closing_event, goal_id=goal_id)

        except asyncio.CancelledError:
            logger.info(f"Orchestrator cancelled for goal {goal_id}")
            self.event_manager.emit_event("GOAL_FAILED", goal_id=goal_id)
        except Exception as e:
            logger.error(f"Orchestrator error for goal {goal_id}: {e}")
            self.goal_manager.fail_goal(goal_id, f"orchestrator_error: {e}")
            self.event_manager.emit_event("GOAL_FAILED", goal_id=goal_id)
        finally:
            self._active_orchestrations.pop(goal_id, None)

    async def _step_plan(self, goal: Dict[str, Any]):
        """Codex generates the next task."""
        goal_id = goal["id"]
        self.goal_manager.increment_iteration(goal_id)
        self.goal_manager.transition(goal_id, "GOAL_PLANNING", trigger="step_plan")

        # Record model call
        call_id = self.db.create_model_call(
            goal_id=goal_id, task_id="",
            model_role="codex_planner",
            reason=f"iteration_{goal['iteration_count']}_planning",
        )
        self.goal_manager.increment_model_calls(goal_id)

        try:
            result = await self.codex_executor.plan(goal)
            self.db.complete_model_call(call_id, "completed",
                                         result.verdict)

            if result.goal_complete:
                self.goal_manager.complete_goal(goal_id)
                return

            next_task = result.next_task or {}
            task_prompt = next_task.get("prompt", goal["objective"])

            # Create task
            if self.task_manager:
                task = self.task_manager.create_task(
                    task_type="cline_exec",
                    prompt=task_prompt,
                    goal_id=goal_id,
                    task_sequence=goal["iteration_count"],
                )
                self.goal_manager.set_current_task(goal_id, task["id"])
                self.goal_manager.set_latest_decision(goal_id, result.parsed)
                logger.info(f"Goal {goal_id}: planned task {task['id']}")

        except Exception as e:
            self.db.complete_model_call(call_id, "failed", str(e)[:200])
            self.goal_manager.increment_failure(goal_id)
            raise

    async def _step_execute(self, goal_id: str):
        """Spawn Cline for the current task."""
        goal = self.goal_manager.get_goal(goal_id)
        task_id = goal.get("current_task_id")
        if not task_id:
            return

        task = self.task_manager.get_task(task_id)
        if not task:
            return

        # Skip if already completed (mock may finish immediately)
        if task["state"] in ("CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED", "CANCELLED"):
            logger.info(f"Goal {goal_id}: task {task_id} already {task['state']}, skip execute")
            return

        # Transition only if still QUEUED
        if task["state"] == "QUEUED":
            try:
                self.task_manager.transition(task_id, "CLINE_STARTING", trigger="orchestrator")
            except ValueError:
                pass

        if self.cline_executor:
            try:
                run_id, info = await self.cline_executor.spawn(task)
                # Task may have completed already (mock). Only transition if not terminal.
                task = self.task_manager.get_task(task_id)
                if task and task["state"] not in ("CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED", "CANCELLED"):
                    try:
                        self.task_manager.transition(task_id, "CLINE_RUNNING",
                                                      trigger="orchestrator_spawned")
                    except ValueError:
                        pass
                logger.info(f"Goal {goal_id}: spawned Cline for task {task_id}, state={task['state'] if task else '?'}")
            except ValueError as e:
                pass  # transition error swallowed
            except Exception as e:
                logger.error(f"Goal {goal_id}: Cline spawn failed: {e}")
                self.goal_manager.increment_failure(goal_id)

    async def _step_wait_cline(self, goal_id: str):
        """Wait for Cline completion event."""
        self.goal_manager.transition(goal_id, "GOAL_WAITING_EVENT",
                                      trigger="waiting_cline")

        # Poll task state first (handles mock/fast completion)
        await asyncio.sleep(0.5)  # brief yield for async completion
        goal = self.goal_manager.get_goal(goal_id)
        task_id = goal.get("current_task_id") if goal else None
        if task_id:
            task = self.task_manager.get_task(task_id)
            if task and task["state"] in ("CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED"):
                event_type = task["state"]
                self.event_manager.emit_event(event_type, goal_id=goal_id, task_id=task_id,
                                               payload={"exit_code": task.get("cline_exit_code")})
                if event_type in ("CLINE_FAILED", "CLINE_STALLED"):
                    self.goal_manager.increment_failure(goal_id)
                logger.info(f"Goal {goal_id}: task already {event_type}, emitted event")
                return

        # Wait for event with configurable timeout, looping until task resolves
        wait_timeout = min(self.event_wait_timeout, 60)  # per-wait cap at 60s
        timeout_seconds = 600
        if self.cline_executor and hasattr(self.cline_executor, 'config'):
            timeout_seconds = getattr(self.cline_executor.config.cline, 'timeout_seconds', 600)
        max_loops = max(1, (timeout_seconds // wait_timeout) + 2)

        for loop_i in range(max_loops):
            event = await self.event_manager.wait_for_event(
                goal_id=goal_id,
                event_types=["CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED",
                             "TRAINING_COMPLETED", "TRAINING_FAILED"],
                timeout=wait_timeout,
            )

            if event:
                # Acknowledge
                self.event_manager.acknowledge(event["event_id"], "orchestrator")

                event_type = event["event_type"]
                if event_type in ("CLINE_FAILED", "CLINE_STALLED"):
                    self.goal_manager.increment_failure(goal_id)
                logger.info(f"Goal {goal_id}: received event {event_type}")
                return

            # Check task state on timeout
            goal = self.goal_manager.get_goal(goal_id)
            task_id = goal.get("current_task_id") if goal else None
            if task_id:
                task = self.task_manager.get_task(task_id)
                if task and task["state"] in ("CLINE_SUCCEEDED", "CLINE_FAILED", "CLINE_STALLED"):
                    event_type = task["state"]
                    self.event_manager.emit_event(event_type, goal_id=goal_id, task_id=task_id)
                    logger.info(f"Goal {goal_id}: task state is {event_type} after timeout "
                                f"(loop {loop_i + 1}/{max_loops})")
                    if event_type in ("CLINE_FAILED", "CLINE_STALLED"):
                        self.goal_manager.increment_failure(goal_id)
                    return
            logger.debug(f"Goal {goal_id}: still waiting for Cline (loop {loop_i + 1}/{max_loops})")

        # Exhausted all wait loops — task truly stuck
        logger.warning(f"Goal {goal_id}: exhausted {max_loops} wait loops for Cline")
        self.goal_manager.increment_failure(goal_id)

    async def _step_training(self, goal_id: str):
        """Handle training if requested by Cline."""
        goal = self.goal_manager.get_goal(goal_id)
        task_id = goal.get("current_task_id")
        if not task_id:
            return

        task = self.task_manager.get_task(task_id)
        if not task or not task.get("training_requested"):
            return

        if not self.training_manager:
            logger.warning(f"Goal {goal_id}: training requested but no training_manager")
            return

        self.goal_manager.transition(goal_id, "GOAL_EXECUTING", trigger="training_start")

        training_cmd = task.get("training_command", "")
        if training_cmd:
            train_task = await self.training_manager.request_training(
                task, training_cmd,
                training_cwd=task.get("training_cwd") or "",
            )
            await self.training_manager.spawn(train_task)
            logger.info(f"Goal {goal_id}: training spawned for task {train_task['id']}")

            # Wait for training completion
            train_event = await self.event_manager.wait_for_event(
                goal_id=goal_id,
                event_types=["TRAINING_COMPLETED", "TRAINING_FAILED",
                             "TRAINING_STALLED", "RESULT_READY"],
                timeout=7200,
            )
            if train_event:
                self.event_manager.acknowledge(train_event["event_id"], "orchestrator")

    async def _step_review(self, goal_id: str) -> bool:
        """Codex reviews results and decides next step."""
        goal = self.goal_manager.get_goal(goal_id)
        if goal["state"] in ("GOAL_COMPLETED", "GOAL_FAILED", "GOAL_CANCELLED"):
            return False

        if not self.codex_executor:
            self.goal_manager.fail_goal(goal_id,
                reason="Codex CLI not available for review. Install Codex or enable cline.mock.")
            return False

        self.goal_manager.transition(goal_id, "GOAL_REVIEWING", trigger="step_review")

        task_id = goal.get("current_task_id")
        task = self.task_manager.get_task(task_id) if task_id else None

        # Collect results
        result_package = await self._build_result_package(goal, task)

        # Record model call
        call_id = self.db.create_model_call(
            goal_id=goal_id, task_id=task_id or "",
            model_role="codex_reviewer",
            reason=f"iteration_{goal['iteration_count']}_review",
        )
        self.goal_manager.increment_model_calls(goal_id)

        try:
            result = await self.codex_executor.review(goal, task, result_package)
            self.db.complete_model_call(call_id, "completed", result.verdict)

            self.event_manager.emit_event(
                "CODEX_REVIEW_REQUIRED",
                goal_id=goal_id, task_id=task_id,
                payload={"verdict": result.verdict}
            )

            if result.goal_complete:
                self.goal_manager.complete_goal(goal_id)
                return False

            if result.verdict in ("approved", "goal_achieved"):
                self.goal_manager.transition(goal_id, "GOAL_ITERATING",
                                              trigger="review_approved")
                return True  # continue loop
            elif result.verdict == "needs_changes":
                next_task = result.next_task
                if next_task and self.task_manager:
                    task_prompt = next_task.get("prompt", "")
                    new_task = self.task_manager.create_task(
                        task_type="cline_exec",
                        prompt=task_prompt,
                        goal_id=goal_id,
                        task_sequence=goal["iteration_count"] + 1,
                    )
                    self.goal_manager.set_current_task(goal_id, new_task["id"])
                self.goal_manager.transition(goal_id, "GOAL_ITERATING",
                                              trigger="review_needs_changes")
                return True
            else:
                # blocked or other — stop
                return False

        except Exception as e:
            self.db.complete_model_call(call_id, "failed", str(e)[:200])
            self.goal_manager.increment_failure(goal_id)
            return True  # try to continue

    async def _build_result_package(self, goal: Dict[str, Any],
                                     task: Dict[str, Any]) -> dict:
        """Build result package using ResultPackager for Codex review."""
        if task:
            pkg = await self.result_packager.build_package(goal, task)
            # Also write to file for Codex CLI consumption
            self.result_packager.generate_review_package_file(pkg)
            return pkg

        # Fallback if no task
        return {
            "goal_id": goal["id"],
            "objective": goal["objective"],
            "iteration": goal["iteration_count"],
            "task_id": None,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }

    async def recover(self):
        """Recover active goals after restart."""
        active_goals = self.goal_manager.get_active_goals()
        for goal in active_goals:
            goal_id = goal["id"]
            if goal_id not in self._active_orchestrations:
                logger.info(f"Recovering orchestrator for goal {goal_id}")
                self._active_orchestrations[goal_id] = asyncio.create_task(
                    self._run_loop(goal_id)
                )

    async def cancel_goal(self, goal_id: str):
        """Cancel a goal."""
        goal = self.goal_manager.get_goal(goal_id)
        if goal:
            self.goal_manager.cancel_goal(goal_id)
        task = self._active_orchestrations.pop(goal_id, None)
        if task:
            task.cancel()

    async def shutdown(self):
        """Cancel all orchestrations."""
        for goal_id, task in list(self._active_orchestrations.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._active_orchestrations.clear()
