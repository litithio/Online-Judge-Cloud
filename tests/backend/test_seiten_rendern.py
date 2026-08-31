"""Tests zum Rendern der HTML-Seiten.

fastapi pinnt starlette nur nach unten, ein frischer Image-Build zieht also
die jeweils neueste Version. Diese Tests schlagen fehl, sobald ein neues
starlette die TemplateResponse-Aufrufe in main.py nicht mehr trägt.
"""

import fakes
import main

BENUTZER = {"sub": "benutzer-a", "preferred_username": "a"}


def test_aufgabenseite_rendert(monkeypatch):
    monkeypatch.setattr(main, "db", fakes.FakeDb())
    antwort = main.aufgaben_seite(fakes.anfrage(), user=BENUTZER)
    assert antwort.status_code == 200


def test_fehlerseite_rendert(monkeypatch):
    # Eine unbekannte Aufgabe rendert über _fehlerseite, den einzigen
    # TemplateResponse-Aufruf mit eigenem status_code.
    monkeypatch.setattr(main, "db", fakes.FakeDb())
    antwort = main.aufgabe_seite("0" * 24, fakes.anfrage(), user=BENUTZER)
    assert antwort.status_code == 404
