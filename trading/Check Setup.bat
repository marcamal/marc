@echo off
REM ===========================================================================
REM  Run this if ATLAS will not start.
REM  It checks everything and tells you exactly what is missing.
REM ===========================================================================
title ATLAS - setup check
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Check-Setup.ps1"
