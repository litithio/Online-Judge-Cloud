#!/usr/bin/env python3
"""Einmalige Migration für #113: Einreichungen von vor last_enqueued_at und
requeue_versuche, dazu PENDING-Einreichungen in Sprachen, die es nicht mehr
gibt.

_verwaiste_pending (durchlauf.py) greift auf last_enqueued_at nur lesend zu,
über $lt gegen eine Schwelle und über $type null. Ein fehlendes Feld findet
keiner der beiden Zweige. MongoDB vergleicht bei $lt nur innerhalb desselben
BSON-Typs, gegen MongoDB 8.0.28 gemessen findet $lt gegen ein Datum weder null
noch ein fehlendes Feld, und $type null verlangt das Feld. Eine Einreichung
ohne last_enqueued_at bliebe damit für den Durchlauf unsichtbar.
requeue_versuche und versuche liest durchlauf.py über eintrag["..."] ohne
Rückfall, ein fehlendes Feld dort wäre ein KeyError, nicht ein stiller
Rückfall auf 0. Diese Migration schließt beide Lücken einmalig in den Daten,
nicht im Code. Die gelesenen Felder bleiben Pflichtfelder, die jede neue
Einreichung ohnehin schon bekommt (app/backend/main.py).

Läuft einmal und beendet sich, danach ist sie wirkungslos: Jede Bedingung
prüft $exists false oder eine feste Liste alter Sprachen, ein zweiter Lauf
träfe keine Einreichung mehr. Lokal über
`docker compose --profile migration run --rm nachtrag-113`, im Cluster als
Job neben dem Seed (ansible/tasks/seed.yaml).
"""

import os

from pymongo import MongoClient

# Kein Seiteneffekt beim Import: MongoClient entsteht erst in durchlauf(),
# nicht auf Modulebene, der Import unten verbindet also nichts.
from durchlauf import ENDZUSTAND_ERSCHOEPFT

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

# python bleibt die einzige Sprache mit einem Worker (app/backend/main.py,
# SPRACHEN). Diese drei gab es einmal (#73), zu ihnen läuft aber kein Worker
# mehr: Ihre PENDING-Einreichungen kämen nie über #113 hinaus, blieben sie
# als PENDING stehen, denn nichts holt sie je aus der Queue.
UNTERSTUETZTE_SPRACHE = "python"
ENTFERNTE_SPRACHEN = ["java", "cpp", "rust"]


def migrieren():
    db = MongoClient(MONGO_URI)["coding_platform"]

    # last_enqueued_at aus created_at nachgetragen, nur wo es fehlt. Als
    # Aggregations-Pipeline-Update, damit jede Einreichung ihren eigenen
    # created_at bekommt statt eines gemeinsamen Zeitpunkts für alle.
    frist_ergebnis = db.submissions.update_many(
        {
            "status": "PENDING",
            "sprache": UNTERSTUETZTE_SPRACHE,
            "last_enqueued_at": {"$exists": False},
        },
        [{"$set": {"last_enqueued_at": "$created_at"}}],
    )
    print(f"last_enqueued_at nachgetragen: {frist_ergebnis.modified_count}")

    # requeue_versuche auf 0, nur wo es fehlt. Auf python beschränkt wie oben:
    # Einreichungen in einer entfernten Sprache bekommen unten ohnehin einen
    # Endzustand statt dieses Felds.
    requeue_ergebnis = db.submissions.update_many(
        {
            "status": "PENDING",
            "sprache": UNTERSTUETZTE_SPRACHE,
            "requeue_versuche": {"$exists": False},
        },
        {"$set": {"requeue_versuche": 0}},
    )
    print(f"requeue_versuche nachgetragen (PENDING): {requeue_ergebnis.modified_count}")

    # versuche auf 0, nur wo es fehlt, für RUNNING: derselbe fehlende
    # Rückfall in _abgelaufene_uebernahmen (durchlauf.py) träfe sonst
    # denselben KeyError, nur auf dem anderen der beiden Durchlauf-Pfade.
    versuche_ergebnis = db.submissions.update_many(
        {"status": "RUNNING", "versuche": {"$exists": False}},
        {"$set": {"versuche": 0}},
    )
    print(f"versuche nachgetragen (RUNNING): {versuche_ergebnis.modified_count}")

    # PENDING-Einreichungen in einer entfernten Sprache: kein Worker führt sie
    # je aus, sie blieben sonst für immer PENDING. ENDZUSTAND_ERSCHOEPFT statt
    # PENDING, ohne last_enqueued_at und ohne requeue_versuche, damit #113 sie
    # nicht mehr als Kandidatin sieht.
    sprachen_ergebnis = db.submissions.update_many(
        {"status": "PENDING", "sprache": {"$in": ENTFERNTE_SPRACHEN}},
        {"$set": {"status": ENDZUSTAND_ERSCHOEPFT}},
    )
    print(
        f"PENDING in entfernten Sprachen ({', '.join(ENTFERNTE_SPRACHEN)}) auf "
        f"{ENDZUSTAND_ERSCHOEPFT}: {sprachen_ergebnis.modified_count}"
    )


if __name__ == "__main__":
    migrieren()
