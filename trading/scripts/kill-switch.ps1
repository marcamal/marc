# =============================================================================
# ATLAS  -  emergency stop from the command line.
#
#   .\scripts\kill-switch.ps1          engage (stop all trading)
#   .\scripts\kill-switch.ps1 -Release resume
#   .\scripts\kill-switch.ps1 -Status  check
#
# This works by creating or deleting data\KILL_SWITCH, so it does not need the
# backend, the API or the dashboard to be working.
# =============================================================================

param(
    [switch]$Release,
    [switch]$Status
)

$root = Split-Path -Parent $PSScriptRoot
$file = Join-Path $root "data\KILL_SWITCH"

if ($Status) {
    if (Test-Path $file) {
        Write-Host ""
        Write-Host "  KILL SWITCH IS ENGAGED" -ForegroundColor Red
        Write-Host ""
        Get-Content $file
    } else {
        Write-Host "  Kill switch is clear. Trading is permitted." -ForegroundColor Green
    }
    exit 0
}

if ($Release) {
    if (Test-Path $file) {
        Remove-Item $file
        Write-Host "  Kill switch released. Trading is permitted again." -ForegroundColor Green
    } else {
        Write-Host "  Kill switch was not engaged." -ForegroundColor DarkGray
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $file) | Out-Null
@"
ATLAS KILL SWITCH
engaged_at: $((Get-Date).ToUniversalTime().ToString("o"))
triggered_by: kill-switch.ps1
reason: engaged manually from the command line

Delete this file to allow trading again.
"@ | Set-Content $file

Write-Host ""
Write-Host "  KILL SWITCH ENGAGED. All new orders are blocked." -ForegroundColor Red
Write-Host "  Release with: .\scripts\kill-switch.ps1 -Release" -ForegroundColor DarkGray
Write-Host ""
