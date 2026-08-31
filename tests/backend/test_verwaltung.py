"""Tests zur Verwaltung (#240): Rollenprüfung, Aufgabe anlegen, alle
Einreichungen sehen.

Wie test_einreichung_zugriff.py laufen die Routen als Funktionen gegen die
Fake-Datenbank, ohne HTTP-Client. verwaltung_aufgabe_anlegen ist async (sie
liest request.form()), asyncio.run genügt dafür, eine eigene
pytest-asyncio-Abhängigkeit nur für diese eine Route lohnt sich nicht.
"""

import asyncio
from datetime import datetime, timezone

import fakes
import main

ADMIN = {"sub": "lehrende-a", "preferred_username": "Dozentin", "roles": ["dozent"]}
STUDENT = {"sub": "studi-a", "preferred_username": "Studi A", "roles": []}
# So sieht ein user-Objekt ohne roles-Feld aus, wie es ältere Aufrufer noch
# bauen (siehe test_seiten_rendern.py). _ist_admin darf daran nicht scheitern
# und muss es als nicht-dozent behandeln.
STUDENT_OHNE_ROLLENFELD = {"sub": "studi-b", "preferred_username": "Studi B"}

GUELTIGE_AUFGABE = {
    "titel": "Römische Zahlen",
    "schwierigkeit": "leicht",
    "beschreibung": "Zahl in römische Schreibweise umwandeln.",
    "tc1_name": "Beispiel",
    "tc1_eingabe": "9",
    "tc1_erwartet": "IX",
}


def _fake_einreichung(user_id, username, status="SUCCESS"):
    return {
        "_id": main.ObjectId(),
        "user_id": user_id,
        "username": username,
        "task_id": "0" * 24,
        "sprache": "python",
        "status": status,
        "result": "bestanden" if status == "SUCCESS" else None,
        "test_results": [] if status == "SUCCESS" else None,
        "created_at": datetime.now(timezone.utc),
    }


def _leere_db(monkeypatch, **kwargs):
    monkeypatch.setattr(main, "db", fakes.FakeDb(**kwargs))
    monkeypatch.setattr(main, "redis_client", fakes.FakeRedis())


def test_dashboard_verweigert_ohne_rolle(monkeypatch):
    _leere_db(monkeypatch)
    antwort = main.verwaltung_seite(fakes.anfrage(), user=STUDENT)
    assert antwort.status_code == 403


def test_dashboard_verweigert_ohne_roles_feld(monkeypatch):
    _leere_db(monkeypatch)
    antwort = main.verwaltung_seite(fakes.anfrage(), user=STUDENT_OHNE_ROLLENFELD)
    assert antwort.status_code == 403


def test_dashboard_erlaubt_mit_rolle(monkeypatch):
    _leere_db(monkeypatch)
    antwort = main.verwaltung_seite(fakes.anfrage(), user=ADMIN)
    assert antwort.status_code == 200


def test_aufgabe_neu_formular_verweigert_ohne_rolle(monkeypatch):
    _leere_db(monkeypatch)
    antwort = main.verwaltung_aufgabe_neu_seite(fakes.anfrage(), user=STUDENT)
    assert antwort.status_code == 403


def test_aufgabe_neu_formular_erlaubt_mit_rolle(monkeypatch):
    _leere_db(monkeypatch)
    antwort = main.verwaltung_aufgabe_neu_seite(fakes.anfrage(), user=ADMIN)
    assert antwort.status_code == 200


def test_aufgabe_anlegen_verweigert_ohne_rolle(monkeypatch):
    _leere_db(monkeypatch)
    antwort = asyncio.run(
        main.verwaltung_aufgabe_anlegen(
            fakes.formular_anfrage(GUELTIGE_AUFGABE), user=STUDENT
        )
    )
    assert antwort.status_code == 403
    assert main.db.tasks.dokumente == []


def test_aufgabe_anlegen_legt_dokument_an(monkeypatch):
    _leere_db(monkeypatch)
    antwort = asyncio.run(
        main.verwaltung_aufgabe_anlegen(
            fakes.formular_anfrage(GUELTIGE_AUFGABE), user=ADMIN
        )
    )
    assert antwort.status_code == 303
    angelegt = main.db.tasks.dokumente
    assert len(angelegt) == 1
    assert angelegt[0]["title"] == "Römische Zahlen"
    assert angelegt[0]["difficulty"] == "leicht"
    assert angelegt[0]["test_cases"] == [
        {"name": "Beispiel", "input": "9", "expected_output": "IX"}
    ]
    # Optionale Limits fehlten im Formular, sollen also auch im Dokument
    # fehlen und nicht als None oder leere Zeichenkette stehen.
    assert "time_limit_seconds" not in angelegt[0]
    assert "memory_limit_mb" not in angelegt[0]


def test_aufgabe_ohne_titel_wird_abgelehnt(monkeypatch):
    _leere_db(monkeypatch)
    daten = GUELTIGE_AUFGABE | {"titel": ""}
    antwort = asyncio.run(
        main.verwaltung_aufgabe_anlegen(fakes.formular_anfrage(daten), user=ADMIN)
    )
    assert antwort.status_code == 400
    assert main.db.tasks.dokumente == []


def test_aufgabe_mit_unbekannter_schwierigkeit_wird_abgelehnt(monkeypatch):
    _leere_db(monkeypatch)
    daten = GUELTIGE_AUFGABE | {"schwierigkeit": "extrem"}
    antwort = asyncio.run(
        main.verwaltung_aufgabe_anlegen(fakes.formular_anfrage(daten), user=ADMIN)
    )
    assert antwort.status_code == 400
    assert main.db.tasks.dokumente == []


def test_aufgabe_ohne_testfall_wird_abgelehnt(monkeypatch):
    _leere_db(monkeypatch)
    daten = {k: v for k, v in GUELTIGE_AUFGABE.items() if not k.startswith("tc1_")}
    antwort = asyncio.run(
        main.verwaltung_aufgabe_anlegen(fakes.formular_anfrage(daten), user=ADMIN)
    )
    assert antwort.status_code == 400
    assert main.db.tasks.dokumente == []


def test_aufgabe_mit_limit_ausserhalb_der_grenze_wird_abgelehnt(monkeypatch):
    _leere_db(monkeypatch)
    daten = GUELTIGE_AUFGABE | {"time_limit_seconds": "999"}
    antwort = asyncio.run(
        main.verwaltung_aufgabe_anlegen(fakes.formular_anfrage(daten), user=ADMIN)
    )
    assert antwort.status_code == 400
    assert main.db.tasks.dokumente == []


def test_verwaltung_einreichungen_verweigert_ohne_rolle(monkeypatch):
    _leere_db(monkeypatch)
    antwort = main.verwaltung_einreichungen_seite(fakes.anfrage(), user=STUDENT)
    assert antwort.status_code == 403


def test_verwaltung_einreichungen_zeigt_alle_nutzer(monkeypatch):
    # Kern von #240: Studierende sehen nur die eigenen Einreichungen
    # (/einreichungen, user_id im Filter), die Verwaltung sieht alle -
    # hier zwei verschiedene user_id in einer einzigen Antwort.
    _leere_db(
        monkeypatch,
        einreichungen=[
            _fake_einreichung("studi-a", "Studi A", "SUCCESS"),
            _fake_einreichung("studi-b", "Studi B", "PENDING"),
        ],
    )
    antwort = main.verwaltung_einreichungen_seite(fakes.anfrage(), user=ADMIN)
    assert antwort.status_code == 200
    koerper = antwort.body.decode()
    assert "Studi A" in koerper
    assert "Studi B" in koerper


def test_verwaltung_einreichungen_filtert_nach_ergebnis(monkeypatch):
    _leere_db(
        monkeypatch,
        einreichungen=[
            _fake_einreichung("studi-a", "Studi A", "SUCCESS"),
            _fake_einreichung("studi-b", "Studi B", "PENDING"),
        ],
    )
    antwort = main.verwaltung_einreichungen_seite(
        fakes.anfrage(), user=ADMIN, ergebnis="bestanden"
    )
    koerper = antwort.body.decode()
    assert "Studi A" in koerper
    assert "Studi B" not in koerper


def test_verwaltung_einreichungen_filtert_nach_person(monkeypatch):
    _leere_db(
        monkeypatch,
        einreichungen=[
            _fake_einreichung("studi-a", "Studi A", "SUCCESS"),
            _fake_einreichung("studi-b", "Studi B", "PENDING"),
        ],
    )
    antwort = main.verwaltung_einreichungen_seite(
        fakes.anfrage(), user=ADMIN, person="studi-b"
    )
    koerper = antwort.body.decode()
    assert "Studi A" not in koerper
    assert "Studi B" in koerper


def test_einreichungen_seite_zeigt_weiterhin_nur_eigene(monkeypatch):
    # Regression zu #76: die Verwaltung darf /einreichungen selbst nicht
    # aufweichen, Studierende sehen dort weiterhin nur ihre eigenen.
    # einreichungen.html zeigt keine Nutzerspalte, darum über den Kontext der
    # TemplateResponse geprüft und nicht über den Seitentext.
    _leere_db(
        monkeypatch,
        einreichungen=[
            _fake_einreichung("studi-a", "Studi A", "SUCCESS"),
            _fake_einreichung("studi-b", "Studi B", "PENDING"),
        ],
    )
    antwort = main.einreichungen_seite(fakes.anfrage(), user=STUDENT)
    gezeigt = antwort.context["submissions"]
    assert len(gezeigt) == 1
    assert gezeigt[0]["user_id"] == "studi-a"
