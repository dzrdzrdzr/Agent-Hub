# Stop GAUSS Agent Control Center daemon (Windows)
$found = $false
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ($_.CommandLine -like "*gauss_agentd*") {
            $_ | Stop-Process -Force
            Write-Host "Stopped PID $($_.Id)"
            $found = $true
        }
    } catch {}
}
if (-not $found) { Write-Host "No daemon running" }
