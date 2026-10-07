# =============================================================================
# ATLAS  -  one-time setup for Windows.
#
#   .\scripts\setup.ps1
#
# Creates the Python virtual environment, installs backend and frontend
# packages, and creates .env from the template.
#
# If PowerShell refuses to run this, allow local scripts once:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# =============================================================================

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

Write-Host ""
Write-Host "  ATLAS setup" -ForegroundColor Cyan
Write-Host "  ===========" -ForegroundColor Cyan
Write-Host ""

# --- Python ------------------------------------------------------------------
Write-Host "[1/5] Checking Python..." -ForegroundColor Yellow
try {
    $pythonVersion = (python --version 2>&1).ToString()
} catch {
    Write-Host ""
    Write-Host "  Python was not found." -ForegroundColor Red
    Write-Host "  Install Python 3.12+ from https://www.python.org/downloads/"
    Write-Host "  IMPORTANT: tick 'Add python.exe to PATH' during installation."
    exit 1
}
Write-Host "      $pythonVersion"

# Require 3.12+. ATLAS uses modern typing syntax that will not parse on 3.11.
$match = [regex]::Match($pythonVersion, "(\d+)\.(\d+)")
if ($match.Success) {
    $major = [int]$match.Groups[1].Value
    $minor = [int]$match.Groups[2].Value
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) {
        Write-Host "      Python 3.12 or newer is required (found $major.$minor)." -ForegroundColor Red
        exit 1
    }
}

# --- virtual environment -----------------------------------------------------
Write-Host "[2/5] Creating the virtual environment..." -ForegroundColor Yellow
$venv = Join-Path $backend ".venv"
if (Test-Path $venv) {
    Write-Host "      Already exists, reusing it."
} else {
    python -m venv $venv
    Write-Host "      Created at backend\.venv"
}

$venvPython = Join-Path $venv "Scripts\python.exe"

# --- backend packages --------------------------------------------------------
Write-Host "[3/5] Installing Python packages (this takes a minute)..." -ForegroundColor Yellow
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $backend "requirements-dev.txt")
Write-Host "      Done."

# --- frontend packages -------------------------------------------------------
Write-Host "[4/5] Installing frontend packages..." -ForegroundColor Yellow
try {
    $null = node --version
    Push-Location $frontend
    npm install --no-fund --no-audit --silent
    Pop-Location
    Write-Host "      Done."
} catch {
    Write-Host "      Node.js was not found  -  skipping the dashboard." -ForegroundColor Yellow
    Write-Host "      Install the LTS build from https://nodejs.org/ and re-run this script."
}

# --- .env --------------------------------------------------------------------
Write-Host "[5/5] Creating .env..." -ForegroundColor Yellow
$envFile = Join-Path $root ".env"
$envExample = Join-Path $root ".env.example"
if (Test-Path $envFile) {
    Write-Host "      Already exists, leaving it alone."
} else {
    Copy-Item $envExample $envFile
    Write-Host "      Created from .env.example"
}

Write-Host ""
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Next:"
Write-Host "    1. ATLAS already runs without an Alpaca account, on simulated data."
Write-Host "    2. To connect for real, put your PAPER keys in trading\.env"
Write-Host "       (app.alpaca.markets -> switch to Paper -> API Keys -> Generate)"
Write-Host "    3. Start the backend:   .\scripts\start-backend.ps1"
Write-Host "    4. Start the dashboard: .\scripts\start-frontend.ps1   (new window)"
Write-Host "    5. Open http://localhost:5173"
Write-Host ""
