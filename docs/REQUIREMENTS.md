# GAUSS Agent Control Center - Requirements

## Core Objective
Build a control center for managing Codex, Cline CLI, DeepSeek, training processes,
GPU, experiment queues, and result review in VS Code Remote-SSH environment.

## Key Requirements
1. Daemon (gauss-agentd): always-running, manages Cline/Codex CLI, training, GPU, state
2. VS Code Extension: sidebar UI, status panels, operation buttons, notifications
3. Cline CLI: execute code changes, tests, training launch via async subprocess
4. Safety: process identity verification, command guard, protected paths
5. State Machine: 8 Phase-1 states (QUEUED...CANCELLED)
6. IPC: JSON-Lines over TCP (Windows) or Unix Socket (Linux)
7. Persistence: SQLite with WAL, atomic writes, 9 tables
8. Recovery: restart reconciliation via SQLite + process table
9. Cross-platform: Windows dev (TCP), Linux deploy (Unix Socket)
10. Mock mode: test without API keys

## Environment
- Dev: Windows, Conda base, D:/tpc/Codex_Cline
- Deploy: Linux Remote-SSH, ~/.local/share/gauss-agent/
- Cline CLI: v3.0.46, C:/Users/Administrator/AppData/Roaming/npm/cline.cmd
