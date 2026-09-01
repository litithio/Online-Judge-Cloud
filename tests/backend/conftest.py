"""Umgebung für den Import von main.py aus app/backend.

auth.py liest GATEWAY_SECRET beim Import und lässt den Prozess ohne Wert gar
nicht erst starten, deshalb steht hier ein Testwert oberhalb der Mindestlänge.
main.py legt beim Import die Clients für MongoDB und Valkey an, beide
verbinden sich erst beim ersten Zugriff, die Tests brauchen also keinen
laufenden Dienst.

BACKEND_PFAD übersteuert wie WORKER_PFAD in tests/conftest.py, welcher
main.py-Stand getestet wird. Das braucht der Nachweis, dass ein Test seinen
Fehler fängt, er läuft dann gegen eine Kopie mit genau diesem Fehler statt
gegen app/backend.
"""

import os
import pathlib
import sys

os.environ["GATEWAY_SECRET"] = "testwert-ohne-jede-zufaelligkeit-nur-fuer-pytest"
sys.path.insert(
    0,
    os.getenv(
        "BACKEND_PFAD",
        str(pathlib.Path(__file__).resolve().parents[2] / "app" / "backend"),
    ),
)
