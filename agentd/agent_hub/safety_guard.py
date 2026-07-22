"""Multi-layer safety checker for Cline commands."""

import os
import re
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional

from .config import SafetyConfig


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class SafetyResult:
    allowed: bool = True
    risk: RiskLevel = RiskLevel.LOW
    checks_failed: List[str] = field(default_factory=list)
    requires_approval: bool = False
    details: str = ""


class SafetyGuard:
    """Validates commands before execution."""

    DESTRUCTIVE_PATTERNS = [
        r"rm\s+-rf",
        r"del\s+/[fq]",
        r"git\s+reset\s+--hard",
        r"git\s+clean\s+-[f]",
        r"git\s+push\s+--force",
        r"git\s+push\s+-f",
        r"format\s+[a-zA-Z]:",
        r"diskpart",
        r"drop\s+table",
    ]

    PROTECTED_PATHS_DEFAULT = [
        "data/",
        "checkpoints/stable/",
        ".agent-hub/",
    ]

    def __init__(self, config: SafetyConfig, workspace_root: str = ""):
        self.config = config
        self.workspace_root = os.path.abspath(workspace_root or os.getcwd())
        self.protected_paths = config.protected_paths or self.PROTECTED_PATHS_DEFAULT
        self.forbidden_commands = config.forbidden_commands or []
        self.allow_network = config.allow_network

    def check(self, command: str, cwd: str = "", env: dict = None,
              workspace_root: str = "") -> SafetyResult:
        """Run all safety checks. Returns SafetyResult.

        Args:
            command: The command string to check.
            cwd: The working directory for the command execution.
            env: Environment variables.
            workspace_root: Explicit per-request workspace boundary.
                           When provided, replaces the default self.workspace_root
                           for all path containment checks.
        """
        result = SafetyResult()
        env = env or {}

        # Determine effective workspace boundary
        effective_root = os.path.realpath(os.path.abspath(
            workspace_root or self.workspace_root
        ))

        # 1. CWD within project
        cwd = os.path.abspath(cwd or effective_root)
        if not self._path_within(cwd, effective_root):
            result.allowed = False
            result.risk = RiskLevel.CRITICAL
            result.checks_failed.append("cwd_outside_project")
            result.details = f"CWD {cwd} is outside workspace {effective_root}"
            return result

        # 2. Check for destructive commands
        for pattern in self.DESTRUCTIVE_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                result.risk = RiskLevel.CRITICAL
                result.allowed = False
                result.checks_failed.append(f"destructive_pattern: {pattern}")
                result.details = f"Command matches destructive pattern: {pattern}"
                return result

        # 3. Check custom forbidden commands
        for forbidden in self.forbidden_commands:
            if forbidden.lower() in command.lower():
                result.risk = RiskLevel.CRITICAL
                result.allowed = False
                result.checks_failed.append(f"forbidden_command: {forbidden}")
                result.details = f"Command matches forbidden: {forbidden}"
                return result

        # 4. Check protected paths in command
        for pp in self.protected_paths:
            if pp in command:
                result.risk = RiskLevel.HIGH
                result.requires_approval = True
                result.checks_failed.append(f"protected_path_referenced: {pp}")

        # 5. Check for path escape via symlink
        if self._detects_symlink_escape(command):
            result.risk = RiskLevel.HIGH
            result.checks_failed.append("symlink_escape_risk")

        # 6. Check redirect targets
        redirect_targets = self._extract_redirects(command)
        for rt in redirect_targets:
            full = self._resolve_path(rt, cwd)
            if full and not self._path_within(full, effective_root):
                result.allowed = False
                result.risk = RiskLevel.CRITICAL
                result.checks_failed.append(f"redirect_outside_workspace: {rt}")

        # 7. Check for operations on main branch
        if re.search(r"git\s+.*main|git\s+.*master", command, re.IGNORECASE):
            result.risk = RiskLevel.HIGH
            result.requires_approval = True
            result.checks_failed.append("branch_protection")

        # 8. Check for network access
        if not self.allow_network:
            if re.search(r"(curl|wget|pip\s+install|npm\s+install)", command, re.IGNORECASE):
                result.risk = RiskLevel.MEDIUM
                result.checks_failed.append("network_access")

        if result.checks_failed:
            result.details = "; ".join(result.checks_failed)

        return result

    def _path_within(self, path: str, parent: str) -> bool:
        """Check if path is within parent directory."""
        try:
            path = os.path.realpath(os.path.abspath(path))
            parent = os.path.realpath(os.path.abspath(parent))
            return os.path.commonpath([path, parent]) == parent
        except ValueError:
            return False

    def _resolve_path(self, path_str: str, cwd: str) -> Optional[str]:
        """Resolve a potentially relative path against cwd."""
        if not path_str:
            return None
        if os.path.isabs(path_str):
            return path_str
        return os.path.join(cwd, path_str)

    def _extract_redirects(self, command: str) -> List[str]:
        """Extract file paths from shell redirect operators."""
        targets = []
        # Match > file, >> file, 2> file
        for m in re.finditer(r"[12]?>>?\s*(\S+)", command):
            targets.append(m.group(1))
        return targets

    def audit_command_log(self, log_path: str,
                          workspace_root: str = "") -> List[str]:
        """Post-hoc audit of executed commands in Cline output log.
        
        Scans the log for dangerous commands that were actually run.
        Returns list of violations found.
        """
        violations = []
        effective_root = os.path.realpath(os.path.abspath(
            workspace_root or self.workspace_root
        ))
        if not os.path.exists(log_path):
            return violations

        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                # Read last 50KB — enough for recent session
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 51200))
                content = f.read()

            for pattern in self.DESTRUCTIVE_PATTERNS:
                for m in re.finditer(pattern, content, re.IGNORECASE):
                    # Extract surrounding context
                    start = max(0, m.start() - 40)
                    end = min(len(content), m.end() + 40)
                    context = content[start:end].replace('\n', ' ').strip()
                    violations.append(
                        f"destructive_pattern:{pattern} at pos {m.start()}: ...{context}..."
                    )

            # Check for writes outside workspace
            for m in re.finditer(r'(?:>|>>)\s*(/[a-zA-Z/].*)', content):
                path_out = m.group(1).strip()
                full = self._resolve_path(path_out, effective_root)
                if full and not self._path_within(full, effective_root):
                    violations.append(f"write_outside_workspace: {path_out}")

            # Check for network operations in non-network mode
            if not self.allow_network:
                net_patterns = [r'curl\s+', r'wget\s+', r'pip\s+install',
                               r'npm\s+install', r'git\s+clone']
                for np in net_patterns:
                    if re.search(np, content, re.IGNORECASE):
                        violations.append(f"network_access_in_offline_mode: {np}")

        except Exception as e:
            violations.append(f"audit_error: {e}")

        return violations

    def _detects_symlink_escape(self, command: str) -> bool:
        """Rough check for symlink-based path escape attempts."""
        return bool(re.search(r"ln\s+-s.*\.\.", command, re.IGNORECASE))
