# claude-interceptor.ps1 - abre Claude Code pasando por el interceptor.
# Solo afecta a esta terminal: tu Claude Code normal sigue igual.
# Por defecto retoma la ultima conversacion de esta carpeta, en bypass (lo que necesita el flujo
# manager para mandarle instrucciones a otras sesiones). Si no hay conversacion previa, abre una nueva.
# Uso:  .\claude-interceptor.ps1               retoma la ultima, en bypass
#       .\claude-interceptor.ps1 -Nueva        conversacion nueva
#       .\claude-interceptor.ps1 -ConPermisos  sin bypass (Claude pide permiso como siempre)
param([int]$Port = 8787, [switch]$Nueva, [switch]$ConPermisos)

try {
    Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 3 | Out-Null
} catch {
    Write-Host "El interceptor no responde en el puerto $Port. Corre .\start.ps1 en otra terminal primero." -ForegroundColor Red
    exit 1
}

$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:$Port"
$flags = @()
if (-not $ConPermisos) { $flags += "--dangerously-skip-permissions" }
Write-Host "Claude Code -> interceptor (http://127.0.0.1:$Port) -> Anthropic" -ForegroundColor Green

if (-not $Nueva) {
    $started = Get-Date
    claude --continue @flags
    # sin conversacion previa en esta carpeta, --continue sale enseguida con error: abrir una nueva
    if ($LASTEXITCODE -eq 0 -or ((Get-Date) - $started).TotalSeconds -gt 10) { exit $LASTEXITCODE }
    Write-Host "No hay conversacion para retomar en esta carpeta: abro una nueva." -ForegroundColor Yellow
}
claude @flags
