"""Configuration management and Cline CLI path discovery."""

import os
import sys
import shutil
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

import yaml


@dataclass
class IPCConfig:
    transport: str = "tcp"  # "tcp" or "unix"
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 19876
    unix_socket: str = ""

    def __post_init__(self):
        if not self.unix_socket:
            xdg = os.environ.get("XDG_RUNTIME_DIR", "/run/user/" + str(os.getuid() if hasattr(os, "getuid") else 1000))
            self.unix_socket = os.path.join(xdg, "agent-hub.sock")


@dataclass
class ClineConfig:
    executable: str = "auto"  # "auto" or absolute path
    timeout_seconds: int = 600
    stall_threshold_seconds: int = 300
    max_retries: int = 1
    mock: bool = False
    mock_exit_code: int = 0
    mock_delay_seconds: float = 1.0
    env: Dict[str, str] = field(default_factory=dict)
    kill_on_shutdown: bool = False  # NEW: kill Cline processes on daemon shutdown
    retention_days: int = 30         # NEW: days to keep terminal tasks
    max_task_history: int = 1000     # NEW: max tasks to keep in DB


@dataclass
class DatabaseConfig:
    path: str = ".agent-hub/state.sqlite"


@dataclass
class LogsConfig:
    dir: str = ".agent-hub/logs/"


@dataclass
class SafetyConfig:
    protected_paths: list = field(default_factory=list)
    forbidden_commands: list = field(default_factory=list)
    allow_network: bool = False
    max_execution_time_seconds: int = 3600


@dataclass
class AgentdConfig:
    ipc: IPCConfig = field(default_factory=IPCConfig)
    cline: ClineConfig = field(default_factory=ClineConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)


def load_cline_extension_config() -> dict:
    """Load API key/model from Cline VS Code extension configuration."""
    import json
    cline_dir = os.path.join(os.path.expanduser("~"), ".cline", "data", "settings")
    providers_path = os.path.join(cline_dir, "providers.json")
    if not os.path.exists(providers_path):
        return {}
    try:
        with open(providers_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        providers = data.get("providers", {})
        last_used = data.get("lastUsedProvider", "cline")
        if last_used in providers:
            p = providers[last_used]
            s = p.get("settings", {})
            result = {
                "provider": last_used,
                "model": s.get("model", ""),
                "api_key": s.get("apiKey", ""),
            }
            if result["api_key"]:
                return result
        if "cline" in providers:
            cs = providers["cline"].get("settings", {})
            auth = cs.get("auth", {})
            if auth.get("accessToken"):
                return {
                    "provider": "cline",
                    "model": cs.get("model", ""),
                    "api_key": auth["accessToken"],
                }
        return {}
    except Exception:
        return {}


def _default_config_path() -> str:
    agent_home = os.environ.get("AGENT_HUB_HOME", "")
    if agent_home:
        return os.path.join(agent_home, "config.yaml")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "config.yaml")


def load_config(path: Optional[str] = None) -> AgentdConfig:
    path = path or _default_config_path()
    path = os.path.abspath(path)

    data: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    agentd_data = data.get("agentd", {})

    ipc_data = agentd_data.get("ipc", {})
    ipc = IPCConfig(
        transport=ipc_data.get("transport", "tcp"),
        tcp_host=ipc_data.get("tcp_host", "127.0.0.1"),
        tcp_port=ipc_data.get("tcp_port", 19876),
        unix_socket=ipc_data.get("unix_socket", ""),
    )

    cline_data = agentd_data.get("cline", {})
    cline = ClineConfig(
        executable=cline_data.get("executable", "auto"),
        timeout_seconds=cline_data.get("timeout_seconds", 600),
        stall_threshold_seconds=cline_data.get("stall_threshold_seconds", 300),
        max_retries=cline_data.get("max_retries", 1),
        mock=cline_data.get("mock", False),
        mock_exit_code=cline_data.get("mock_exit_code", 0),
        mock_delay_seconds=cline_data.get("mock_delay_seconds", 1.0),
        env=cline_data.get("env", {}),
        kill_on_shutdown=cline_data.get("kill_on_shutdown", False),
        retention_days=cline_data.get("retention_days", 30),
        max_task_history=cline_data.get("max_task_history", 1000),
    )

    db_data = agentd_data.get("database", {})
    db = DatabaseConfig(path=db_data.get("path", ".agent-hub/state.sqlite"))

    logs_data = agentd_data.get("logs", {})
    logs = LogsConfig(dir=logs_data.get("dir", ".agent-hub/logs/"))

    safety_data = agentd_data.get("safety", {})
    safety = SafetyConfig(
        protected_paths=safety_data.get("protected_paths", []),
        forbidden_commands=safety_data.get("forbidden_commands", []),
        allow_network=safety_data.get("allow_network", False),
        max_execution_time_seconds=safety_data.get("max_execution_time_seconds", 3600),
    )

    return AgentdConfig(ipc=ipc, cline=cline, database=db, logs=logs, safety=safety)


def resolve_cline_path(explicit_path: str = "auto") -> str:
    """Resolve Cline CLI executable path with platform-aware discovery.

    Priority:
    1. Explicit absolute path given by user
    2. CLINE_PATH environment variable
    3. PATH lookup for 'cline'
    4. Platform-specific npm global bin lookup
    """
    if explicit_path and explicit_path != "auto":
        if os.path.isfile(explicit_path):
            return os.path.abspath(explicit_path)
        raise FileNotFoundError(f"Cline executable not found at: {explicit_path}")

    env_path = os.environ.get("CLINE_PATH", "")
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)

    # PATH lookup
    which = shutil.which("cline")
    if which:
        return which

    # The daemon is commonly launched from a Conda/venv interpreter while the
    # extension host has a minimal PATH. Prefer a sibling Cline installation in
    # that same environment before falling back to global npm locations.
    env_bin = os.path.dirname(os.path.realpath(sys.executable))
    env_candidates = [
        os.path.join(env_bin, "cline"),
        os.path.join(env_bin, "cline.cmd"),
    ]
    for candidate in env_candidates:
        if os.path.isfile(candidate):
            return candidate

    # Platform-specific npm global bin
    if sys.platform == "win32":
        npm_bin = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm")
        candidates = env_candidates + [
            os.path.join(npm_bin, "cline.cmd"),
            os.path.join(npm_bin, "cline.ps1"),
            os.path.join(npm_bin, "cline"),
        ]
    else:
        npm_bin = os.path.join(os.path.expanduser("~"), ".npm-global", "bin")
        candidates = env_candidates + [
            os.path.join(npm_bin, "cline"),
            "/usr/local/bin/cline",
        ]

    for c in candidates:
        if os.path.isfile(c):
            return c

    raise FileNotFoundError(
        "Cline CLI not found. Install with: npm install -g cline\n"
        "Or set cline.executable in config.yaml to the absolute path."
    )


def get_cline_version(cline_path: str) -> str:
    """Get installed Cline CLI version."""
    import subprocess
    try:
        result = subprocess.run(
            [cline_path, "--version"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def cmd_hash(command: list) -> str:
    """SHA256 hash of a command list for identity verification."""
    return hashlib.sha256(" ".join(command).encode()).hexdigest()
