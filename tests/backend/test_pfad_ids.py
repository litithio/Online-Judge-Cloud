"""Tests zu ungültigen IDs in den Pfaden der JSON-Endpunkte (#259).

Eine ID ohne ObjectId-Format warf an /tasks/{task_id} und
/submission/{sub_id} InvalidId und damit eine 500. Die Antwort soll
dieselbe 404 mit derselben Meldung sein wie bei einer fehlenden
Ressource, eine kaputte ID verrät dann nicht mehr als eine fehlende
(#76). Je Endpunkt ein eigener Test.
"""

import pytest
from fastapi import HTTPException

import fakes
import main

NUTZER = {"sub": "benutzer-a", "preferred_username": "a"}


@pytest.fixture(autouse=True)
def leere_datenbank(monkeypatch):
    monkeypatch.setattr(main, "db", fakes.FakeDb())


def test_kaputte_id_an_tasks_liefert_404():
    with pytest.raises(HTTPException) as fehler:
        main.get_task("abc", user=NUTZER)
    assert fehler.value.status_code == 404
    assert fehler.value.detail == "Aufgabe nicht gefunden"


def test_kaputte_id_an_submission_liefert_404():
    with pytest.raises(HTTPException) as fehler:
        main.get_submission_status("abc", user=NUTZER)
    assert fehler.value.status_code == 404
    assert fehler.value.detail == "Submission nicht gefunden"


def test_kaputte_und_fehlende_submission_antworten_gleich():
    # Erst die Gleichheit macht die 404 wertlos für die Unterscheidung,
    # ob eine ID am Format oder an der Suche gescheitert ist. Für
    # /tasks/{task_id} fehlt dieser Vergleich, der Weg zur fehlenden
    # Aufgabe läuft über db.tasks.aggregate, das der Fake nicht kennt.
    with pytest.raises(HTTPException) as kaputt:
        main.get_submission_status("abc", user=NUTZER)
    with pytest.raises(HTTPException) as fehlt:
        main.get_submission_status("0" * 24, user=NUTZER)
    assert (kaputt.value.status_code, kaputt.value.detail) == (
        fehlt.value.status_code,
        fehlt.value.detail,
    )
