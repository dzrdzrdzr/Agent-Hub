# Test Evidence

## 2026-07-20 - Unit Tests (Python)
```
Command: cd D:/tpc/Codex_Cline && python -m pytest agentd/tests/ -v -p no:dash
Exit: 0
6 passed, 0 failed in 0.37s

agentd/tests/test_config.py::test_load_default_config PASSED
agentd/tests/test_config.py::test_resolve_cline_path_auto PASSED
agentd/tests/test_config.py::test_cmd_hash PASSED
agentd/tests/test_db.py::test_db_create_task PASSED
agentd/tests/test_task_manager.py::test_transitions PASSED
agentd/tests/test_task_manager.py::test_task_lifecycle PASSED
```

## 2026-07-20 - IPC Ping Test
```
Command: python -c "..." connect to 127.0.0.1:19876
Response: {"type":"response","id":"1","result":{"pong":true,"version":"0.1.0"}}
```

## 2026-07-20 - Daemon Startup
```
- CWD: D:/tpc/Codex_Cline
- DB: .agent-control/state.sqlite
- Cline: C:/Users/Administrator/AppData/Roaming/npm/cline.CMD (v3.0.46)
- IPC: 127.0.0.1:19876 (tcp)
- Recovery: 0 tasks recovered
```

## 2026-07-20 - Cline CLI Verification
```
Command: cline --version
Output: 3.0.46
Exit: 0

Command: cline --help
Output: Full help text (29 options/commands)
Exit: 0

Command: cline doctor
Output: cli version 3.0.46, hub not running (expected)
Exit: 0

Read-only smoke test: PASSED (listed directory, read DESIGN.md, no file modifications)
```
