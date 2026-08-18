#!/usr/bin/env python3
"""Durchlauf aus #82: nimmt Einreichungen zurück, deren Frist ohne Ergebnis
verstrichen ist.

Das deckt zwei Fälle ab, die für die Queue gleich aussehen: ein Umgebungsfehler,
den der Worker absichtlich nicht selbst beantwortet (#78, #52), und ein
Worker-Pod, der gestorben ist oder hängt, ohne den XACK-losen Fall zu melden.
Beides bleibt als RUNNING ohne Ergebnis stehen, bis die Frist abläuft.

Läuft einmal und beendet sich. Im Cluster als CronJob
(app/chart/templates/judge.yaml), lokal von Hand über
`docker compose run --rm worker python3 durchlauf.py`.
"""

import os
from datetime import datetime, timezone

import redis
from pymongo import MongoClient, ReturnDocument

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
REDIS_URI = os.getenv("REDIS_URI", "redis://localhost:6379")

# n aus der Beschlusslage zu #82: wie viele Versuche eine Einreichung
# bekommt, bevor sie als nicht beurteilt gilt statt erneut eingereiht zu
# werden. versuche zählt Claims, nicht Durchlauf-Läufe, siehe worker.py.
MAX_VERSUCHE = int(os.getenv("MAX_VERSUCHE", "3"))

# Platzhalter: welchen Zustand eine erschöpfte Einreichung bekommt, entscheidet
# #81. UNRESOLVED markiert bis dahin nur "kein Urteil zustande gekommen",
# weder SUCCESS/FAILED über den Code noch ein eigener Name aus #81.
ENDZUSTAND_ERSCHOEPFT = "UNRESOLVED"


def _abgelaufene_uebernahmen(db, jetzt):
    """Findet Kandidaten. Die Entscheidung je Einreichung trifft der bedingte
    Update unten erneut, gegen denselben Zeitpunkt jetzt: Ein Worker kann
    zwischen diesem find und dem Update noch fertig geworden sein."""
    return db.submissions.find(
        {"status": "RUNNING", "frist": {"$lt": jetzt}},
        {"_id": 1, "sprache": 1, "versuche": 1},
    )


def durchlauf():
    db = MongoClient(MONGO_URI)["coding_platform"]
    redis_client = redis.Redis.from_url(REDIS_URI)
    jetzt = datetime.now(timezone.utc)

    erneut_eingereiht = 0
    aufgegeben = 0

    # Materialisiert, nicht als offener Cursor: die Schleife schreibt
    # währenddessen an dieselbe Collection, ein offener Cursor darauf sähe
    # davon je nach Batchgröße unvorhersagbar etwas mit.
    for eintrag in list(_abgelaufene_uebernahmen(db, jetzt)):
        sub_id = eintrag["_id"]

        if eintrag["versuche"] >= MAX_VERSUCHE:
            ergebnis = db.submissions.find_one_and_update(
                {"_id": sub_id, "status": "RUNNING", "frist": {"$lt": jetzt}},
                {
                    "$set": {
                        "status": ENDZUSTAND_ERSCHOEPFT,
                        "run_token": None,
                        "frist": None,
                    }
                },
            )
            if ergebnis is not None:
                aufgegeben += 1
                print(
                    f"Einreichung {sub_id}: {eintrag['versuche']} Versuche "
                    f"ausgeschöpft, {ENDZUSTAND_ERSCHOEPFT}"
                )
            continue

        ergebnis = db.submissions.find_one_and_update(
            {"_id": sub_id, "status": "RUNNING", "frist": {"$lt": jetzt}},
            {"$set": {"status": "PENDING", "run_token": None, "frist": None}},
            return_document=ReturnDocument.AFTER,
        )
        if ergebnis is None:
            # Ein Worker ist dazwischengekommen, siehe Kommentar oben an
            # _abgelaufene_uebernahmen. Nichts zu tun, sein Ergebnis gilt.
            continue

        redis_client.rpush(f"judge:{ergebnis['sprache']}", str(sub_id))
        erneut_eingereiht += 1
        print(
            f"Einreichung {sub_id}: Frist abgelaufen nach Versuch "
            f"{eintrag['versuche']}, erneut in judge:{ergebnis['sprache']} eingereiht"
        )

    print(
        f"Durchlauf fertig: {erneut_eingereiht} erneut eingereiht, {aufgegeben} aufgegeben"
    )


if __name__ == "__main__":
    durchlauf()
