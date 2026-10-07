# =============================================================================
# ATLAS  -  start the dashboard.
#
#   .\scripts\start-frontend.ps1
#
# Run this in a SECOND PowerShell window, with the backend already running.
# Press Ctrl+C to stop.
# =============================================================================

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $root "frontend"

if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host ""
    Write-Host "  Frontend packages are missing. Run .\scripts\setup.ps1 first," -ForegroundColor Red
    Write-Host "  or: cd frontend; npm install" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Starting the ATLAS dashboard on http://localhost:5173" -ForegroundColor Cyan
Write-Host "  The backend must be running in another window." -ForegroundColor DarkGray
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

Push-Location $frontend
try {
    npm run dev
} finally {
    Pop-Location
}
