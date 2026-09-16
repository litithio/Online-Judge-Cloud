"""Test dazu, welche Testfallnamen die Ergebnisseite zeigt (#208).

Seit #71 steht der Name des Testfalls in der Ergebniszeile. Namen wie "Nur
der Startwert 1" nennen die Eingabe, die das Urteil seit #208 nicht mehr
verrät. Die Seite zeigt den Namen deshalb nur für Beispiele, jeder andere
Fall heißt "Testfall N".
"""

from datetime import datetime, timezone

from bson import ObjectId

import fakes
import main

BENUTZER = {"sub": "benutzer-a", "preferred_username": "a"}


def test_nur_beispiele_zeigen_ihren_namen(monkeypatch):
    sub_id = ObjectId()
    task_id = ObjectId()
    ergebnis = {
        "verdict": "AC",
        "detail": "bestanden",
        "zeit_ms": 9,
        "speicher_kb": 9400,
    }
    sub = {
        "_id": sub_id,
        "user_id": BENUTZER["sub"],
        "task_id": str(task_id),
        "status": "SUCCESS",
        "result": "3 von 3 Testfällen bestanden",
        "sprache": "python",
        "created_at": datetime.now(timezone.utc),
        "test_results": [{"test_id": i, **ergebnis} for i in (1, 2, 3)],
    }
    aufgabe = {
        "_id": task_id,
        "title": "Collatz",
        "test_cases": [
            {"name": "Nur der Startwert 1"},
            {"name": "Startwerte bis 10", "sample": True},
            # sample false ausdrücklich, laden.py erlaubt beide Schreibweisen.
            {"name": "Startwerte bis eine Million", "sample": False},
        ],
    }
    monkeypatch.setattr(
        main, "db", fakes.FakeDb(aufgaben=[aufgabe], einreichungen=[sub])
    )

    antwort = main.einreichung_seite(str(sub_id), fakes.anfrage(), user=BENUTZER)

    assert antwort.status_code == 200
    seite = antwort.body.decode()
    assert "Startwerte bis 10" in seite
    assert "Nur der Startwert 1" not in seite
    assert "Startwerte bis eine Million" not in seite
    assert "Testfall 1" in seite
    assert "Testfall 3" in seite
