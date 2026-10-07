# sesiones.ps1 - muestra las sesiones de Claude Code activas en esta PC (las de terminal, VSCode y
# segundo plano), con su etiqueta y si estan trabajando. Lee `claude agents --json`.
# Uso:  .\sesiones.ps1          se actualiza cada 5 s hasta Ctrl+C
#       .\sesiones.ps1 -Una     una sola vez
param([switch]$Una, [int]$Cada = 5)

function Mostrar {
    $raw = claude agents --json 2>$null | Out-String
    # en PowerShell 5.1 ConvertFrom-Json devuelve el array como UN objeto: el ForEach lo desarma
    try { $sesiones = @($raw | ConvertFrom-Json | ForEach-Object { $_ }) } catch {
        Write-Host "No pude leer las sesiones (claude agents --json): $($raw.Trim())" -ForegroundColor Red
        return
    }
    $ahora = [DateTimeOffset]::Now
    $filas = foreach ($s in $sesiones | Sort-Object startedAt) {
        $desde = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$s.startedAt).ToLocalTime()
        $hace = $ahora - $desde
        [pscustomobject]@{
            Etiqueta = $s.name
            Estado   = switch ($s.status) { "busy" { "trabajando" } "idle" { "libre" } default { $s.status } }
            Tipo     = switch ($s.kind) { "interactive" { "ventana" } "background" { "segundo plano" } default { $s.kind } }
            Carpeta  = Split-Path $s.cwd -Leaf
            Abierta  = "{0:HH:mm} (hace {1})" -f $desde.DateTime, $(if ($hace.TotalHours -ge 1) { "{0}h{1:00}m" -f [int][math]::Floor($hace.TotalHours), $hace.Minutes } else { "{0}m" -f [int]$hace.TotalMinutes })
        }
    }
    Write-Host ("Sesiones de Claude activas: {0}   ({1:HH:mm:ss})" -f @($filas).Count, $ahora.DateTime) -ForegroundColor Cyan
    if ($filas) { $filas | Format-Table -AutoSize | Out-String -Width 200 | Write-Host }
    else { Write-Host "  (ninguna)" }
}

if ($Una) { Mostrar; exit 0 }
while ($true) {
    Clear-Host
    Mostrar
    Write-Host "Se actualiza cada $Cada s. Ctrl+C para salir." -ForegroundColor DarkGray
    Start-Sleep -Seconds $Cada
}
