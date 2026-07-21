@echo off
REM Start Agent Hub daemon (Windows)
cd /d "%~dp0\.."
echo Starting agent-hub daemon...
python -m agentd.agent_hub.main
pause
