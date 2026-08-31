#!/usr/bin/env python3
"""Compatibility wrapper for the installable ``agent-hub`` CLI.

The module-level ``rpc`` name is intentionally retained because existing tests
and integrations replace it when embedding this script directly.
"""

from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "agentd"))

from agent_hub import cli as _cli  # noqa: E402


rpc = _cli.rpc


def main(argv=None):
    """Run the packaged CLI while preserving script-level RPC injection."""

    _cli.rpc = rpc
    return _cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
