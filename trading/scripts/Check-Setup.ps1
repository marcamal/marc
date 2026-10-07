# =============================================================================
# ATLAS  -  setup check.
#
# Run this when something will not start. It checks everything ATLAS needs and
# tells you exactly what is missing and how to fix it, instead of failing with
# a stack trace.
#
# Double-click "Check Setup.bat", or:
#   .\scripts\Check-Setup.ps1
# =============================================================================

$root     = Split-Path -Parent $PSScriptRoot
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

$problems = @()

function Pass($text)  { Write-Host "  [ OK ]  $text" -ForegroundColor Green }
function Fail($text)  { Write-Host "  [FAIL]  $text" -ForegroundColor Red }
function Note($text)  { Write-Host "  [ -- ]  $text" -ForegroundColor Yellow }
function Info($text)  { Write-Host "          $text" -ForegroundColor DarkGray }

Clear-Host
Write-Host ""
Write-Host "  ATLAS - setup check" -ForegroundColor Cyan
Write-Host "  ===================" -ForegroundColor Cyan
Write-Host ""

# --- 1. Python ---------------------------------------------------------------

$pythonOk = $false
try {
    $pythonVersion = (python --version 2>&1).ToString().Trim()
    if ($pythonVersion -notmatch "Python") { throw "not python" }

    $m = [regex]::Match($pythonVersion, "(\d+)\.(\d+)")
    $major = [int]$m.Groups[1].Value
    $minor = [int]$m.Groups[2].Value

    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) {
        Fail "$pythonVersion is too old. ATLAS needs 3.12 or newer."
        Info "Install the latest from https://www.python.org/downloads/"
        $problems += "Python too old"
    } else {
        Pass "$pythonVersion"
        $pythonOk = $true
    }
} catch {
    Fail "Python was not found."
    Info "1. Install it from https://www.python.org/downloads/"
    Info "2. TICK 'Add python.exe to PATH' on the first installer screen."
    Info "3. Restart your computer afterwards."
    Info ""
    Info "Already installed it? Then the PATH box was probably not ticked."
    Info "Re-run the installer, choose 'Modify', and tick it."
    $problems += "Python missing"
}

# --- 2. Node.js --------------------------------------------------------------

$nodeOk = $false
try {
    $nodeVersion = (node --version 2>&1).ToString().Trim()
    $nodeMajor = [int]($nodeVersion -replace "^v" -replace "\..*$")
    if ($nodeMajor -lt 18) {
        Fail "Node $nodeVersion is too old. ATLAS needs 18 or newer."
        Info "Install the LTS build from https://nodejs.org/"
        $problems += "Node too old"
    } else {
        Pass "Node.js $nodeVersion"
        $nodeOk = $true
    }
} catch {
    Fail "Node.js was not found."
    Info "Install the big green LTS button at https://nodejs.org/"
    Info "Then restart your computer."
    $problems += "Node missing"
}

# --- 3. the project folder ---------------------------------------------------

# Paths are built segment by segment rather than with embedded "\", so this
# check is verifiable off Windows too.
$expected = @(
    @{ Path = (Join-Path (Join-Path (Join-Path $root "backend") "app") "main.py"); Name = "backend code" }
    @{ Path = (Join-Path (Join-Path $root "frontend") "package.json");             Name = "dashboard code" }
    @{ Path = (Join-Path (Join-Path $root "config") "risk.yaml");                  Name = "risk configuration" }
    @{ Path = (Join-Path $root ".env.example");                                    Name = "settings template" }
)
$missingFiles = @()
foreach ($item in $expected) {
    if (-not (Test-Path $item.Path)) { $missingFiles += $item.Name }
}

if ($missingFiles.Count -gt 0) {
    Fail "The project folder is incomplete. Missing: $($missingFiles -join ', ')"
    Info "Unzip the ATLAS download again, and keep the whole 'trading' folder together."
    $problems += "Files missing"
} else {
    Pass "Project files are all present"
    Info "Folder: $root"
}

# --- 4. has setup been run? --------------------------------------------------

$venvPython = Join-Path $backend ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Pass "Python packages are installed"
} else {
    Note "Python packages are not installed yet."
    Info "This is normal before the first run - the ATLAS icon installs them."
}

if (Test-Path (Join-Path $frontend "node_modules")) {
    Pass "Dashboard packages are installed"
} else {
    Note "Dashboard packages are not installed yet."
    Info "Also normal before the first run."
}

# --- 5. settings and keys ----------------------------------------------------

$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $envText = Get-Content $envFile -Raw
    $keyLine = ($envText -split "`n" | Where-Object { $_ -match "^\s*ALPACA_API_KEY\s*=" }) | Select-Object -First 1
    $keyValue = if ($keyLine) { ($keyLine -split "=", 2)[1].Trim() } else { "" }

    if ($keyValue) {
        Pass "Alpaca API key is set"
        # Paper keys start with PK. Live keys start with AK - warn, do not block.
        if ($keyValue -match "^AK") {
            Note "That key starts with 'AK', which usually means a LIVE key."
            Info "ATLAS stays in paper mode regardless, but you probably want the"
            Info "PAPER key: app.alpaca.markets -> switch to Paper -> API Keys."
        }
    } else {
        Note "No Alpaca key yet - ATLAS will run on SIMULATED data."
        Info "That is fine for exploring. Add keys later (see START HERE.txt)."
    }
} else {
    Note "No .env file yet - it is created automatically on first run."
}

# --- 6. kill switch ----------------------------------------------------------

$killFile = Join-Path $root "data\KILL_SWITCH"
if (Test-Path $killFile) {
    Note "THE KILL SWITCH IS ON - no trading will happen."
    Info "Delete this file to allow trading: $killFile"
}

# --- 7. are the ports free? --------------------------------------------------

foreach ($port in @(8000, 5173)) {
    $inUse = $null
    try {
        $inUse = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    } catch { }
    if ($inUse) {
        Note "Port $port is already in use - ATLAS may already be running."
        Info "If the dashboard does not load, close the other ATLAS window first."
    }
}

# --- verdict -----------------------------------------------------------------

Write-Host ""
Write-Host "  ---------------------------------------------------------------"
if ($problems.Count -eq 0) {
    Write-Host "   EVERYTHING LOOKS GOOD." -ForegroundColor Green
    Write-Host "  ---------------------------------------------------------------"
    Write-Host ""
    Write-Host "   Next: double-click the ATLAS icon on your Desktop."
    if (-not (Test-Path $venvPython)) {
        Write-Host "   The first start takes a few minutes while it installs."
    }
} else {
    Write-Host "   FIX THESE FIRST:" -ForegroundColor Red
    Write-Host "  ---------------------------------------------------------------"
    Write-Host ""
    foreach ($p in $problems) { Write-Host "    - $p" -ForegroundColor Red }
    Write-Host ""
    Write-Host "   The fix for each one is written above."
    Write-Host "   After installing anything, RESTART YOUR COMPUTER and run this again."
}
Write-Host ""
Write-Host "  Press any key to close."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
