#!/bin/bash
# Install Agent Hub daemon on Linux
set -e
AGENT_HOME="${HOME}/.local/share/agent-hub"
echo "Installing agent-hub daemon to ${AGENT_HOME}..."
mkdir -p "${AGENT_HOME}/"{agentd,logs,runtime}
cp -r "$(dirname "$0")/../agentd/agent_hub" "${AGENT_HOME}/agentd/"
cp "$(dirname "$0")/../config.yaml" "${AGENT_HOME}/config.yaml"
# Unix socket config
sed -i 's/transport: tcp/transport: unix/' "${AGENT_HOME}/config.yaml" 2>/dev/null || true
echo "Installation complete."
echo "Next: cp scripts/agent-hubd.service ~/.config/systemd/user/"
echo "      systemctl --user daemon-reload"
echo "      systemctl --user enable --now agent-hubd"
