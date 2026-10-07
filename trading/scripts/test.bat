@echo off
REM Double-click this to run the ATLAS test suite.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0test.ps1"
pause
