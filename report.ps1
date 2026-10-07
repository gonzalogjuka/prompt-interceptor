# report.ps1 - resumen de lo que hizo el interceptor (desde metrics.jsonl).
Set-Location $PSScriptRoot
if (-not (Test-Path "metrics.jsonl")) {
    Write-Host "Todavia no hay metricas: usa Claude Code via .\claude-interceptor.ps1 y volve a correr esto." -ForegroundColor Yellow
    exit 0
}
& (Join-Path $PSScriptRoot ".venv\Scripts\python.exe") report.py metrics.jsonl
