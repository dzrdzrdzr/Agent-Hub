# Blockers

## Current Blockers
- None.

## Resolved
- Python -c shell escaping on Windows: resolved by using TTY + write_stdin
- os.getuid() on Windows: resolved with try/except helper function
- SQLite old_state NOT NULL on initial transition: resolved (made nullable)

## Potential Future Blockers
- Cline API key authentication (mock mode covers this)
- VS Code extension submission to marketplace (not required for Phase 1)
