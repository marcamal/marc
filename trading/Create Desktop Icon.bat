@echo off
REM Double-click this once to put an ATLAS icon on your Desktop.
title ATLAS - create desktop icon
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Create-Desktop-Shortcut.ps1"
