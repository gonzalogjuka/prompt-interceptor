# claude-interceptor.ps1 - abre Claude Code pasando por el interceptor.
# Solo afecta a esta terminal: tu Claude Code normal sigue igual.
# Uso:  .\claude-interceptor.ps1            (argumentos extra se pasan a claude)
param([int]$Port = 8787)

try {
    Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 3 | Out-Null
} catch {
    Write-Host "El interceptor no responde en el puerto $Port. Corre .\start.ps1 en otra terminal primero." -ForegroundColor Red
    exit 1
}

$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:$Port"
Write-Host "Claude Code -> interceptor (http://127.0.0.1:$Port) -> Anthropic" -ForegroundColor Green
claude @args
