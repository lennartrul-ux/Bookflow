#!/bin/bash
# Dubbelklik om The Format House · Leads te starten (macOS).
cd "$(dirname "$0")" || exit 1
echo ""
echo "  The Format House · Leads wordt gestart…"
echo ""
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  osascript -e 'display dialog "Python 3.10 of nieuwer is nodig. Installeer Python via python.org en dubbelklik daarna opnieuw op dit bestand." buttons {"Naar python.org"} default button 1 with title "The Format House · Leads"' >/dev/null 2>&1
  open "https://www.python.org/downloads/macos/"
  exit 1
fi
if [ ! -x .venv/bin/python ] || [ ! -f .venv/.installed ] || [ pyproject.toml -nt .venv/.installed ]; then
  echo "  Eerste keer: alles installeren (1 à 2 minuten)…"
  "$PY" -m venv .venv || { echo "Kon geen Python-omgeving maken."; read -r -n1; exit 1; }
  .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
  .venv/bin/python -m pip install -q -e . || { echo ""; echo "  Installatie mislukt. Stuur een screenshot van dit venster."; read -r -n1; exit 1; }
  touch .venv/.installed
fi
[ -f .env ] || cp .env.example .env
.venv/bin/python -m formathouse.webapp
