#!/usr/bin/env bash
# Führt die Unit-Tests unter tests/ aus. Der tests-Job in lint.yml ruft genau
# dieses Skript auf, lokal läuft es über ./scripts/check.sh, damit prüfen
# beide dasselbe.
#
# Die Tests laufen in den Dienst-Images statt in einer lokalen Umgebung.
# worker.py braucht beim Import pymongo und redis, main.py braucht fastapi,
# und geprüft werden soll gegen genau die Python-Version und glibc, mit der
# die Dienste im Cluster laufen. Die Tests unter tests/backend laufen deshalb
# im Backend-Image, alle übrigen im Worker-Image. pytest liegt bewusst in
# keinem der Images, Testwerkzeug gehört nicht in das ausgelieferte Artefakt.
# Es wird je Lauf in den Wegwerf-Container installiert, die Version kommt aus
# requirements.txt, damit sie an einer Stelle steht.
set -euo pipefail
cd "$(dirname "$0")/.."

pytest_pin=$(grep -E '^pytest==' requirements.txt || true)
if [ -z "$pytest_pin" ]; then
  echo "pytest steht nicht gepinnt in requirements.txt" >&2
  exit 1
fi

# Eigene Tags, damit der Lauf nicht die :local-Images des Compose-Stands
# überschreibt.
docker build -q -t judge-worker:tests app/worker >/dev/null
docker build -q -t judge-backend:tests app/backend >/dev/null

# uid 1000 statt root. Als root nähme der Import von worker.py den Sandbox-Pfad
# mit UID-Bereich und Namespace-Probe, als gewöhnlicher User genügt das
# SANDBOX_TRENNUNG_ERZWINGEN=0 aus tests/conftest.py. HOME=/tmp, damit
# pip install --user ohne schreibbares Home funktioniert. Cache und Bytecode
# bleiben aus, sonst legt der Lauf als Container-User Dateien in den
# Arbeitsbaum.
#
# --ignore=tests/backend im Worker-Lauf, weil pytest die Backend-Tests sonst
# schon beim Einsammeln importiert und der Import von main.py im Worker-Image
# an fastapi scheitert.
docker run --rm --user 1000:1000 \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -v "$PWD":/repo -w /repo judge-worker:tests \
  sh -c "pip install --user --quiet --no-cache-dir --no-warn-script-location '$pytest_pin' \
    && python -m pytest -q -p no:cacheprovider tests/ --ignore=tests/backend"

docker run --rm --user 1000:1000 \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -v "$PWD":/repo -w /repo judge-backend:tests \
  sh -c "pip install --user --quiet --no-cache-dir --no-warn-script-location '$pytest_pin' \
    && python -m pytest -q -p no:cacheprovider tests/backend"
