# Start GAUSS Agent Control Center daemon (Windows PowerShell)
Set-Location "$PSScriptRoot\.."
Write-Host "Starting gauss-agentd..."
python -m agentd.gauss_agentd.main
