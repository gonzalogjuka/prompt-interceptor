#!/usr/bin/env bash
# start.sh - Linux/Mac: prepara el entorno (solo la primera vez) y levanta el interceptor.
# Uso:  ./start.sh                primer plano, log en pantalla
#       ./start.sh --background   log en interceptor.log (lo usa autostart.sh / systemd)
# Igual que start.ps1: carga config.env (viaja con el repo) y despues .env (local, pisa lo anterior).
set -euo pipefail
cd "$(dirname "$0")"

BACKGROUND=false
[ "${1:-}" = "--background" ] && BACKGROUND=true

load_env() {
    local file
    for file in config.env .env; do
        [ -f "$file" ] || continue
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%$'\r'}"
            line="${line#"${line%%[![:space:]]*}"}"
            [ -z "$line" ] || [ "${line:0:1}" = "#" ] || [[ "$line" != *=* ]] && continue
            key="${line%%=*}"; value="${line#*=}"
            key="${key%"${key##*[![:space:]]}"}"; value="${value#"${value%%[![:space:]]*}"}"
            if [ -n "$value" ]; then export "$key=$value"; fi
        done < "$file"
    done
    return 0  # una ultima linea vacia ("ANTHROPIC_API_KEY=") no es un error
}

load_env

# Al arrancar en segundo plano trae lo ultimo del repo (los PRs de la mejora diaria que mergeaste).
# Nunca frena el arranque: sin red o sin credenciales sigue con lo que hay.
if $BACKGROUND && [ "${AUTO_UPDATE:-true}" = "true" ] && [ -d .git ]; then
    if out=$(GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never timeout 30 git pull --ff-only --quiet 2>&1); then
        echo "$(date '+%F %T') INFO    [update] repo al dia ($(git log -1 --format=%h))" >> interceptor.log
        load_env  # config.env pudo cambiar
    else
        echo "$(date '+%F %T') WARNING [update] no se pudo actualizar, sigo con $(git log -1 --format=%h): ${out//$'\n'/ }" >> interceptor.log
    fi
fi

venv_python() {
    if [ -x .venv/bin/python ]; then echo .venv/bin/python
    elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe  # Git Bash en Windows
    fi
}
if [ -z "$(venv_python)" ]; then
    echo "Creando entorno virtual (.venv)..."
    "$(command -v python3 || command -v python)" -m venv .venv
fi
PY=$(venv_python)
echo "Verificando dependencias..."
"$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt

if [ ! -f .env ]; then
    printf '# .env - solo de esta maquina, no se sube al repo. Pisa los valores de config.env.\nANTHROPIC_API_KEY=\n' > .env
    echo "Se creo .env (solo para la key y ajustes locales)"
fi

PORT="${PORT:-8787}"
if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "El interceptor ya esta corriendo en el puerto $PORT. Nada que hacer."
    exit 0
fi

$BACKGROUND && export LOG_FILE="$PWD/interceptor.log"
[ -z "${ANTHROPIC_API_KEY:-}" ] && echo "ANTHROPIC_API_KEY vacia: el interprete reutiliza las credenciales del cliente."
echo "Interceptor escuchando en http://127.0.0.1:$PORT  (Ctrl+C para detener)"
exec "$PY" -m interceptor.app
