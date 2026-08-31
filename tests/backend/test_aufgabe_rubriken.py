"""Tests zur Aufgabenseite mit Rubriken und Beispiel aus Testfällen (#71).

Geprüft wird die Auswahl der Beispielfälle in aufgabe_seite. Nur ein Testfall
mit sample als echtem True darf auf der Seite erscheinen, jede andere Eingabe
bleibt verborgen.
"""

from bson import ObjectId

import fakes
import main

BENUTZER = {"sub": "benutzer-a", "preferred_username": "a"}
TASK_ID = ObjectId()

AUFGABE = {
    "_id": TASK_ID,
    "title": "Testaufgabe",
    "description": "Einleitung.",
    "input_format": "Eine Zeile Eingabeformat.",
    "output_format": "Eine Zeile Ausgabeformat.",
    "difficulty": "leicht",
    "test_cases": [
        {
            "name": "Beispiel",
            "sample": True,
            "input": "beispiel-eingabe\n",
            "expected_output": "beispiel-ausgabe",
        },
        {
            "name": "Versteckt",
            "input": "geheime-eingabe",
            "expected_output": "geheime-ausgabe",
        },
        {
            "name": "Kaputt markiert",
            "sample": "false",
            "input": "auch-geheim",
            "expected_output": "auch-geheim-ausgabe",
        },
    ],
}


def _seite(monkeypatch):
    monkeypatch.setattr(main, "db", fakes.FakeDb(aufgaben=[AUFGABE]))
    antwort = main.aufgabe_seite(str(TASK_ID), fakes.anfrage(), user=BENUTZER)
    assert antwort.status_code == 200
    return antwort.body.decode()


def test_rubriken_und_beispiel_erscheinen(monkeypatch):
    seite = _seite(monkeypatch)
    assert "Eine Zeile Eingabeformat." in seite
    assert "Eine Zeile Ausgabeformat." in seite
    assert "beispiel-ausgabe" in seite
    # Genau eine Leerzeile zwischen Eingabe und Ausgabe, der Trenner aus dem
    # Entwurf. Den Zeilenumbruch am Eingabeende nimmt aufgabe_seite weg.
    assert "beispiel-eingabe\n\nAusgabe:" in seite


def test_nicht_markierte_eingaben_bleiben_verborgen(monkeypatch):
    seite = _seite(monkeypatch)
    # "false" als Zeichenkette wäre als Wahrheitswert wahr. laden.py lehnt
    # das ab, und aufgabe_seite vergleicht mit True, damit auch ein von Hand
    # geschriebenes Dokument den Fall nicht sichtbar macht.
    assert "geheime-eingabe" not in seite
    assert "auch-geheim" not in seite


def test_aufgabe_ohne_neue_felder_rendert(monkeypatch):
    # Stand in MongoDB vor dem nächsten Seed. Die Seite zeigt dann nur die
    # Beschreibung und keine leeren Rubriken.
    alt = {
        "_id": TASK_ID,
        "title": "Alt",
        "description": "Nur Text.",
        "test_cases": [],
    }
    monkeypatch.setattr(main, "db", fakes.FakeDb(aufgaben=[alt]))
    antwort = main.aufgabe_seite(str(TASK_ID), fakes.anfrage(), user=BENUTZER)
    assert antwort.status_code == 200
    seite = antwort.body.decode()
    assert "Nur Text." in seite
    assert "<h3>Eingabe</h3>" not in seite
    assert "<h3>Beispiel</h3>" not in seite
