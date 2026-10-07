# bench.ps1 - corre el benchmark A/B: mismas tareas directo a la API y a traves del interceptor.
# Necesita ANTHROPIC_API_KEY (en .env o en el entorno). Los argumentos se pasan al script:
#   .\bench.ps1
#   .\bench.ps1 --min-chars 200
#   .\bench.ps1 --only pod-oom,acr-pull --model claude-sonnet-5-5
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check -r requirements.txt
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $k, $v = $line -split "=", 2
            if ($v.Trim()) { [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process") }
        }
    }
}
if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host "Falta ANTHROPIC_API_KEY: ponela en .env (el benchmark llama a la API directamente)." -ForegroundColor Red
    exit 1
}
& $py bench\run_bench.py @args
