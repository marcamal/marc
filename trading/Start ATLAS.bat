@echo off
REM ===========================================================================
REM  ATLAS - double-click this file to start everything.
REM
REM  It does first-time setup if needed, starts the backend and the dashboard,
REM  and opens your browser. Keep the window open while you use ATLAS.
REM ===========================================================================
title ATLAS
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-ATLAS.ps1"
if errorlevel 1 pause
