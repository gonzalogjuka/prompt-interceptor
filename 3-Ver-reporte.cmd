@echo off
title prompt-interceptor - reporte
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0report.ps1"
pause
