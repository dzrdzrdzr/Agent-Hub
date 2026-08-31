#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
VENV="${VENV:-$ROOT/.venv}"

"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e "$ROOT/agentd[dev]"

if [[ ! -f "$ROOT/config.local.yaml" ]]; then
  cp "$ROOT/config.yaml" "$ROOT/config.local.yaml"
fi

cat <<EOF
Agent Hub development environment is ready.

Activate:
  source "$VENV/bin/activate"

Use local configuration:
  export AGENT_HUB_CONFIG="$ROOT/config.local.yaml"

Start daemon:
  agent-hub-daemon

Check it from another terminal:
  agent-hub ping
EOF
