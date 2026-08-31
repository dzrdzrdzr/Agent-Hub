#!/usr/bin/env python3
"""Compatibility wrapper for the installable ``agent-hub`` CLI."""

from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "agentd"))

from agent_hub.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
