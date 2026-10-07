#!/usr/bin/env bash
# claude-interceptor.sh - abre Claude Code pasando por el interceptor (solo esta terminal).
# Uso:  ./claude-interceptor.sh            (argumentos extra se pasan a claude)
PORT="${PORT:-8787}"
if ! curl -fsS --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "El interceptor no responde en el puerto $PORT. Corre ./autostart.sh --install (o ./start.sh)." >&2
    exit 1
fi
export ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT"
echo "Claude Code -> interceptor ($ANTHROPIC_BASE_URL) -> Anthropic"
exec claude "$@"
