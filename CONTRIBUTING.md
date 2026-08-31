# Contributing

Agent Hub manages real local processes and persistent task state. Changes should be small, testable, and explicit about lifecycle behavior.

## Development setup

```bash
./scripts/bootstrap.sh
source .venv/bin/activate
export AGENT_HUB_CONFIG="$PWD/config.test.yaml"
pytest agentd/tests -q
```

For the extension:

```bash
cd extension
npm ci
npm run compile
```

## Pull requests

Include:

- the failure mode or user workflow being changed;
- affected task or goal states;
- process-lifecycle implications;
- tests that reproduce the old behavior and validate the new behavior;
- confirmation that no credentials, local paths, logs, checkpoints, or generated state are committed.

Do not weaken safety checks, retry bounds, process identity checks, or state-transition validation solely to make a test pass.

## Commit scope

Keep daemon, extension, documentation, and generated artifacts separate when possible. Do not commit `.agent-hub/`, `config.local.yaml`, `.env`, VSIX files, build output, or model/project-specific inspection data.
