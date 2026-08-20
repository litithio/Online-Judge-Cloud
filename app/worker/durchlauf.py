#!/usr/bin/env python3
"""Durchlauf aus #82, erweitert um #113: nimmt Einreichungen zurück, deren
Warten auf ein Ergebnis oder auf einen Worker ohne Fortschritt blieb.

Das deckt drei Fälle ab, die für die Queue gleich aussehen:

* RUNNING mit abgelaufener Frist (#82): ein Umgebungsfehler, den der Worker
  absichtlich nicht selbst beantwortet (#78, #52), oder ein Worker-Pod, der
  gestorben ist oder hängt, ohne den XACK-losen Fall zu melden. Beides bleibt
  als RUNNING ohne Ergebnis stehen, bis die Frist abläuft.
* PENDING ohne frischen Queue-Eintrag (#113): der Eintrag in Valkey ging
  verloren, etwa weil das Volume seinen Inhalt verliert, weil die API zwischen
  dem Insert und dem RPUSH stirbt, oder weil ein Worker den Eintrag per BLPOP
  schon zog, aber vor der bedingten Übernahme in MongoDB starb (#85). Dazu der
  Fall, dass der RPUSH der API ohne Bestätigung blieb. Die Einreichung trägt
  dann last_enqueued_at None (app/backend/main.py) und wartet nicht erst eine
  Frist ab, geprüft wird sie wie alle anderen. Ohne Queue-Eintrag bleibt so eine
  Einreichung sonst für immer auf PENDING stehen, ohne dass je eine Frist zu
  laufen beginnt. last_enqueued_at allein kann das nicht von einer
  Einreichung unterscheiden, die nur hinter einem Rückstau wartet, ihr
  Eintrag steht dabei ja noch in Valkey; erst LPOS gegen die jeweilige Liste
  unten trennt beide Fälle.

Läuft einmal und beendet sich. Im Cluster als CronJob
(app/chart/templates/judge.yaml), lokal von Hand über
`docker compose run --rm worker python3 durchlauf.py`.
"""

import os
from datetime import datetime, timedelta, timezone

import redis
from pymongo import MongoClient, ReturnDocument

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
REDIS_URI = os.getenv("REDIS_URI", "redis://localhost:6379")

# n aus der Beschlusslage zu #82: wie viele Versuche eine Einreichung
# bekommt, bevor sie als nicht beurteilt gilt statt erneut eingereiht zu
# werden. Gilt für zwei eigene Zähler, nicht denselben: versuche (worker.py)
# zählt tatsächliche Übernahmen für _abgelaufene_uebernahmen unten,
# requeue_versuche zählt Requeues durch _verwaiste_pending für #113. Ein
# Queue-Eintrag, der verlorenging, bevor ihn ein Worker zog, erreicht nie
# eine Übernahme, versuche bliebe für ihn also für immer 0 und MAX_VERSUCHE
# griffe nie.
MAX_VERSUCHE = int(os.getenv("MAX_VERSUCHE", "3"))

# Aus #113: wie lange eine Einreichung auf PENDING liegen darf, bevor der
# Durchlauf sie als Kandidatin für einen verlorenen Queue-Eintrag prüft (den
# eigentlichen Verlust stellt erst der LPOS-Check gegen Valkey fest, siehe
# unten). Muss über der
# normalen Wartezeit einer gesunden Einreichung liegen, sonst reiht der
# Durchlauf sie ein zweites Mal ein, während sie nur auf ihren Worker wartet:
# KEDA pollt die Queue ohne eigenes pollingInterval alle 30 Sekunden
# (keda-scaledobject-python.yaml), dazu kommt die Zeit, bis der Worker-Pod
# aus minReplicaCount 0 startet und das Image zieht.
REENQUEUE_AFTER_SECONDS = int(os.getenv("REENQUEUE_AFTER_SECONDS", "180"))

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


def _ohne_frischen_eintrag(schwelle):
    """Die $or-Zweige über last_enqueued_at, gemeinsam für die Suche unten und
    für die beiden bedingten Updates in durchlauf(). An einer Stelle, weil ein
    Zweig, der nur in der Suche steht, im Update fehlt und die gefundene
    Einreichung dort dann nie greift.

    Zwei Zweige, weil MongoDB bei $lt nur innerhalb desselben BSON-Typs
    vergleicht. Gegen MongoDB 8.0.28 gemessen findet $lt gegen ein Datum von
    vier Dokumenten mit altem Datum, neuem Datum, null und fehlendem Feld nur
    das mit altem Datum. Null steht dort, wo der RPUSH schon bei der Anlage
    ohne Bestätigung blieb (app/backend/main.py). Ob die ID trotzdem in der
    Liste steht, ist damit offen, die Verbindung kann nach der Ausführung
    abgebrochen sein. Entschieden wird das wie bei jedem anderen Kandidaten
    unten per LPOS, nur wartet diese Einreichung keine
    REENQUEUE_AFTER_SECONDS ab.

    $type null und nicht die Gleichheit {"last_enqueued_at": None}. Die
    Gleichheit trifft in MongoDB auch Einreichungen ganz ohne das Feld, und
    die haben aus derselben Zeit auch kein requeue_versuche. Die Schleife
    unten liest es über eintrag[...] ohne Rückfall, ein Treffer dieser Art
    beendete den Durchlauf also mit einem KeyError, bevor der Rest der Liste
    dran wäre. Diese Einreichungen holt nachtrag_113.py in die Daten, nicht
    dieser Filter.
    """
    return [
        {"last_enqueued_at": {"$lt": schwelle}},
        {"last_enqueued_at": {"$type": "null"}},
    ]


def _verwaiste_pending(db, schwelle):
    """Findet Kandidaten für #113: PENDING, deren Queue-Eintrag seit
    REENQUEUE_AFTER_SECONDS nicht erneuert wurde oder ohne Bestätigung blieb.
    Nur Kandidaten, noch keine Entscheidung: Rückstau sieht hier genauso aus
    wie Verlust, das trennt erst der LPOS-Check gegen Valkey in durchlauf().
    Wie bei _abgelaufene_uebernahmen entscheidet der bedingte Update dort je
    Einreichung erneut, gegen denselben Zeitpunkt schwelle: Ein Worker kann
    zwischen diesem find und dem Update noch übernommen haben."""
    return db.submissions.find(
        {"status": "PENDING", "$or": _ohne_frischen_eintrag(schwelle)},
        {"_id": 1, "sprache": 1, "requeue_versuche": 1, "last_enqueued_at": 1},
    )


def durchlauf():
    db = MongoClient(MONGO_URI)["coding_platform"]
    redis_client = redis.Redis.from_url(REDIS_URI)
    jetzt = datetime.now(timezone.utc)
    schwelle = jetzt - timedelta(seconds=REENQUEUE_AFTER_SECONDS)

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
            {
                "$set": {
                    "status": "PENDING",
                    "run_token": None,
                    "frist": None,
                    # Zählt für #113 als frischer Queue-Eintrag, sonst griffe
                    # die PENDING-Suche unten sofort wieder auf dieselbe
                    # Einreichung zu.
                    "last_enqueued_at": jetzt,
                }
            },
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

    # Zweite Suche aus #113, siehe Modul-Docstring: PENDING-Einreichungen, deren
    # Queue-Eintrag verlorenging statt an einer abgelaufenen Frist zu hängen.
    for eintrag in list(_verwaiste_pending(db, schwelle)):
        sub_id = eintrag["_id"]

        # last_enqueued_at allein sagt nur "seit wann PENDING", nicht ob der
        # Eintrag noch aussteht oder verloren ist: Ein Rückstau vor ihm sieht
        # genauso alt aus wie ein Verlust. LPOS fragt stattdessen Valkey
        # selbst, ob die ID noch in der Liste steht. MongoDB bleibt dabei die
        # einzige Quelle für den Zustand der Einreichung, das hier ist nur eine
        # Anfrage an die Warteschlange nach ihrem eigenen Inhalt, kein zweiter
        # Ort für diesen Zustand. Noch in der Liste: nichts zu tun, auch nicht
        # an requeue_versuche, der nächste Lauf prüft erneut.
        if redis_client.lpos(f"judge:{eintrag['sprache']}", str(sub_id)) is not None:
            continue

        if eintrag["requeue_versuche"] >= MAX_VERSUCHE:
            ergebnis = db.submissions.find_one_and_update(
                {
                    "_id": sub_id,
                    "status": "PENDING",
                    "$or": _ohne_frischen_eintrag(schwelle),
                },
                {"$set": {"status": ENDZUSTAND_ERSCHOEPFT}},
            )
            if ergebnis is not None:
                aufgegeben += 1
                print(
                    f"Einreichung {sub_id}: {eintrag['requeue_versuche']} Requeues "
                    f"ausgeschöpft, nie aus der Queue übernommen, {ENDZUSTAND_ERSCHOEPFT}"
                )
            continue

        ergebnis = db.submissions.find_one_and_update(
            {
                "_id": sub_id,
                "status": "PENDING",
                "$or": _ohne_frischen_eintrag(schwelle),
            },
            {
                "$set": {"last_enqueued_at": jetzt},
                "$inc": {"requeue_versuche": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if ergebnis is None:
            # Ein Worker hat inzwischen übernommen, oder ein anderer
            # Durchlauf-Lauf war schneller. Siehe Kommentar oben an
            # _verwaiste_pending.
            continue

        redis_client.rpush(f"judge:{ergebnis['sprache']}", str(sub_id))
        erneut_eingereiht += 1
        # Beide Zweige von _ohne_frischen_eintrag landen hier, die Meldung
        # nennt den, der zutraf. Ohne die Unterscheidung stünde über einer
        # gerade erst angelegten Einreichung, sie warte seit über
        # REENQUEUE_AFTER_SECONDS.
        grund = (
            "RPUSH bei der Anlage unbestätigt"
            if eintrag["last_enqueued_at"] is None
            else f"seit über {REENQUEUE_AFTER_SECONDS}s PENDING ohne Queue-Eintrag"
        )
        print(
            f"Einreichung {sub_id}: {grund}, erneut in "
            f"judge:{ergebnis['sprache']} eingereiht "
            f"(Requeue {ergebnis['requeue_versuche']} von {MAX_VERSUCHE})"
        )

    print(
        f"Durchlauf fertig: {erneut_eingereiht} erneut eingereiht, {aufgegeben} aufgegeben"
    )


if __name__ == "__main__":
    durchlauf()
