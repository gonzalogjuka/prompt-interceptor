#!/usr/bin/env bash
# autostart.sh - Linux: el interceptor como servicio de usuario de systemd (sin root).
# Arranca solo, se reinicia si se cae y, con "linger", corre aunque no haya sesion abierta
# (una VM a la que entras por SSH). Equivalente de autostart.ps1 en Windows.
# Uso:
#   ./autostart.sh --install     lo instala, lo arranca y lo deja al inicio
#   ./autostart.sh --restart     lo reinicia (trae lo ultimo del repo: ver AUTO_UPDATE en config.env)
#   ./autostart.sh --stop        lo detiene (vuelve en el proximo arranque)
#   ./autostart.sh --uninstall   lo quita
#   ./autostart.sh --status      muestra si esta corriendo
set -euo pipefail
cd "$(dirname "$0")"
DIR="$PWD"
UNIT=prompt-interceptor.service
UNIT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT"
PORT=$(grep -E '^PORT=' config.env .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\r' || true)
PORT="${PORT:-8787}"

running() { curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; }

case "${1:---status}" in
    --install)
        chmod +x start.sh
        mkdir -p "$(dirname "$UNIT_FILE")"
        cat > "$UNIT_FILE" <<EOF
[Unit]
Description=prompt-interceptor (proxy local para Claude)
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/start.sh --background
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now "$UNIT"
        # sin linger, los servicios de usuario mueren al cerrar la ultima sesion SSH
        loginctl enable-linger "$USER" 2>/dev/null || echo "Aviso: no pude activar linger (sudo loginctl enable-linger $USER)."
        for _ in $(seq 1 60); do running && break; sleep 1; done
        if running; then echo "Interceptor corriendo en http://127.0.0.1:$PORT"
        else echo "No respondio todavia: journalctl --user -u $UNIT -e  o  tail -f interceptor.log"; fi
        ;;
    --restart) systemctl --user restart "$UNIT"; echo "Reiniciado." ;;
    --stop) systemctl --user stop "$UNIT"; echo "Detenido." ;;
    --uninstall)
        systemctl --user disable --now "$UNIT" 2>/dev/null || true
        rm -f "$UNIT_FILE"; systemctl --user daemon-reload
        echo "Quitado."
        ;;
    --status)
        echo "Corriendo:          $(running && echo "si, http://127.0.0.1:$PORT" || echo no)"
        echo "Servicio instalado: $([ -f "$UNIT_FILE" ] && echo si || echo no)"
        ;;
    *) sed -n '2,11p' "$0"; exit 1 ;;
esac
