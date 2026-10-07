@echo off
title prompt-interceptor - benchmark
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bench.ps1"
pause
