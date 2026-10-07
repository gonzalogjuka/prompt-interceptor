# autostart.ps1 - arranque automatico del interceptor al iniciar sesion en Windows, sin ventana.
# No necesita permisos de administrador: usa un acceso directo en la carpeta Inicio del usuario.
# Uso:
#   .\autostart.ps1 -Install     lo agrega al inicio de sesion y lo arranca ahora
#   .\autostart.ps1 -Uninstall   lo quita del inicio de sesion y lo detiene
#   .\autostart.ps1 -Stop        detiene el que corre en segundo plano
#   .\autostart.ps1 -Status      muestra si esta corriendo y si arranca solo
param([switch]$Install, [switch]$Uninstall, [switch]$Stop, [switch]$Status, [int]$Port = 8787)
$ErrorActionPreference = "Stop"
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "prompt-interceptor.lnk"
$start = Join-Path $PSScriptRoot "start.ps1"
$psArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`" -Background"

function Test-Running {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}
function Stop-Interceptor {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
}

if ($Install) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = "powershell.exe"
    $sc.Arguments = $psArgs
    $sc.WorkingDirectory = $PSScriptRoot
    $sc.WindowStyle = 7
    $sc.Description = "prompt-interceptor (segundo plano)"
    $sc.Save()
    Write-Host "Arranque automatico instalado: $lnk" -ForegroundColor Green
    if (-not (Test-Running)) {
        Start-Process powershell.exe -ArgumentList $psArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
        Write-Host "Arrancando en segundo plano (la primera vez puede tardar mientras instala dependencias)..."
        for ($i = 0; $i -lt 60 -and -not (Test-Running); $i++) { Start-Sleep -Seconds 1 }
    }
    if (Test-Running) { Write-Host "Interceptor corriendo en http://127.0.0.1:$Port" -ForegroundColor Green }
    else { Write-Host "No respondio todavia. Revisa interceptor.log con 4-Ver-log.cmd" -ForegroundColor Yellow }
}
elseif ($Uninstall) {
    if (Test-Path $lnk) { Remove-Item $lnk }
    Stop-Interceptor
    Write-Host "Arranque automatico quitado e interceptor detenido." -ForegroundColor Green
}
elseif ($Stop) {
    Stop-Interceptor
    Write-Host "Interceptor detenido (vuelve a arrancar en el proximo inicio de sesion si esta instalado)." -ForegroundColor Green
}
else {
    $running = Test-Running
    Write-Host ("Corriendo:         " + $(if ($running) { "si, http://127.0.0.1:$Port" } else { "no" }))
    Write-Host ("Arranque al login: " + $(if (Test-Path $lnk) { "si" } else { "no" }))
}
