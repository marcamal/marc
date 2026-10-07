# =============================================================================
# ATLAS — one-click launcher.
#
# Double-click "Start ATLAS.bat", or the desktop icon, and this does
# everything: first-time setup if needed, starts the backend, starts the
# dashboard, waits until both are actually responding, and opens your browser.
#
# Keep this window open while you use ATLAS. Closing it stops everything.
# =============================================================================

$ErrorActionPreference = "Stop"

$root     = Split-Path -Parent $PSScriptRoot
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"

$BackendUrl  = "http://127.0.0.1:8000"
$FrontendUrl = "http://localhost:5173"

$script:backendProcess  = $null
$script:frontendProcess = $null

# --- helpers -----------------------------------------------------------------

function Write-Step($text)    { Write-Host "  $text" -ForegroundColor Cyan }
function Write-Ok($text)      { Write-Host "  $text" -ForegroundColor Green }
function Write-Warn($text)    { Write-Host "  $text" -ForegroundColor Yellow }
function Write-Err($text)     { Write-Host "  $text" -ForegroundColor Red }

function Test-Url($url) {
    try {
        $null = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        return $true
    } catch {
        # A 4xx still means something is listening, which is what we are asking.
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

function Wait-ForUrl($url, $label, $timeoutSeconds = 90) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Url $url) { return $true }
        Start-Sleep -Milliseconds 700
        Write-Host "." -NoNewline -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Err "$label did not start within $timeoutSeconds seconds."
    return $false
}

function Stop-Atlas {
    Write-Host ""
    Write-Step "Stopping ATLAS..."
    foreach ($p in @($script:frontendProcess, $script:backendProcess)) {
        if ($p -and -not $p.HasExited) {
            try {
                # Kill the whole tree: uvicorn and vite both spawn children that
                # would otherwise keep the ports bound.
                Start-Process -FilePath "taskkill.exe" `
                    -ArgumentList "/PID", $p.Id, "/T", "/F" `
                    -NoNewWindow -Wait -ErrorAction SilentlyContinue
            } catch { }
        }
    }
    Write-Ok "ATLAS stopped."
}

Clear-Host
Write-Host ""
Write-Host "   █████╗ ████████╗██╗      █████╗ ███████╗" -ForegroundColor Blue
Write-Host "  ██╔══██╗╚══██╔══╝██║     ██╔══██╗██╔════╝" -ForegroundColor Blue
Write-Host "  ███████║   ██║   ██║     ███████║███████╗" -ForegroundColor Blue
Write-Host "  ██╔══██║   ██║   ██║     ██╔══██║╚════██║" -ForegroundColor Blue
Write-Host "  ██║  ██║   ██║   ███████╗██║  ██║███████║" -ForegroundColor Blue
Write-Host "  ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚══════╝" -ForegroundColor Blue
Write-Host "   personal trading and research system" -ForegroundColor DarkGray
Write-Host ""

# --- is it already running? --------------------------------------------------

if ((Test-Url "$BackendUrl/api/system/health") -and (Test-Url $FrontendUrl)) {
    Write-Ok "ATLAS is already running."
    Start-Process $FrontendUrl
    Write-Host ""
    Write-Host "  Opened $FrontendUrl in your browser." -ForegroundColor DarkGray
    Write-Host "  Press any key to close this window (ATLAS keeps running)."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 0
}

# --- prerequisites -----------------------------------------------------------

Write-Step "Checking Python..."
try {
    $pythonVersion = (python --version 2>&1).ToString().Trim()
} catch {
    Write-Host ""
    Write-Err "Python is not installed, or not on your PATH."
    Write-Host ""
    Write-Host "  1. Download Python 3.12 or newer: https://www.python.org/downloads/"
    Write-Host "  2. Run the installer."
    Write-Host "  3. IMPORTANT: tick 'Add python.exe to PATH' on the first screen."
    Write-Host "  4. Restart your computer, then double-click this again."
    Write-Host ""
    Write-Host "  Press any key to close."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}
Write-Ok "$pythonVersion"

$hasNode = $true
try {
    $nodeVersion = (node --version 2>&1).ToString().Trim()
    Write-Ok "Node.js $nodeVersion"
} catch {
    $hasNode = $false
    Write-Warn "Node.js not found — the dashboard needs it."
    Write-Host "       Install the LTS build from https://nodejs.org/ and run this again."
    Write-Host "       (The backend will still start, so the API works.)"
}

# --- first-run setup ---------------------------------------------------------

if (-not (Test-Path $venvPython)) {
    Write-Host ""
    Write-Step "First run — setting up. This takes a few minutes, only once."
    Write-Host ""
    & (Join-Path $PSScriptRoot "setup.ps1")
    if (-not (Test-Path $venvPython)) {
        Write-Err "Setup did not finish. Scroll up to see what failed."
        Write-Host "  Press any key to close."
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        exit 1
    }
    Write-Host ""
}

if ($hasNode -and -not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Step "Installing dashboard packages (once)..."
    Push-Location $frontend
    npm install --no-fund --no-audit --silent
    Pop-Location
    Write-Ok "Done."
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Copy-Item (Join-Path $root ".env.example") (Join-Path $root ".env")
    Write-Warn "Created .env — ATLAS will run on SIMULATED data until you add Alpaca keys."
}

# --- stop everything cleanly when this window closes -------------------------

$null = Register-EngineEvent PowerShell.Exiting -Action { Stop-Atlas }

# --- start the backend -------------------------------------------------------

Write-Host ""
Write-Step "Starting the backend"
$script:backendProcess = Start-Process -FilePath $venvPython `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $backend -PassThru -WindowStyle Minimized

if (-not (Wait-ForUrl "$BackendUrl/api/system/health" "The backend")) {
    Write-Host ""
    Write-Err "The backend did not start. Check its minimised window in the taskbar."
    Write-Host "  Press any key to close."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    Stop-Atlas
    exit 1
}
Write-Host ""
Write-Ok "Backend running on $BackendUrl"

# --- start the dashboard -----------------------------------------------------

if ($hasNode) {
    Write-Step "Starting the dashboard"
    $script:frontendProcess = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", "npm run dev" `
        -WorkingDirectory $frontend -PassThru -WindowStyle Minimized

    if (Wait-ForUrl $FrontendUrl "The dashboard" 90) {
        Write-Host ""
        Write-Ok "Dashboard running on $FrontendUrl"
        Start-Sleep -Milliseconds 800
        Start-Process $FrontendUrl
    }
}

# --- show what the operator needs to know ------------------------------------

$mode = "unknown"
try {
    $status = Invoke-RestMethod -Uri "$BackendUrl/api/system/status" -TimeoutSec 5
    $mode = $status.mode.effective.ToUpper()
    $simulated = $status.broker.simulated
} catch { $simulated = $true }

Write-Host ""
Write-Host "  ---------------------------------------------------------------"
Write-Host "   ATLAS IS RUNNING" -ForegroundColor Green
Write-Host "  ---------------------------------------------------------------"
Write-Host ""
Write-Host "   Dashboard :  $FrontendUrl"
Write-Host "   API docs  :  $BackendUrl/docs"
Write-Host "   Mode      :  $mode" -NoNewline
if ($simulated) {
    Write-Host "  (simulated data — no Alpaca keys yet)" -ForegroundColor Yellow
} else {
    Write-Host "  (connected to Alpaca paper trading)" -ForegroundColor Green
}
Write-Host ""
Write-Host "   EMERGENCY STOP: the red Kill Switch button in the dashboard," -ForegroundColor DarkGray
Write-Host "   or create an empty file called KILL_SWITCH in the data folder." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ---------------------------------------------------------------"
Write-Host ""
Write-Host "   Press  Q  to stop ATLAS." -ForegroundColor Cyan
Write-Host "   Press  D  to reopen the dashboard."
Write-Host "   Closing this window also stops ATLAS."
Write-Host ""

while ($true) {
    if ($Host.UI.RawUI.KeyAvailable) {
        $key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        switch ($key.Character.ToString().ToUpper()) {
            "Q" { Stop-Atlas; exit 0 }
            "D" { Start-Process $FrontendUrl }
        }
    }

    # If the backend dies on its own, do not pretend everything is fine.
    if ($script:backendProcess -and $script:backendProcess.HasExited) {
        Write-Host ""
        Write-Err "The backend stopped unexpectedly."
        Write-Host "  Press any key to close."
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        Stop-Atlas
        exit 1
    }

    Start-Sleep -Milliseconds 400
}
