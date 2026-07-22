"""Build bounded, structured evidence packages for Codex review."""

import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Maximum bytes of stdout/stderr tail to include in review packages
_LOG_TAIL_MAX_BYTES = 10240  # 10 KB per stream

# ANSI escape sequence pattern for stripping
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    return _ANSI_RE.sub('', text)


def _read_bounded_tail(filepath: str, max_bytes: int = _LOG_TAIL_MAX_BYTES) -> str:
    """Read the tail of a log file, bounded to max_bytes, reverse-seek approach."""
    if not filepath or not os.path.exists(filepath):
        return "(no log file)"
    try:
        file_size = os.path.getsize(filepath)
        if file_size == 0:
            return "(empty log file)"
        with open(filepath, "rb") as f:
            if file_size <= max_bytes:
                f.seek(0)
                raw = f.read().decode("utf-8", errors="replace")
            else:
                f.seek(-max_bytes, os.SEEK_END)
                raw = f.read().decode("utf-8", errors="replace")
        return _strip_ansi(raw)
    except OSError as e:
        return f"(error reading log: {e})"


class ResultPackager:
    """Generates review packages from task and training results."""

    def __init__(self, baseline_dir: str = None):
        self.baseline_dir = baseline_dir or os.path.join(
            os.getcwd(), ".agent-hub", "baselines"
        )
        os.makedirs(self.baseline_dir, exist_ok=True)

    async def build_package(self, goal: Dict[str, Any],
                            task: Dict[str, Any],
                            training_results: Dict[str, Any] = None) -> Dict[str, Any]:
        """Build a complete review package for Codex."""

        pkg = {
            "package_version": "1.1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "goal": self._goal_summary(goal),
            "task": self._task_summary(task),
            "training": None,
            "metrics": None,
            "baseline_comparison": None,
            "changed_files": [],
            "test_results": None,
            "cli_output": self._cli_output_tails(task),
            "validation_evidence": self._validation_evidence(task),
            "issues": [],
            "recommendations": [],
        }

        # Structured result from Cline
        struct = task.get("structured_result", "")
        if struct:
            try:
                parsed = json.loads(struct)
                pkg["changed_files"] = parsed.get("changed_files", [])
                pkg["test_results"] = parsed.get("test_results", "")
                pkg["issues"] = parsed.get("unresolved_issues", [])
            except json.JSONDecodeError:
                pass

        # Training results
        if task.get("training_requested"):
            pkg["training"] = self._training_summary(task, training_results)
            pkg["metrics"] = await self._extract_metrics(task)

            # Baseline comparison
            baseline_name = task.get("goal_id", "default")
            baseline = self._load_baseline(baseline_name)
            if baseline and pkg["metrics"]:
                pkg["baseline_comparison"] = self._compare_baseline(
                    pkg["metrics"], baseline
                )

        # Recommendations
        pkg["recommendations"] = self._generate_recommendations(pkg)

        return pkg

    def _goal_summary(self, goal: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": goal.get("id"),
            "objective": goal.get("objective", "")[:500],
            "state": goal.get("state"),
            "iteration": goal.get("iteration_count", 0),
            "max_iterations": goal.get("max_iterations", 10),
            "failures": goal.get("failure_count", 0),
            "model_calls": goal.get("accumulated_model_calls", 0),
        }

    def _task_summary(self, task: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": task.get("id"),
            "prompt": (task.get("prompt") or "")[:300],
            "state": task.get("state"),
            "exit_code": task.get("cline_exit_code"),
            "retry_count": task.get("retry_count", 0),
            "training_requested": bool(task.get("training_requested")),
        }

    def _cli_output_tails(self, task: Dict[str, Any]) -> Dict[str, str]:
        """Return bounded, ANSI-stripped stdout and stderr tails from a task."""
        stdout_path = task.get("log_stdout", "")
        stderr_path = task.get("log_stderr", "")
        return {
            "stdout": _read_bounded_tail(stdout_path),
            "stderr": _read_bounded_tail(stderr_path),
        }

    def _validation_evidence(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Extract validation evidence from task output.

        Scans stdout for markers like PASS/FAIL/test results so the reviewer
        can verify that expected outputs exist.
        """
        stdout_path = task.get("log_stdout", "")
        evidence: Dict[str, Any] = {
            "has_output": False,
            "pass_count": 0,
            "fail_count": 0,
            "error_lines": [],
            "markers_found": [],
            "summary": "",
        }
        if not stdout_path or not os.path.exists(stdout_path):
            return evidence

        try:
            raw = _read_bounded_tail(stdout_path, max_bytes=65536)
            clean = _strip_ansi(raw)
            evidence["has_output"] = bool(clean.strip())

            # Count test pass/fail markers
            pass_pattern = re.compile(r'\bPASS(?:ED)?\b', re.IGNORECASE)
            fail_pattern = re.compile(r'\bFAIL(?:ED|URE)?\b', re.IGNORECASE)
            pytest_pass = re.compile(r'\b(\d+)\s+passed\b', re.IGNORECASE)
            pytest_fail = re.compile(r'\b(\d+)\s+failed\b', re.IGNORECASE)

            evidence["pass_count"] = len(pass_pattern.findall(clean))
            evidence["fail_count"] = len(fail_pattern.findall(clean))

            for line in clean.splitlines():
                m = pytest_pass.search(line)
                if m:
                    evidence["markers_found"].append(
                        {"type": "pytest_passed", "value": int(m.group(1)),
                         "line": line.strip()}
                    )
                m = pytest_fail.search(line)
                if m:
                    evidence["markers_found"].append(
                        {"type": "pytest_failed", "value": int(m.group(1)),
                         "line": line.strip()}
                    )

            for line in clean.splitlines():
                if "error" in line.lower() or "traceback" in line.lower():
                    evidence["error_lines"].append(line.strip()[:200])

            parts = []
            if evidence["pass_count"]:
                parts.append(f"{evidence['pass_count']} PASS mentions")
            if evidence["fail_count"]:
                parts.append(f"{evidence['fail_count']} FAIL mentions")
            if evidence["markers_found"]:
                parts.append(f"{len(evidence['markers_found'])} test-marker matches")
            evidence["summary"] = "; ".join(parts) or "(no test evidence found)"

        except OSError as e:
            evidence["summary"] = f"(error scanning output: {e})"

        return evidence

    def _training_summary(self, task: Dict[str, Any],
                          training_results: Dict[str, Any] = None) -> Dict[str, Any]:
        summary = {
            "state": task.get("training_state"),
            "exit_code": task.get("training_exit_code"),
            "log_path": task.get("training_log"),
            "command": (task.get("training_command") or "")[:200],
        }

        if training_results:
            summary.update({
                "metrics": training_results.get("metrics"),
                "errors": training_results.get("errors", []),
                "summary": training_results.get("summary", ""),
            })

        return summary

    async def _extract_metrics(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Extract training metrics from log files and result files."""
        metrics = {
            "final_loss": None,
            "final_accuracy": None,
            "best_loss": None,
            "best_accuracy": None,
            "total_steps": None,
            "training_time_seconds": None,
            "errors_detected": 0,
            "raw_metrics": {},
        }

        # Parse training log
        log_path = task.get("training_log", "")
        if log_path and os.path.exists(log_path):
            try:
                with open(log_path, "r") as f:
                    content = f.read()

                # Extract loss values
                losses = [float(m) for m in re.findall(r'loss[=:]?\s*(\d+\.?\d*)', content)]
                if losses:
                    metrics["final_loss"] = losses[-1]
                    metrics["best_loss"] = min(losses)

                # Extract accuracy
                accs = [float(m) for m in re.findall(r'acc[=:]?\s*(\d+\.?\d*)', content)]
                if accs:
                    metrics["final_accuracy"] = accs[-1]
                    metrics["best_accuracy"] = max(accs)

                # Extract step count
                steps = re.findall(r'Step (\d+)/(\d+)', content)
                if steps:
                    metrics["total_steps"] = int(steps[-1][1])

                # Count errors
                metrics["errors_detected"] = len(
                    re.findall(r'(error|exception|traceback)', content, re.IGNORECASE)
                )
            except Exception as e:
                logger.warning(f"Failed to parse training log {log_path}: {e}")

        # Check metrics file
        metrics_path = task.get("metrics_path", "")
        if metrics_path and os.path.exists(metrics_path):
            try:
                with open(metrics_path, "r") as f:
                    metrics["raw_metrics"] = json.load(f)
            except Exception:
                pass

        # Check result file for structured output
        output_path = task.get("output_path", "")
        if output_path:
            result_file = os.path.join(output_path, "result.json") if os.path.isdir(output_path) else output_path
            if os.path.isfile(result_file):
                try:
                    with open(result_file, "r") as f:
                        raw = json.load(f)
                    metrics["raw_metrics"].update(raw)
                except Exception:
                    pass

        return metrics

    def _load_baseline(self, name: str) -> Optional[Dict[str, Any]]:
        """Load baseline metrics for comparison."""
        path = os.path.join(self.baseline_dir, f"{name}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def save_baseline(self, name: str, metrics: Dict[str, Any]):
        """Save current metrics as baseline for future comparison."""
        path = os.path.join(self.baseline_dir, f"{name}.json")
        metrics["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w") as f:
            json.dump(metrics, f, indent=2)

    def _compare_baseline(self, current: Dict[str, Any],
                          baseline: Dict[str, Any]) -> Dict[str, Any]:
        """Compare current metrics against baseline."""
        comparison = {"improvements": [], "regressions": [], "unchanged": []}

        for key in ["final_loss", "best_loss", "final_accuracy", "best_accuracy"]:
            cur_val = current.get(key)
            base_val = baseline.get(key)

            if cur_val is None or base_val is None:
                continue

            diff = cur_val - base_val
            item = {
                "metric": key,
                "current": cur_val,
                "baseline": base_val,
                "delta": diff,
            }

            if key.endswith("loss"):
                # Lower is better
                if diff < -0.001:
                    item["verdict"] = "improvement"
                    comparison["improvements"].append(item)
                elif diff > 0.001:
                    item["verdict"] = "regression"
                    comparison["regressions"].append(item)
                else:
                    item["verdict"] = "unchanged"
                    comparison["unchanged"].append(item)
            else:
                # Higher is better
                if diff > 0.001:
                    item["verdict"] = "improvement"
                    comparison["improvements"].append(item)
                elif diff < -0.001:
                    item["verdict"] = "regression"
                    comparison["regressions"].append(item)
                else:
                    item["verdict"] = "unchanged"
                    comparison["unchanged"].append(item)

        comparison["regression_count"] = len(comparison["regressions"])
        comparison["improvement_count"] = len(comparison["improvements"])

        return comparison

    def _generate_recommendations(self, pkg: Dict[str, Any]) -> List[str]:
        """Generate automated recommendations based on results."""
        recs = []

        # Check exit code
        exit_code = pkg.get("task", {}).get("exit_code")
        if exit_code and exit_code != 0:
            recs.append("Cline task failed with non-zero exit code. Review errors.")
            return recs

        # Check training
        training = pkg.get("training")
        if training:
            t_state = training.get("state")
            if t_state == "TRAINING_FAILED":
                recs.append("Training failed. Check training log for errors.")
            elif t_state == "TRAINING_STALLED":
                recs.append("Training stalled. Consider reducing batch size or increasing timeout.")
            elif t_state == "TRAINING_COMPLETED" or t_state == "RESULT_READY":
                recs.append("Training completed. Review metrics before next iteration.")

        # Check baseline comparison
        comparison = pkg.get("baseline_comparison")
        if comparison:
            if comparison.get("regression_count", 0) > 0:
                recs.append(f"Detected {comparison['regression_count']} metric regression(s). Review before proceeding.")
            if comparison.get("improvement_count", 0) > 0:
                recs.append(f"Detected {comparison['improvement_count']} metric improvement(s). Changes look promising.")

        # Check issues
        issues = pkg.get("issues", [])
        if issues:
            recs.append(f"Found {len(issues)} unresolved issues from Cline output.")

        if not recs:
            recs.append("All checks passed. Goal can proceed or complete.")

        return recs

    def generate_review_package_file(self, pkg: Dict[str, Any],
                                      output_dir: str = None) -> str:
        """Write the review package to a file for Codex to consume."""
        output_dir = output_dir or os.path.join(os.getcwd(), ".agent-hub", "review_packages")
        os.makedirs(output_dir, exist_ok=True)

        goal_id = pkg.get("goal", {}).get("id", "unknown")
        filename = f"review_{goal_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w") as f:
            json.dump(pkg, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"Review package written: {filepath}")
        return filepath
