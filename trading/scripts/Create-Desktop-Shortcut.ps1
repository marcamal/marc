# =============================================================================
# Puts an ATLAS icon on your Desktop.
#
# Run this once:
#   .\scripts\Create-Desktop-Shortcut.ps1
# or double-click "Create Desktop Icon.bat".
#
# After that, just double-click the ATLAS icon on your Desktop to start.
# =============================================================================

$ErrorActionPreference = "Stop"

$root     = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root "Start ATLAS.bat"
$icon     = Join-Path $PSScriptRoot "atlas.ico"
$desktop  = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "ATLAS.lnk"

if (-not (Test-Path $launcher)) {
    Write-Host "  Could not find 'Start ATLAS.bat' next to the scripts folder." -ForegroundColor Red
    Write-Host "  Run this from inside the trading folder." -ForegroundColor Red
    exit 1
}

$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)

$shortcut.TargetPath       = $launcher
# Working directory matters: the launcher resolves every other path from here.
$shortcut.WorkingDirectory = $root
$shortcut.Description      = "ATLAS - personal trading and research system"
$shortcut.WindowStyle      = 1
if (Test-Path $icon) { $shortcut.IconLocation = "$icon,0" }
$shortcut.Save()

Write-Host ""
Write-Host "  Done." -ForegroundColor Green
Write-Host ""
Write-Host "  There is now an ATLAS icon on your Desktop."
Write-Host "  Double-click it to start the system."
Write-Host ""
Write-Host "  Tip: right-click the icon -> 'Pin to Taskbar' to keep it handy."
Write-Host ""
Write-Host "  Press any key to close."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
