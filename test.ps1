# test.ps1 - corre los tests contra un upstream simulado (no usa API key ni red).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
& $py -m pytest  # opciones en pytest.ini
