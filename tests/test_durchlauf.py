"""Tests zum RPUSH-Fehler im Durchlauf (#151).

Ein nicht antwortendes Valkey darf den Lauf nicht beenden. Der Durchlauf
meldet den Fehler je Einreichung, setzt last_enqueued_at auf None wie
/submit seit #150 und arbeitet die restlichen Kandidaten ab.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import durchlauf


def _lauf(laufende, wartende, rpush, lpos=None):
    """Führt durchlauf() gegen gestellte Kandidaten aus und sammelt die
    update_one-Aufrufe ein. MongoClient und redis sind ersetzt, der Test
    braucht keine laufenden Dienste."""
    db = mock.MagicMock()
    db.submissions.find.side_effect = lambda filter, projektion: (
        laufende if filter.get("status") == "RUNNING" else wartende
    )
    db.submissions.find_one_and_update.side_effect = lambda filter, update, **kw: {
        "sprache": "python",
        "requeue_versuche": 1,
    }
    redis_ersatz = mock.MagicMock()
    redis_ersatz.Redis.from_url.return_value.rpush.side_effect = rpush
    redis_ersatz.Redis.from_url.return_value.lpos.return_value = lpos
    with (
        mock.patch.object(durchlauf, "MongoClient") as mongo,
        mock.patch.object(durchlauf, "redis", redis_ersatz),
    ):
        mongo.return_value.__getitem__.return_value = db
        durchlauf.durchlauf()
    return db.submissions.update_one.call_args_list


def test_erfolgreiche_requeues_zaehlen_weiter(capsys):
    # Der Erfolgspfad, damit eine Regression an der Zählung auffällt. Fast
    # jeder Test hier erwartet 0 erneut eingereiht, ohne diesen fiele ein
    # Zähler, der nie mehr hochzählt, durch keinen davon.
    alt = datetime.now(timezone.utc) - timedelta(hours=1)
    laufende = [{"_id": "a" * 24, "sprache": "python", "versuche": 0}]
    # lpos 0 heißt, der Eintrag steht noch in der Liste. Die wartende
    # Einreichung wird übersprungen, ohne Requeue und ohne Zähler.
    wartende = [
        {
            "_id": "b" * 24,
            "sprache": "python",
            "requeue_versuche": 0,
            "last_enqueued_at": alt,
        },
    ]

    updates = _lauf(laufende, wartende, None, lpos=0)

    ausgabe = capsys.readouterr().out
    assert "Durchlauf fertig: 1 erneut eingereiht" in ausgabe
    assert updates == []


def test_rpush_fehler_beendet_den_lauf_nicht(capsys):
    laufende = [
        {"_id": "a" * 24, "sprache": "python", "versuche": 0},
        {"_id": "b" * 24, "sprache": "python", "versuche": 0},
    ]

    updates = _lauf(laufende, [], ConnectionError("Valkey antwortet nicht"))

    ausgabe = capsys.readouterr().out
    # Beide Kandidaten kommen dran, der erste Fehler bricht den Lauf nicht ab.
    assert ausgabe.count("RPUSH ohne Bestätigung") == 2
    assert "Durchlauf fertig: 0 erneut eingereiht" in ausgabe
    # last_enqueued_at auf None wie in #150, der nächste Lauf prüft die
    # Einreichung dann sofort per LPOS statt REENQUEUE_AFTER_SECONDS zu warten.
    gesetzt = [
        aufruf.args[0]["_id"]
        for aufruf in updates
        if aufruf.args[1] == {"$set": {"last_enqueued_at": None}}
    ]
    assert gesetzt == ["a" * 24, "b" * 24]


def test_lpos_fehler_beendet_den_lauf_nicht(capsys):
    alt = datetime.now(timezone.utc) - timedelta(hours=1)
    wartende = [
        {
            "_id": "d" * 24,
            "sprache": "python",
            "requeue_versuche": 0,
            "last_enqueued_at": alt,
        },
        {
            "_id": "e" * 24,
            "sprache": "python",
            "requeue_versuche": 0,
            "last_enqueued_at": alt,
        },
    ]
    db = mock.MagicMock()
    db.submissions.find.side_effect = lambda filter, projektion: (
        [] if filter.get("status") == "RUNNING" else wartende
    )
    redis_ersatz = mock.MagicMock()
    redis_ersatz.Redis.from_url.return_value.lpos.side_effect = ConnectionError(
        "Valkey antwortet nicht"
    )
    with (
        mock.patch.object(durchlauf, "MongoClient") as mongo,
        mock.patch.object(durchlauf, "redis", redis_ersatz),
    ):
        mongo.return_value.__getitem__.return_value = db
        durchlauf.durchlauf()

    ausgabe = capsys.readouterr().out
    # Beide Kandidaten gemeldet, der Lauf endet mit der Zusammenfassung. Ohne
    # Antwort von Valkey bleibt die Einreichung unangetastet, der nächste Lauf
    # prüft erneut.
    assert ausgabe.count("LPOS ohne Antwort") == 2
    assert "Durchlauf fertig: 0 erneut eingereiht" in ausgabe
    assert not db.submissions.find_one_and_update.called


def test_gescheiterter_rpush_verbraucht_keinen_requeue_im_selben_lauf():
    # Der None-Marker aus dem RUNNING-Zweig darf die Einreichung nicht noch im
    # selben Lauf in die PENDING-Suche spülen. Sonst zahlt sie für denselben
    # Fehler sofort einen requeue_versuche-Zähler, den erst der nächste Lauf
    # verbrauchen soll. Der Fake bildet dafür den Zustand der Einreichung ab,
    # statische Listen sähen den Übergang nicht.
    alt = datetime.now(timezone.utc) - timedelta(hours=1)
    doc = {
        "_id": "f" * 24,
        "status": "RUNNING",
        "sprache": "python",
        "versuche": 0,
        "requeue_versuche": 0,
        "last_enqueued_at": alt,
    }

    def find(filter, projektion):
        if filter.get("status") == "RUNNING":
            return [dict(doc)] if doc["status"] == "RUNNING" else []
        return (
            [dict(doc)]
            if doc["status"] == "PENDING" and doc["last_enqueued_at"] is None
            else []
        )

    def find_one_and_update(filter, update, **kw):
        if doc["status"] != filter["status"]:
            return None
        for feld, wert in update.get("$set", {}).items():
            doc[feld] = wert
        for feld, wert in update.get("$inc", {}).items():
            doc[feld] += wert
        return dict(doc)

    def update_one(filter, update):
        for feld, wert in update["$set"].items():
            doc[feld] = wert

    db = mock.MagicMock()
    db.submissions.find.side_effect = find
    db.submissions.find_one_and_update.side_effect = find_one_and_update
    db.submissions.update_one.side_effect = update_one
    redis_ersatz = mock.MagicMock()
    redis_ersatz.Redis.from_url.return_value.rpush.side_effect = ConnectionError(
        "Valkey antwortet nicht"
    )
    redis_ersatz.Redis.from_url.return_value.lpos.return_value = None
    with (
        mock.patch.object(durchlauf, "MongoClient") as mongo,
        mock.patch.object(durchlauf, "redis", redis_ersatz),
    ):
        mongo.return_value.__getitem__.return_value = db
        durchlauf.durchlauf()

    assert redis_ersatz.Redis.from_url.return_value.rpush.call_count == 1
    assert doc["requeue_versuche"] == 0
    assert doc["last_enqueued_at"] is None


def test_fehler_am_none_marker_beendet_den_lauf_nicht(capsys):
    laufende = [{"_id": "g" * 24, "sprache": "python", "versuche": 0}]
    db = mock.MagicMock()
    db.submissions.find.side_effect = lambda filter, projektion: (
        laufende if filter.get("status") == "RUNNING" else []
    )
    db.submissions.find_one_and_update.return_value = {"sprache": "python"}
    db.submissions.update_one.side_effect = ConnectionError("Primary-Wahl läuft")
    redis_ersatz = mock.MagicMock()
    redis_ersatz.Redis.from_url.return_value.rpush.side_effect = ConnectionError(
        "Valkey antwortet nicht"
    )
    with (
        mock.patch.object(durchlauf, "MongoClient") as mongo,
        mock.patch.object(durchlauf, "redis", redis_ersatz),
    ):
        mongo.return_value.__getitem__.return_value = db
        durchlauf.durchlauf()

    ausgabe = capsys.readouterr().out
    # Auch wenn der Marker nicht geschrieben werden kann, läuft der Lauf zu
    # Ende. Die Einreichung greift dann erst über den $lt-Zweig des nächsten
    # Laufs.
    assert "RPUSH ohne Bestätigung" in ausgabe
    assert "last_enqueued_at nicht auf None" in ausgabe
    assert "Durchlauf fertig: 0 erneut eingereiht" in ausgabe


def test_rpush_fehler_beim_requeue_beendet_den_lauf_nicht(capsys):
    alt = datetime.now(timezone.utc) - timedelta(hours=1)
    wartende = [
        {
            "_id": "c" * 24,
            "sprache": "python",
            "requeue_versuche": 0,
            "last_enqueued_at": alt,
        },
    ]

    updates = _lauf([], wartende, ConnectionError("Valkey antwortet nicht"))

    ausgabe = capsys.readouterr().out
    assert "RPUSH ohne Bestätigung" in ausgabe
    assert "Durchlauf fertig: 0 erneut eingereiht" in ausgabe
    gesetzt = [
        aufruf.args[0]["_id"]
        for aufruf in updates
        if aufruf.args[1] == {"$set": {"last_enqueued_at": None}}
    ]
    assert gesetzt == ["c" * 24]
