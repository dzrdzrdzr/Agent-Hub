"""Codex CLI executor for non-interactive planning and review.

Supports auto-discovery, timeout, retry, structured result output.
"""

import os
import sys
import json
import uuid
import shutil
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

CODEX_BINARIES = ["codex", "codex-cli"]


def resolve_codex_path() -> Optional[str]:
    """Auto-discover Codex CLI binary."""
    for name in CODEX_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    # Try npm global
    npm_bin = os.path.join(os.path.expanduser("~"), ".npm-global", "bin")
    for name in CODEX_BINARIES:
        candidate = os.path.join(npm_bin, name)
        if os.path.isfile(candidate):
            return candidate
    return None


class CodexResult:
    """Structured result from Codex execution."""
    def __init__(self, raw_output: str, exit_code: int):
        self.raw_output = raw_output
        self.exit_code = exit_code
        self._parsed = None

    @property
    def parsed(self) -> dict:
        if self._parsed is None:
            self._parsed = self._parse_output(self.raw_output)
        return self._parsed

    def _parse_output(self, raw: str) -> dict:
        """Try to extract JSON from Codex output."""
        # Find JSON block in output
        import re
        # Try fenced code block
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # Try raw JSON
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        return {"raw": raw}

    @property
    def verdict(self) -> str:
        return self.parsed.get("verdict", "unknown")

    @property
    def next_task(self) -> Optional[dict]:
        return self.parsed.get("next_task")

    @property
    def goal_complete(self) -> bool:
        return self.parsed.get("goal_complete", False)

    @property
    def blocking_issues(self) -> list:
        return self.parsed.get("blocking_issues", [])

    @property
    def required_actions(self) -> list:
        return self.parsed.get("required_actions", [])


class CodexExecutor:
    """Non-interactive Codex CLI executor for planning and review."""

    def __init__(self, codex_path: str = None, timeout: int = 300,
                 max_retries: int = 1, budget_tracker=None):
        self.codex_path = codex_path or resolve_codex_path()
        self.timeout = timeout
        self.max_retries = max_retries
        self.budget_tracker = budget_tracker

    @property
    def available(self) -> bool:
        return self.codex_path is not None and os.path.isfile(self.codex_path)

    async def run(self, prompt: str, cwd: str = None,
                  input_file: str = None,
                  env: dict = None) -> CodexResult:
        """Run Codex non-interactively."""
        if not self.available:
            return CodexResult(
                json.dumps({"verdict": "error", "error": "codex_not_found"}),
                -1
            )

        cwd = cwd or os.getcwd()
        env = env or os.environ.copy()

        for attempt in range(self.max_retries + 1):
            try:
                result = await self._run_once(prompt, cwd, input_file, env)
                if result.exit_code == 0:
                    return result
                if attempt < self.max_retries:
                    logger.warning(f"Codex retry {attempt + 1}/{self.max_retries}")
                    await asyncio.sleep(2)
                else:
                    return result
            except asyncio.TimeoutError:
                if attempt < self.max_retries:
                    logger.warning(f"Codex timeout, retry {attempt + 1}/{self.max_retries}")
                else:
                    return CodexResult("", -2)

        return CodexResult("", -2)

    async def _run_once(self, prompt: str, cwd: str, input_file: str = None,
                        env: dict = None) -> CodexResult:
        """Single Codex execution attempt."""
        cmd = [self.codex_path, "exec", prompt]

        logger.info(f"Codex: {' '.join(cmd[:3])}... (cwd={cwd})")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )

            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                err_str = stderr.decode("utf-8", errors="replace")
                if err_str.strip():
                    logger.debug(f"Codex stderr: {err_str[:200]}")

            return CodexResult(output, process.returncode)
        except asyncio.TimeoutError:
            # Kill subprocess on timeout — don't leave orphaned processes
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            raise

    async def plan(self, goal: Dict[str, Any], cwd: str = None,
                   env: dict = None) -> CodexResult:
        """Generate execution plan for a goal."""
        prompt = self._build_plan_prompt(goal)
        return await self.run(prompt, cwd=cwd, env=env)

    async def review(self, goal: Dict[str, Any], task: Dict[str, Any],
                     result_package: dict = None,
                     cwd: str = None, env: dict = None) -> CodexResult:
        """Review completed task results."""
        prompt = self._build_review_prompt(goal, task, result_package)
        return await self.run(prompt, cwd=cwd, env=env)

    def _build_plan_prompt(self, goal: Dict[str, Any]) -> str:
        return (
            f"You are an AI research planner. Your goal:\n\n{goal['objective']}\n\n"
            f"Completion criteria: {goal.get('completion_criteria', 'Not specified')}\n"
            f"Stop conditions: {goal.get('stop_conditions', 'Not specified')}\n"
            f"Iteration: {goal.get('iteration_count', 0)}/{goal.get('max_iterations', 10)}\n\n"
            f"Output a JSON block with:\n"
            f'{{"verdict": "plan_ready", "next_task": {{"prompt": "...", '
            f'"task_type": "cline_exec", "training_expected": false, '
            f'"validation": "..."}}, '
            f'"goal_complete": false, "reasoning": "..."}}\n\n'
            f"If the goal is already achieved, set goal_complete: true and verdict: \"goal_achieved\"."
        )

    def _build_review_prompt(self, goal: Dict[str, Any], task: Dict[str, Any],
                             result_package: dict = None) -> str:
        result_str = json.dumps(result_package, indent=2) if result_package else "No results"
        return (
            f"You are an AI code reviewer. Review the following task result.\n\n"
            f"Goal: {goal.get('objective', 'Unknown')[:500]}\n"
            f"Task: {task.get('prompt', 'Unknown')[:300]}\n"
            f"Task state: {task.get('state', 'Unknown')}\n"
            f"Exit code: {task.get('cline_exit_code', 'N/A')}\n\n"
            f"Results:\n{result_str}\n\n"
            f"Output a JSON block with:\n"
            f'{{"verdict": "approved|needs_changes|blocked|goal_achieved", '
            f'"confirmed_findings": [], "blocking_issues": [], '
            f'"required_actions": [], "next_task": {{"prompt": "...", '
            f'"training_expected": false, "validation": "..."}}, '
            f'"architecture_change_allowed": true, '
            f'"merge_allowed": true, '
            f'"goal_complete": false, "reasoning": "..."}}'
        )


class MockCodexExecutor:
    """Mock Codex executor for testing without real Codex CLI."""

    def __init__(self, plan_sequence: list = None):
        """
        Args:
            plan_sequence: list of dicts with 'verdict', 'goal_complete', 'next_task'
        """
        self.plan_sequence = plan_sequence or []
        self._call_count = 0
        self.available = True

    async def run(self, prompt: str, cwd: str = None,
                  input_file: str = None, env: dict = None) -> CodexResult:
        idx = min(self._call_count, len(self.plan_sequence) - 1)
        if idx < len(self.plan_sequence):
            result = self.plan_sequence[idx]
        else:
            result = {"verdict": "goal_achieved", "goal_complete": True,
                      "reasoning": "no more plans"}
        self._call_count += 1
        return CodexResult(json.dumps(result), 0)

    async def plan(self, goal: Dict[str, Any], cwd: str = None,
                   env: dict = None) -> CodexResult:
        return await self.run("plan", cwd=cwd, env=env)

    async def review(self, goal: Dict[str, Any], task: Dict[str, Any],
                     result_package: dict = None,
                     cwd: str = None, env: dict = None) -> CodexResult:
        return await self.run("review", cwd=cwd, env=env)
