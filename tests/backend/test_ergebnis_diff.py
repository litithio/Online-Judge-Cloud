"""Tests zum Diff-Block (Eingabe/Erwartet/Erhalten) bei WA (#252).

worker.py schreibt eingabe/erwartet/erhalten seit #252 zusätzlich zu detail,
aber nur bei WA - eine ältere Einreichung von davor trägt nur detail als
fertigen Satz. _testfaelle_ansicht muss beide Formen tragen, ohne für
Altdaten abzustürzen.
"""

from datetime import datetime, timezone

from bson import ObjectId

import fakes
import main

BENUTZER = {"sub": "benutzer-a", "preferred_username": "a"}


def _einreichung_seite(monkeypatch, test_results):
    sub_id = ObjectId()
    task_id = ObjectId()
    sub = {
        "_id": sub_id,
        "user_id": BENUTZER["sub"],
        "task_id": str(task_id),
        "status": "FAILED",
        "result": "1 von 1 Testfällen bestanden",
        "sprache": "python",
        "created_at": datetime.now(timezone.utc),
        "test_results": test_results,
    }
    aufgabe = {
        "_id": task_id,
        "title": "Zweisumme",
        "test_cases": [{"name": "Negative Zahlen"}],
    }
    monkeypatch.setattr(
        main, "db", fakes.FakeDb(aufgaben=[aufgabe], einreichungen=[sub])
    )
    antwort = main.einreichung_seite(str(sub_id), fakes.anfrage(), user=BENUTZER)
    assert antwort.status_code == 200
    return antwort.body.decode()


def test_wa_mit_neuen_feldern_zeigt_diff_block(monkeypatch):
    seite = _einreichung_seite(
        monkeypatch,
        [
            {
                "test_id": 1,
                "verdict": "WA",
                "detail": "Erwartet '1 3', bekommen '0 2'",
                "eingabe": "-3 4 -1 8, Ziel 5",
                "erwartet": "1 3",
                "erhalten": "0 2",
                "zeit_ms": 11,
                "speicher_kb": 9600,
            }
        ],
    )
    assert '<div class="diff">' in seite
    assert "<em>Eingabe</em>-3 4 -1 8, Ziel 5" in seite
    assert "<em>Erwartet</em>1 3" in seite
    assert '<span class="ist">0 2</span>' in seite
    # Der alte Satz aus detail erscheint nicht zusätzlich als zusatz-Zeile.
    assert "bekommen" not in seite


def test_wa_ohne_neue_felder_faellt_auf_detail_zurueck(monkeypatch):
    # Stand einer Einreichung von vor #252: nur detail, kein eingabe/erwartet/
    # erhalten. Die Seite darf dafür weder abstürzen noch den Diff-Block
    # zeigen, sondern wie bisher den freien Satz.
    seite = _einreichung_seite(
        monkeypatch,
        [
            {
                "test_id": 1,
                "verdict": "WA",
                "detail": "Erwartet '1 3', bekommen '0 2'",
                "zeit_ms": 11,
                "speicher_kb": 9600,
            }
        ],
    )
    assert '<div class="diff">' not in seite
    assert "Erwartet &#39;1 3&#39;, bekommen &#39;0 2&#39;" in seite


def test_bestandener_testfall_zeigt_weder_diff_noch_zusatz(monkeypatch):
    seite = _einreichung_seite(
        monkeypatch,
        [
            {
                "test_id": 1,
                "verdict": "AC",
                "detail": "bestanden",
                "zeit_ms": 9,
                "speicher_kb": 9400,
            }
        ],
    )
    assert '<div class="diff">' not in seite
    assert '<span class="zusatz">' not in seite


def test_zeitlimit_zeigt_weiterhin_nur_zusatz(monkeypatch):
    # TLE/MLE/RE/OLE haben keine erwartete/erhaltene Ausgabe zum Vergleichen,
    # detail bleibt dort der einzige Text.
    seite = _einreichung_seite(
        monkeypatch,
        [
            {
                "test_id": 1,
                "verdict": "TLE",
                "detail": "Abgebrochen beim Erreichen des Zeitlimits",
                "zeit_ms": 2000,
                "speicher_kb": 49254,
            }
        ],
    )
    assert '<div class="diff">' not in seite
    assert "Abgebrochen beim Erreichen des Zeitlimits" in seite


def test_verborgener_wa_zeigt_keinen_diff_block(monkeypatch):
    # Stand seit #208: ein verborgener Testfall trägt bei WA nur detail,
    # die Seite zeigt den Satz und keinen Diff-Block.
    seite = _einreichung_seite(
        monkeypatch,
        [
            {
                "test_id": 1,
                "verdict": "WA",
                "detail": "Testfall nicht einsehbar",
                "zeit_ms": 11,
                "speicher_kb": 9600,
            }
        ],
    )
    assert '<div class="diff">' not in seite
    assert "Testfall nicht einsehbar" in seite
