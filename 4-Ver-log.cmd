@echo off
title prompt-interceptor - log
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path interceptor.log) { Get-Content interceptor.log -Tail 40 -Wait -Encoding UTF8 } else { Write-Host 'Todavia no hay log: el modo segundo plano no corrio aun (autostart.ps1 -Install).'; pause }"
