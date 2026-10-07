# start.ps1 - prepara el entorno (solo la primera vez) y levanta el interceptor.
# Uso:  .\start.ps1               ventana visible, log en pantalla
#       .\start.ps1 -Background   sin ventana, log en interceptor.log (lo usa autostart.ps1)
param([switch]$Background)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Carga config.env (viaja con el repo) y despues .env (local, pisa lo anterior).
# Las lineas vacias o comentadas se ignoran.
function Import-EnvFiles {
    foreach ($file in @("config.env", ".env")) {
        if (-not (Test-Path $file)) { continue }
        Get-Content $file | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $k, $v = $line -split "=", 2
                if ($v.Trim()) { [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process") }
            }
        }
    }
}
Import-EnvFiles

# Al arrancar en segundo plano trae lo ultimo del repo (los PRs de la mejora diaria que mergeaste).
# Nunca frena el arranque: sin red o sin credenciales sigue con lo que hay.
if ($Background -and $env:AUTO_UPDATE -ne "false" -and (Test-Path ".git")) {
    $env:GIT_TERMINAL_PROMPT = "0"; $env:GCM_INTERACTIVE = "never"  # nunca pedir login en segundo plano
    # via cmd: el stderr de git llega como texto plano, no como ErrorRecord de PowerShell
    $job = Start-Job { Set-Location $using:PSScriptRoot; cmd /c "git pull --ff-only --quiet 2>&1" | Out-String; $LASTEXITCODE }
    $done = Wait-Job $job -Timeout 30
    $out = if ($done) { Receive-Job $job } else { @("timeout de 30 s", 1) }
    Remove-Job $job -Force
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $head = git log -1 --format=%h
    if ($out[-1] -eq 0) {
        "$stamp INFO    [update] repo al dia ($head)" | Add-Content "interceptor.log" -Encoding utf8
        Import-EnvFiles  # config.env pudo cambiar
    } else {
        $why = ($out[0..($out.Count - 2)] -join " ") -replace "\s+", " "
        "$stamp WARNING [update] no se pudo actualizar, sigo con ${head}: $why" | Add-Content "interceptor.log" -Encoding utf8
    }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creando entorno virtual (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
}
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "Verificando dependencias..." -ForegroundColor Cyan
& $py -m pip install --quiet --disable-pip-version-check -r requirements.txt

if (-not (Test-Path ".env")) {
    "# .env - solo de esta PC, no se sube al repo. Pisa los valores de config.env.`nANTHROPIC_API_KEY=" |
        Set-Content ".env" -Encoding utf8
    Write-Host "Se creo .env (solo para la key y ajustes locales)" -ForegroundColor Yellow
}

$port = if ($env:PORT) { $env:PORT } else { "8787" }
try {
    Invoke-RestMethod "http://127.0.0.1:$port/healthz" -TimeoutSec 2 | Out-Null
    Write-Host "El interceptor ya esta corriendo en el puerto $port (quizas en segundo plano). Nada que hacer." -ForegroundColor Yellow
    if (-not $Background) { Read-Host "Enter para cerrar" }
    exit 0
} catch { }

if ($Background) { $env:LOG_FILE = Join-Path $PSScriptRoot "interceptor.log" }
if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host "ANTHROPIC_API_KEY vacia: el interprete va a reutilizar las credenciales del cliente." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Interceptor escuchando en http://127.0.0.1:$port  (Ctrl+C para detener)" -ForegroundColor Green
Write-Host "En otra terminal:  .\claude-interceptor.ps1" -ForegroundColor Green
Write-Host ""
& $py -m interceptor.app
