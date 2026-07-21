# Start Agent Hub daemon (Windows PowerShell)
Set-Location "$PSScriptRoot\.."
Write-Host "Starting agent-hub daemon..."
python -m agentd.agent_hub.main
