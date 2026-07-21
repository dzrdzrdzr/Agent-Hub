# Stop Agent Hub daemon (Windows)
$found = $false
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ($_.CommandLine -like "*agent_hub*") {
            $_ | Stop-Process -Force
            Write-Host "Stopped PID $($_.Id)"
            $found = $true
        }
    } catch {}
}
if (-not $found) { Write-Host "No daemon running" }
