"""Tests zur Prüfung des Bodys von /submit (#80).

Die Feldprüfungen laufen gegen das Pydantic-Modell selbst, denn FastAPI
prüft den Body vor dem Aufruf der Route und die Tests hier rufen die Route
als Funktion auf. Das Nachschlagen der Aufgabe und der Erfolgsfall laufen
wie in test_einreichung_zugriff.py gegen die Fake-Datenbank aus fakes.py.

Je nachgestelltem Weg aus #80 ein eigener Test. task_id ohne
ObjectId-Format, code als null oder Zahl und code mit einzelnem Surrogat
erreichten bisher den Worker und endeten dort als SYSTEM_ERROR.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.responses import Response

import fakes
import main

TASK_ID = ObjectId()
NUTZER = {"sub": "benutzer-a", "preferred_username": "a"}


def gueltiger_body(**ersetzungen):
    body = {"task_id": str(TASK_ID), "code": "print(1)", "sprache": "python"}
    body.update(ersetzungen)
    return body


def test_fehlendes_task_id_wird_abgelehnt():
    body = gueltiger_body()
    del body["task_id"]
    with pytest.raises(ValidationError):
        main.SubmitBody(**body)


def test_task_id_als_zahl_wird_abgelehnt():
    with pytest.raises(ValidationError):
        main.SubmitBody(**gueltiger_body(task_id=123))


def test_task_id_ohne_objectid_format_wird_abgelehnt():
    # "abc" ist eine Zeichenkette, warf am Worker aber InvalidId. Der Typ
    # str allein hält diesen Wert nicht auf, erst die Formatprüfung.
    with pytest.raises(ValidationError):
        main.SubmitBody(**gueltiger_body(task_id="abc"))


def test_fehlender_code_wird_abgelehnt():
    body = gueltiger_body()
    del body["code"]
    with pytest.raises(ValidationError):
        main.SubmitBody(**body)


def test_code_als_null_wird_abgelehnt():
    with pytest.raises(ValidationError):
        main.SubmitBody(**gueltiger_body(code=None))


def test_code_als_zahl_wird_abgelehnt():
    with pytest.raises(ValidationError):
        main.SubmitBody(**gueltiger_body(code=7))


def test_code_ueber_hoechstlaenge_wird_abgelehnt():
    zu_lang = "a" * (main.CODE_MAX_ZEICHEN + 1)
    with pytest.raises(ValidationError):
        main.SubmitBody(**gueltiger_body(code=zu_lang))


def test_code_an_der_hoechstlaenge_passiert():
    body = main.SubmitBody(**gueltiger_body(code="a" * main.CODE_MAX_ZEICHEN))
    assert len(body.code) == main.CODE_MAX_ZEICHEN


def test_code_mit_einzelnem_surrogat_wird_abgelehnt():
    # Ein einzelnes Surrogat übersteht json.loads unverändert und warf am
    # Worker UnicodeEncodeError beim Schreiben der loesung.py. Die Ablehnung
    # kommt aus der str-Prüfung von pydantic-core, main.py trägt dafür
    # keinen eigenen Validator. Dieser Test hält das Verhalten für die
    # gepinnte pydantic-Version fest.
    with pytest.raises(ValidationError):
        main.SubmitBody(**gueltiger_body(code="print(1) \ud800"))


def test_validierungsantwort_mit_surrogat_bleibt_kodierbar():
    # Der Standard-Handler von FastAPI schreibt die abgelehnte Eingabe in die
    # 422-Antwort zurück. Trägt sie ein einzelnes Surrogat, wirft die
    # UTF-8-Kodierung der Antwort selbst UnicodeEncodeError, und aus der
    # Ablehnung wird eine 500. Nachgestellt über HTTP gegen fastapi 0.141.1.
    with pytest.raises(ValidationError) as fehler:
        main.SubmitBody(**gueltiger_body(code="print(1) \ud800"))
    antwort = asyncio.run(
        main.validierungsfehler_antwort(
            fakes.anfrage(), RequestValidationError(fehler.value.errors())
        )
    )
    assert antwort.status_code == 422
    antwort.body.decode("utf-8")


def test_unbekanntes_feld_wird_abgelehnt():
    with pytest.raises(ValidationError):
        main.SubmitBody(**gueltiger_body(status="SUCCESS"))


def test_sprache_ohne_angabe_faellt_auf_standard():
    body = gueltiger_body()
    del body["sprache"]
    assert main.SubmitBody(**body).sprache == main.STANDARD_SPRACHE


@pytest.fixture
def leere_datenbank(monkeypatch):
    """Fake-Datenbank ohne Aufgaben, Queue als Liste statt Valkey."""
    db = fakes.FakeDb()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "redis_client", fakes.FakeRedis())
    return db


def test_unbekannte_aufgabe_liefert_404_ohne_einreichung(leere_datenbank):
    with pytest.raises(HTTPException) as fehler:
        main.submit_code(
            main.SubmitBody(**gueltiger_body()),
            fakes.anfrage(),
            Response(),
            user=NUTZER,
        )
    assert fehler.value.status_code == 404
    # Vor dem Nachschlagen darf nichts in submissions landen, sonst liegt
    # dort wieder ein Datensatz, den niemand abarbeitet.
    assert leere_datenbank.submissions.dokumente == []


def test_einreichung_mit_bekannter_aufgabe_wird_angelegt(monkeypatch):
    db = fakes.FakeDb(
        aufgaben=[
            {
                "_id": TASK_ID,
                "title": "Summe",
                "created_at": datetime.now(timezone.utc),
            }
        ]
    )
    queue = fakes.FakeRedis()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "redis_client", queue)
    antwort = main.submit_code(
        main.SubmitBody(**gueltiger_body()),
        fakes.anfrage(),
        Response(),
        user=NUTZER,
    )
    assert antwort["status"] == "PENDING"
    assert db.submissions.dokumente[0]["task_id"] == str(TASK_ID)
    assert queue.listen["judge:python"] == [antwort["submission_id"]]
