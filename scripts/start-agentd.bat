@echo off
REM Start GAUSS Agent Control Center daemon (Windows)
cd /d "%~dp0\.."
echo Starting gauss-agentd...
python -m agentd.gauss_agentd.main
pause
