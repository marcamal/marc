# =============================================================================
# ATLAS — run the test suite.
#
#   .\scripts\test.ps1              all tests
#   .\scripts\test.ps1 -Coverage    with a coverage report
#   .\scripts\test.ps1 -Filter risk only tests matching "risk"
#
# No test touches the network or places a brokerage order.
# =============================================================================

param(
    [switch]$Coverage,
    [string]$Filter = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "  The virtual environment is missing. Run .\scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

$arguments = @("-m", "pytest")
if ($Filter) { $arguments += @("-k", $Filter) }
if ($Coverage) { $arguments += @("--cov=app", "--cov-report=term-missing") }

Write-Host ""
Write-Host "  Running the ATLAS test suite..." -ForegroundColor Cyan
Write-Host ""

Push-Location $backend
try {
    & $venvPython @arguments
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "  All tests passed." -ForegroundColor Green
} else {
    Write-Host "  Some tests failed." -ForegroundColor Red
}
exit $exitCode
