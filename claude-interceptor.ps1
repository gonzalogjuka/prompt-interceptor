# claude-interceptor.ps1 - abre Claude Code pasando por el interceptor.
# Solo afecta a esta terminal: tu Claude Code normal sigue igual.
# Por defecto retoma la ultima conversacion de esta carpeta, en bypass (lo que necesita el flujo
# manager para mandarle instrucciones a otras sesiones). Si no hay conversacion previa, claude abre una nueva.
# Uso:  .\claude-interceptor.ps1               retoma la ultima, en bypass
#       .\claude-interceptor.ps1 -Nueva        conversacion nueva
#       .\claude-interceptor.ps1 -ConPermisos  sin bypass (Claude pide permiso como siempre)
#       .\claude-interceptor.ps1 -Nueva -Nombre trabajador-1
#                                               etiqueta: se ve en 6-Ver-sesiones.cmd, en --resume y en el
#                                               titulo de la ventana, y es el nombre para mandarle mensajes
param([int]$Port = 8787, [switch]$Nueva, [switch]$ConPermisos, [string]$Nombre)

try {
    Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 3 | Out-Null
} catch {
    Write-Host "El interceptor no responde en el puerto $Port. Corre .\start.ps1 en otra terminal primero." -ForegroundColor Red
    exit 1
}

# abierto desde otra sesion de Claude (p. ej. una pestaña que lanza el manager), se heredan sus variables:
# la nueva se creeria hija de esa, sin transcript ni registro propio (no recibe mensajes). Se limpian.
foreach ($v in "CLAUDECODE", "CLAUDE_PID", "CLAUDE_JOB_DIR", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
               "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
               "CLAUDE_CODE_MESSAGING_TOKEN") {
    Remove-Item "env:$v" -ErrorAction SilentlyContinue
}
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:$Port"
$flags = @()
if (-not $ConPermisos) { $flags += "--dangerously-skip-permissions" }
if ($Nombre) { $flags += @("--name", $Nombre) }
Write-Host "Claude Code -> interceptor (http://127.0.0.1:$Port) -> Anthropic" -ForegroundColor Green

# sin conversacion previa en la carpeta, --continue abre una nueva por su cuenta
if (-not $Nueva) { $flags = @("--continue") + $flags }
claude @flags
