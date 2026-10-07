@echo off
title Sesiones de Claude
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sesiones.ps1"
pause
