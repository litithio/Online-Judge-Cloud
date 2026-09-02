"""Tests zur Spalte "Ihr Stand" und zum Einreichungs-Hinweis am Editor (#252).

main._stand_je_aufgabe steht unit-getestet für sich (die Aggregation über
mehrere Einreichungen), aufgaben_seite/aufgabe_seite nur noch dafür, dass sie
das Ergebnis tatsächlich ins Template tragen. Über find()+sort() wie
_einreichungen_liste, nicht über db.tasks.aggregate: das kennt
FakeCollection (fakes.py) nicht, aufgaben_seite/aufgabe_seite laufen hier
aber direkt gegen FakeDb.
"""

from datetime import datetime, timedelta, timezone

from bson import ObjectId

import fakes
import main

BENUTZER = {"sub": "benutzer-a", "preferred_username": "a"}
JETZT = datetime.now(timezone.utc)


def _einreichung(task_id, status, verdicts, vor):
    return {
        "_id": ObjectId(),
        "user_id": BENUTZER["sub"],
        "task_id": str(task_id),
        "status": status,
        "test_results": [{"verdict": v} for v in verdicts],
        "created_at": JETZT - vor,
    }


def test_ohne_einreichung_kein_eintrag(monkeypatch):
    monkeypatch.setattr(main, "db", fakes.FakeDb())
    assert main._stand_je_aufgabe(BENUTZER["sub"]) == {}
    assert main._stand_text(None) == "noch nicht abgegeben"
    assert main._stand_beste_text(None) is None


def test_beste_erscheint_nur_bei_abweichung(monkeypatch):
    # Erst 5 von 5, dann eine schlechtere Einreichung - die letzte zählt für
    # den Haupttext, die frühere bessere nur als Zusatz (Entscheidung #252).
    task_id = ObjectId()
    monkeypatch.setattr(
        main,
        "db",
        fakes.FakeDb(
            einreichungen=[
                _einreichung(task_id, "SUCCESS", ["AC"] * 5, timedelta(hours=2)),
                _einreichung(
                    task_id,
                    "FAILED",
                    ["AC", "AC", "WA", "NOT_RUN", "NOT_RUN"],
                    timedelta(minutes=5),
                ),
            ]
        ),
    )
    eintrag = main._stand_je_aufgabe(BENUTZER["sub"])[str(task_id)]
    assert main._stand_text(eintrag) == "2 von 5"
    assert main._stand_beste_text(eintrag) == "5 von 5"


def test_beste_bleibt_stumm_wenn_gleich_der_letzten(monkeypatch):
    task_id = ObjectId()
    monkeypatch.setattr(
        main,
        "db",
        fakes.FakeDb(
            einreichungen=[
                _einreichung(task_id, "SUCCESS", ["AC"] * 3, timedelta(hours=1))
            ]
        ),
    )
    eintrag = main._stand_je_aufgabe(BENUTZER["sub"])[str(task_id)]
    assert main._stand_text(eintrag) == "3 von 3"
    assert main._stand_beste_text(eintrag) is None


def test_laufende_letzte_zeigt_trotzdem_die_bessere_frühere(monkeypatch):
    # Die letzte Einreichung läuft noch (kein Punktstand), eine frühere
    # fertige war schon mal besser - der Hinweis darf das zeigen.
    task_id = ObjectId()
    monkeypatch.setattr(
        main,
        "db",
        fakes.FakeDb(
            einreichungen=[
                _einreichung(task_id, "SUCCESS", ["AC"] * 4, timedelta(hours=3)),
                _einreichung(task_id, "PENDING", [], timedelta(minutes=1)),
            ]
        ),
    )
    eintrag = main._stand_je_aufgabe(BENUTZER["sub"])[str(task_id)]
    assert main._stand_text(eintrag) == "in der Warteschlange"
    assert main._stand_beste_text(eintrag) == "4 von 4"


def test_andere_aufgabe_bleibt_unberuehrt(monkeypatch):
    # task_id grenzt ein (aufgabe_seite): eine Einreichung zu einer fremden
    # Aufgabe darf im Ergebnis für die angefragte nicht auftauchen.
    eigene, fremde = ObjectId(), ObjectId()
    monkeypatch.setattr(
        main,
        "db",
        fakes.FakeDb(
            einreichungen=[
                _einreichung(eigene, "SUCCESS", ["AC"], timedelta(hours=1)),
                _einreichung(fremde, "SUCCESS", ["AC"] * 2, timedelta(hours=1)),
            ]
        ),
    )
    stand = main._stand_je_aufgabe(BENUTZER["sub"], task_id=str(eigene))
    assert list(stand) == [str(eigene)]


def test_aufgabenseite_zeigt_stand_und_beste_zusatz(monkeypatch):
    task_id = ObjectId()
    aufgabe = {"_id": task_id, "title": "Zweisumme", "description": "Text"}
    monkeypatch.setattr(
        main,
        "db",
        fakes.FakeDb(
            aufgaben=[aufgabe],
            einreichungen=[
                _einreichung(task_id, "SUCCESS", ["AC"] * 5, timedelta(hours=2)),
                _einreichung(
                    task_id, "FAILED", ["AC", "AC", "WA"], timedelta(minutes=5)
                ),
            ],
        ),
    )
    antwort = main.aufgaben_seite(fakes.anfrage(), user=BENUTZER)
    seite = antwort.body.decode()
    assert "2 von 3" in seite
    assert "beste: 5 von 5" in seite


def test_aufgabenseite_ohne_einreichung_zeigt_wartet(monkeypatch):
    aufgabe = {"_id": ObjectId(), "title": "Neue Aufgabe", "description": "Text"}
    monkeypatch.setattr(main, "db", fakes.FakeDb(aufgaben=[aufgabe]))
    antwort = main.aufgaben_seite(fakes.anfrage(), user=BENUTZER)
    assert "noch nicht abgegeben" in antwort.body.decode()


def test_editor_hinweis_zeigt_letzte_einreichung(monkeypatch):
    task_id = ObjectId()
    aufgabe = {"_id": task_id, "title": "Zweisumme"}
    monkeypatch.setattr(
        main,
        "db",
        fakes.FakeDb(
            aufgaben=[aufgabe],
            einreichungen=[
                _einreichung(task_id, "SUCCESS", ["AC"] * 5, timedelta(hours=2)),
                _einreichung(
                    task_id, "FAILED", ["AC", "AC", "WA"], timedelta(minutes=5)
                ),
            ],
        ),
    )
    antwort = main.aufgabe_seite(str(task_id), fakes.anfrage(), user=BENUTZER)
    seite = antwort.body.decode()
    assert "Ihre letzte Einreichung:" in seite
    assert "2 von 3 Testfällen" in seite
    assert "(beste: 5 von 5)" in seite


def test_editor_hinweis_fehlt_ohne_einreichung(monkeypatch):
    task_id = ObjectId()
    aufgabe = {"_id": task_id, "title": "Zweisumme"}
    monkeypatch.setattr(main, "db", fakes.FakeDb(aufgaben=[aufgabe]))
    antwort = main.aufgabe_seite(str(task_id), fakes.anfrage(), user=BENUTZER)
    assert "Ihre letzte Einreichung" not in antwort.body.decode()
