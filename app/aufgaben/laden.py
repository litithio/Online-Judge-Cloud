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

# Die Lösungen gehören nicht in die Datenbank. Sie sind Material für den
# Prüflauf und liegen deshalb im Chart (app/chart/loesungen).
ORDNER = pathlib.Path(__file__).parent
FELDER = ("title", "description", "difficulty", "test_cases")

# Deutsch und als feste Liste, weil der Wert unverändert als Marke in der
# Aufgabenliste steht. Ein Tippfehler fiele sonst erst auf der Seite auf.
SCHWIERIGKEITEN = ("leicht", "mittel", "schwer")

# Zeit und Speicher darf jede Aufgabe selbst festlegen, weil sich der Bedarf
# stark unterscheidet. Fehlen sie, nimmt der Worker seine Vorgaben.
# Obergrenzen wie in app/worker/worker.py. Eine Aufgabe soll das Zeitlimit nicht
# abschalten und nicht mehr Speicher erlauben, als der Container hat.
GRENZEN = {"time_limit_seconds": 60, "memory_limit_mb": 256}


def gelesen(datei):
    """Liest eine Aufgabe und meldet fehlende Felder mit Dateinamen."""
    aufgabe = json.loads(datei.read_text(encoding="utf-8"))
    fehlt = [f for f in FELDER if not aufgabe.get(f)]
    if fehlt:
        raise SystemExit(f"{datei.name}: {', '.join(fehlt)} fehlt oder ist leer")
    if aufgabe["difficulty"] not in SCHWIERIGKEITEN:
        raise SystemExit(
            f"{datei.name}: difficulty muss leicht, mittel oder schwer sein, "
            f"ist {aufgabe['difficulty']!r}"
        )
    # Auf Typen geprüft, nicht nur auf Vorhandensein. Der Worker schreibt die
    # Eingabe unverändert in eine Datei, eine Zahl statt einer Zeichenkette
    # lässt dort jeden Lauf als Umgebungsfehler enden. Die Einreichung bleibt
    # dann auf RUNNING liegen, bis der Durchlauf sie nach MAX_VERSUCHE aufgibt.
    # Und ein "in"-Test auf einem String würde Teilzeichenketten finden statt
    # Feldnamen.
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
        # Der Name beschriftet die Zeile des Testfalls auf der Ergebnisseite.
        if not isinstance(fall.get("name"), str) or not fall["name"]:
            raise SystemExit(f"{datei.name}: Testfall {nummer} braucht einen Namen")
    # Optional, aber wenn vorhanden, dann brauchbar. Eine Grenze als Zeichenkette
    # oder als Null würde jede Einreichung dieser Aufgabe falsch beurteilen.
    for feld, hoechstens in GRENZEN.items():
        if feld not in aufgabe:
            continue
        wert = aufgabe[feld]
        if not isinstance(wert, int) or isinstance(wert, bool) or wert <= 0:
            raise SystemExit(
                f"{datei.name}: {feld} muss eine positive ganze Zahl sein, ist {wert!r}"
            )
        if wert > hoechstens:
            raise SystemExit(
                f"{datei.name}: {feld} darf höchstens {hoechstens} sein, ist {wert}"
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
        setzen = {
            "description": aufgabe["description"],
            "difficulty": aufgabe["difficulty"],
            "test_cases": aufgabe["test_cases"],
        }
        # Was in der Datei fehlt, wird in der Datenbank entfernt. Sonst bliebe
        # ein Limit stehen, das jemand aus der Aufgabe genommen hat, und die
        # Datei wäre nicht mehr die Wahrheit.
        entfernen = {}
        for feld in GRENZEN:
            if feld in aufgabe:
                setzen[feld] = aufgabe[feld]
            else:
                entfernen[feld] = ""

        aenderung = {"$set": setzen}
        if entfernen:
            aenderung["$unset"] = entfernen
        db.tasks.update_one({"title": aufgabe["title"]}, aenderung, upsert=True)
        print(f"{datei.name}: {len(aufgabe['test_cases'])} Testfälle")

    print(
        f"{len(dateien)} Aufgaben geladen, "
        f"{db.tasks.count_documents({})} in der Collection"
    )


if __name__ == "__main__":
    sys.exit(main())
