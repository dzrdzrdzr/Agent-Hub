# Agent Hub daemon package

This directory contains the installable Python daemon and CLI for the
[Agent Hub](https://github.com/dzrdzrdzr/Agent-Hub) project.

From the repository root:

```bash
python -m pip install -e "./agentd[dev]"
agent-hub init --path config.local.yaml
export AGENT_HUB_CONFIG="$PWD/config.local.yaml"
agent-hub-daemon
```

The full setup, safety model, VS Code extension, and architecture are documented
in the repository-level README.
