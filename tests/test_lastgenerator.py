"""Tests zum Lastgenerator als Pod (#275).

Im Pod kommen Aufgaben und Lösungen als ConfigMaps unter fremden Pfaden an,
der Herkunftswert nur aus der Umgebung, und die erste Verbindung zur API kann
unter der default-deny-Policy scheitern. Jeder der drei Punkte hat hier einen
eigenen Test. Das Skript liegt neben dem Chart, damit Helm es als ConfigMap
einlesen kann, der Import holt es von dort.
"""

import importlib
import io
import json
import pathlib
import sys
import urllib.error
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "chart"))

import lastgenerator  # noqa: E402


def _neu_laden(monkeypatch, aufgaben, loesungen):
    """Lädt das Modul mit gesetzten Pfad-Variablen neu.

    Die Pfade stehen als Konstanten am Modul, ein bloßes setenv nach dem
    Import bliebe wirkungslos. Der Reload am Ende stellt den Stand für die
    anderen Tests wieder her.
    """
    monkeypatch.setenv("AUFGABEN_PFAD", str(aufgaben))
    monkeypatch.setenv("LOESUNGEN_PFAD", str(loesungen))
    return importlib.reload(lastgenerator)


def test_pfade_aus_der_umgebung(tmp_path, monkeypatch):
    # Der Pod mountet /aufgaben und /loesungen, das Repo-Layout gibt es dort
    # nicht. Ohne die Variablen läse das Skript neben sich selbst und fände
    # nichts.
    aufgaben = tmp_path / "aufgaben"
    aufgaben.mkdir()
    (aufgaben / "summe.json").write_text(json.dumps({"title": "Summe"}))
    loesungen = tmp_path / "loesungen" / "summe"
    loesungen.mkdir(parents=True)
    (loesungen / "akzeptiert.py").write_text("print(1)\n")
    (loesungen / "zeitlimit.py").write_text("while True: pass\n")
    try:
        modul = _neu_laden(monkeypatch, aufgaben, tmp_path / "loesungen")
        zuordnung = modul._loesungen_je_titel()
    finally:
        monkeypatch.delenv("AUFGABEN_PFAD")
        monkeypatch.delenv("LOESUNGEN_PFAD")
        importlib.reload(lastgenerator)
    assert zuordnung == {
        "Summe": {"korrekt": "print(1)\n", "langsam": "while True: pass\n"}
    }


def test_vorgabepfade_zeigen_ins_repo():
    # Lokal gegen den Compose-Stand liest das Skript app/aufgaben und
    # app/chart/loesungen, dieselben Dateien wie der Prüflauf aus #19.
    wurzel = pathlib.Path(__file__).resolve().parents[1] / "app"
    assert lastgenerator.AUFGABEN == wurzel / "aufgaben"
    assert lastgenerator.LOESUNGEN == wurzel / "chart" / "loesungen"


def test_herkunftswert_nur_aus_der_umgebung(monkeypatch):
    # Kein stilles Lesen aus ansible/auth-credentials.yaml mehr. Im Pod gibt
    # es die Datei nicht, die Meldung nennt, woher der Wert kommt.
    monkeypatch.delenv("GATEWAY_SECRET", raising=False)
    with pytest.raises(SystemExit, match="GATEWAY_SECRET fehlt"):
        lastgenerator._herkunftswert()
    monkeypatch.setenv("GATEWAY_SECRET", "geheim")
    assert lastgenerator._kopfzeilen("nutzer")["X-Gateway-Auth"] == "geheim"


def _antwort(daten):
    antwort = io.BytesIO(json.dumps(daten).encode())
    antwort.__enter__ = lambda self=antwort: self
    antwort.__exit__ = lambda self=antwort, *a: None
    return antwort


def test_abwarten_wiederholt_verbindungsfehler(monkeypatch):
    # Unter default-deny weist die API die erste Verbindung eines frischen
    # Pods ab (#62). Zwei Fehlversuche, dann die Antwort, der Lauf geht weiter.
    versuche = [
        urllib.error.URLError(ConnectionRefusedError()),
        ConnectionResetError(),
        _antwort([{"id": "1", "title": "Summe"}]),
    ]

    def urlopen(anfrage, timeout):
        ergebnis = versuche.pop(0)
        if isinstance(ergebnis, Exception):
            raise ergebnis
        return ergebnis

    monkeypatch.setattr(lastgenerator.urllib.request, "urlopen", urlopen)
    with mock.patch.object(lastgenerator.time, "sleep") as schlaf:
        aufgaben = lastgenerator._aufgaben_abwarten("http://backend:8000", {})
    assert aufgaben == [{"id": "1", "title": "Summe"}]
    assert schlaf.call_count == 2


def test_abwarten_gibt_nach_der_frist_auf(monkeypatch):
    # Nach der Frist endet der Lauf mit einer Meldung statt einer Ausnahme
    # aus urllib, damit kubectl logs den Grund zeigt.
    monkeypatch.setattr(
        lastgenerator.urllib.request,
        "urlopen",
        mock.Mock(side_effect=urllib.error.URLError(ConnectionRefusedError())),
    )
    uhr = iter([0, 0, 61])
    monkeypatch.setattr(lastgenerator.time, "monotonic", lambda: next(uhr))
    monkeypatch.setattr(lastgenerator.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="nicht erreichbar"):
        lastgenerator._aufgaben_abwarten("http://backend:8000", {}, frist=60)


def test_abwarten_reicht_http_fehler_durch(monkeypatch):
    # Ein 401 ist eine Antwort der API, Warten änderte daran nichts. Die
    # Schleife darf ihn nicht 60 Sekunden lang wiederholen.
    fehler = urllib.error.HTTPError("http://backend:8000/tasks", 401, "", {}, None)
    monkeypatch.setattr(
        lastgenerator.urllib.request, "urlopen", mock.Mock(side_effect=fehler)
    )
    with mock.patch.object(lastgenerator.time, "sleep") as schlaf:
        with pytest.raises(urllib.error.HTTPError):
            lastgenerator._aufgaben_abwarten("http://backend:8000", {})
    assert schlaf.call_count == 0
