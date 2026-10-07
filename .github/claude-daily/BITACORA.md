# Bitácora de mejoras diarias

La escribe la mejora diaria automática (`.github/workflows/mejora-diaria.yml`), la más nueva arriba.
Sirve de memoria entre días: qué se hizo, qué se descartó y qué ideas quedan.

## 2026-10-07 — punto de partida
- Tests reorganizados por tema (`tests/`): 54 tests, 91 casos, todos contra la API simulada.
- Sin cubrir todavía: que `.env` pise a `config.env` (lo hace `start.ps1`, PowerShell).
- Ideas: tests de propiedades sobre `gate.inspect` y `compress`; streaming cortado a mitad de evento;
  requests de otros clientes compatibles (SDK de Python/TS sin tools, con `REQUIRE_TOOLS=false`).
