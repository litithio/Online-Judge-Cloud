"""Tests zum Zugriff auf fremde Einreichungen (#76).

Die Routen laufen hier als Funktionen mit der Fake-Datenbank aus fakes.py
statt über einen HTTP-Client. Geprüft wird der Filter der Abfrage, dafür
braucht es weder die Auth-Kette noch httpx als Abhängigkeit nur für Tests.
"""

from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

import fakes
import main

SUB_ID = ObjectId()
BESITZER = {"sub": "benutzer-a", "preferred_username": "a"}
ANDERER = {"sub": "benutzer-b", "preferred_username": "b"}
# roles fehlt bei BESITZER/ANDERER absichtlich (wie ein user ohne die Rolle
# dozent es von get_current_user auch bekäme), _ist_admin muss darauf mit
# .get("roles", []) reagieren statt mit KeyError.
DOZENT = {"sub": "lehrende-a", "preferred_username": "l", "roles": ["dozent"]}


@pytest.fixture(autouse=True)
def eine_einreichung(monkeypatch):
    """Eine fertige Einreichung von BESITZER in der Fake-Datenbank."""
    einreichung = {
        "_id": SUB_ID,
        "user_id": BESITZER["sub"],
        "username": BESITZER["preferred_username"],
        "task_id": "0" * 24,
        "code": "print()",
        "sprache": "python",
        "status": "SUCCESS",
        "result": "bestanden",
        "test_results": [],
        "created_at": datetime.now(timezone.utc),
    }
    monkeypatch.setattr(main, "db", fakes.FakeDb([einreichung]))
    return einreichung


def test_fremde_einreichung_liefert_404():
    # 404 und nicht 403. Ein 403 würde die Existenz einer fremden
    # Einreichung bestätigen (#76).
    with pytest.raises(HTTPException) as fehler:
        main.get_submission_status(str(SUB_ID), user=ANDERER)
    assert fehler.value.status_code == 404


def test_eigene_einreichung_bleibt_abrufbar():
    antwort = main.get_submission_status(str(SUB_ID), user=BESITZER)
    assert antwort["id"] == str(SUB_ID)


def test_fremde_ergebnisseite_liefert_404():
    antwort = main.einreichung_seite(str(SUB_ID), fakes.anfrage(), user=ANDERER)
    assert antwort.status_code == 404


def test_eigene_ergebnisseite_bleibt_erreichbar():
    antwort = main.einreichung_seite(str(SUB_ID), fakes.anfrage(), user=BESITZER)
    assert antwort.status_code == 200


def test_dozent_darf_fremde_ergebnisseite_sehen():
    # #240: die Verwaltung verlinkt aus verwaltung-einreichungen.html auf
    # fremde Einreichungen, der Filter aus #76 darf das für die Rolle dozent
    # nicht mehr blockieren.
    antwort = main.einreichung_seite(str(SUB_ID), fakes.anfrage(), user=DOZENT)
    assert antwort.status_code == 200
