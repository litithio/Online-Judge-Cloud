"""Umgebung für den Import von worker.py.

worker.py initialisiert beim Import die Sandbox. Ohne SANDBOX_TRENNUNG_ERZWINGEN=0
bricht der Import ab, sobald der Testlauf nicht als root läuft, und SANDBOX_BASIS
muss auf ein schreibbares Verzeichnis zeigen, sonst scheitert das Anlegen unter
/work. Beides steht hier und nicht in den Testdateien, denn pytest lädt conftest.py
vor jedem Testmodul, damit funktioniert dort ein gewöhnliches import worker.

WORKER_PFAD übersteuert, welcher worker.py-Stand getestet wird. Das braucht der
Nachweis, dass ein Test seinen Fehler fängt, er läuft dann gegen eine Kopie mit
genau diesem Fehler statt gegen app/worker.
"""

import os
import pathlib
import sys
import tempfile

os.environ["SANDBOX_TRENNUNG_ERZWINGEN"] = "0"
os.environ["SANDBOX_BASIS"] = os.path.join(tempfile.mkdtemp(), "judge")
sys.path.insert(
    0,
    os.getenv(
        "WORKER_PFAD",
        str(pathlib.Path(__file__).resolve().parents[1] / "app" / "worker"),
    ),
)
