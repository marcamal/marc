# =============================================================================
# ATLAS — start the backend.
#
#   .\scripts\start-backend.ps1
#
# Press Ctrl+C to stop.
# =============================================================================

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host ""
    Write-Host "  The virtual environment is missing. Run .\scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host ""
    Write-Host "  No .env file — ATLAS will run on simulated data." -ForegroundColor Yellow
    Write-Host "  Copy .env.example to .env and add your Alpaca paper keys to connect." -ForegroundColor Yellow
    Write-Host ""
}

Write-Host ""
Write-Host "  Starting ATLAS backend on http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "  API docs: http://127.0.0.1:8000/docs" -ForegroundColor Cyan
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

Push-Location $backend
try {
    # --reload restarts on a code change. Drop it if you find it distracting.
    & $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
} finally {
    Pop-Location
}
