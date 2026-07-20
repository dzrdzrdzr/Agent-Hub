# GAUSS Agent Control Center - Agent Instructions

## Project Structure
```
D:/tpc/Codex_Cline/
  agentd/               Python daemon (gauss-agentd)
  extension/             VS Code extension (TypeScript)
  docs/                  Documentation
  scripts/               Install/deploy scripts
  config.yaml            Daemon configuration
  DESIGN.md              System design document
  .agent-control/        Runtime data (gitignored)
```

## Build Commands
```bash
# Python daemon
pip install -e agentd/
python -m agentd.gauss_agentd.main

# VS Code extension
cd extension/ && npm install && npm run compile && npm run package

# Tests
cd agentd/ && python -m pytest tests/ -v -p no:dash
```

## Coding Rules
- No hardcoded paths (use config or auto-discovery)
- All subprocess calls use asyncio (non-blocking)
- Process ownership verified before any kill
- Zero model calls for mechanical checks (PID, exit code, log polling)
- Status writes use atomic SQLite transactions
- All state transitions logged with trigger + timestamp
- APIs keys NEVER committed to repo

## Forbidden Operations
- git reset --hard
- rm -rf (outside workspace)
- pip install without user approval
- Auto-push to remote
- Kill processes not owned by GAUSS
- Write outside workspace directory

## Definition of Done
- All Python tests pass (6/6 minimum)
- Daemon starts and listens on IPC
- VS Code extension compiles without errors
- VSIX package generated
- End-to-end IPC ping works
- Mock mode demo works
- Linux deploy files present
