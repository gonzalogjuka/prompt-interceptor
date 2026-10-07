@echo off
title Claude Code via interceptor
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-interceptor.ps1"
pause
