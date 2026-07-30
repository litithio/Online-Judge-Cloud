#!/usr/bin/env python3
"""Lädt die Aufgaben aus diesem Ordner nach MongoDB.

Der Titel ist der Schlüssel, damit ein zweiter Lauf eine Aufgabe aktualisiert
statt sie ein zweites Mal anzulegen. Läuft im Worker-Image, weil dort pymongo
schon liegt. Im Cluster führt dasselbe Skript ein Job nach dem Ausrollen aus.
"""

import json
import os
import pathlib
import sys

from pymongo import MongoClient

# Nur die oberste Ebene wird gelesen, loesungen/ bleibt damit außen vor. Die
# Lösungen gehören nicht in die Datenbank, sie sind Material für den Prüflauf.
ORDNER = pathlib.Path(__file__).parent
FELDER = ("title", "description", "test_cases")


def gelesen(datei):
    """Liest eine Aufgabe und meldet fehlende Felder mit Dateinamen."""
    aufgabe = json.loads(datei.read_text(encoding="utf-8"))
    fehlt = [f for f in FELDER if not aufgabe.get(f)]
    if fehlt:
        raise SystemExit(f"{datei.name}: {', '.join(fehlt)} fehlt oder ist leer")
    # Auf Typen geprüft, nicht nur auf Vorhandensein. Der Worker schreibt die
    # Eingabe unverändert in eine Datei, eine Zahl statt einer Zeichenkette
    # beantwortet dort jede Einreichung mit SYSTEM_ERROR. Und ein "in"-Test auf
    # einem String würde Teilzeichenketten finden statt Feldnamen.
    for nummer, fall in enumerate(aufgabe["test_cases"], 1):
        if not isinstance(fall, dict):
            raise SystemExit(f"{datei.name}: Testfall {nummer} ist kein Objekt")
        if not isinstance(fall.get("input"), str) or not isinstance(
            fall.get("expected_output"), str
        ):
            raise SystemExit(
                f"{datei.name}: Testfall {nummer} braucht input und "
                f"expected_output als Zeichenketten"
            )
    return aufgabe


def main():
    dateien = sorted(ORDNER.glob("*.json"))
    if not dateien:
        raise SystemExit(f"keine Aufgaben in {ORDNER}")

    db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))[
        "coding_platform"
    ]
    for datei in dateien:
        aufgabe = gelesen(datei)
        db.tasks.update_one(
            {"title": aufgabe["title"]},
            {
                "$set": {
                    "description": aufgabe["description"],
                    "test_cases": aufgabe["test_cases"],
                }
            },
            upsert=True,
        )
        print(f"{datei.name}: {len(aufgabe['test_cases'])} Testfälle")

    print(
        f"{len(dateien)} Aufgaben geladen, "
        f"{db.tasks.count_documents({})} in der Sammlung"
    )


if __name__ == "__main__":
    sys.exit(main())
