"""Tests zum Warten auf die API in loesungen_pruefen.py (#62).

Unter default-deny weist die API die erste Verbindung eines frischen Pods ab.
Das Skript wartet deshalb auf die API, und drei Dinge müssen dabei stimmen.
Ein Verbindungsfehler löst einen neuen Versuch aus, eine HTTP-Antwort wie 401
nicht, und nach der Frist endet der Lauf mit einer Meldung. Das Skript liegt
neben dem Chart, damit Helm es als ConfigMap einlesen kann, der Import holt
es von dort. GATEWAY_SECRET muss vor dem Import stehen, das Skript liest es
beim Laden.
"""

import io
import json
import os
import pathlib
import sys
import urllib.error
from unittest import mock

import pytest

os.environ.setdefault("GATEWAY_SECRET", "test")
sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "chart" / "tests")
)

import loesungen_pruefen  # noqa: E402


def _antwort(daten):
    # BytesIO ist selbst ein Context Manager, wie das Objekt aus urlopen.
    return io.BytesIO(json.dumps(daten).encode())


def test_abwarten_wiederholt_verbindungsfehler(monkeypatch):
    # Zwei abgewiesene Verbindungen, dann die Antwort. Die Antwort des
    # gelungenen Versuchs ist das Ergebnis, kein weiterer Aufruf folgt.
    versuche = [
        urllib.error.URLError(ConnectionRefusedError()),
        ConnectionResetError(),
        _antwort([{"id": "1", "title": "Summe"}]),
    ]

    def urlopen(anfrage, timeout):
        assert anfrage.full_url == "http://backend:8000/tasks"
        herkunft = loesungen_pruefen.KOPFZEILEN["X-Gateway-Auth"]
        assert anfrage.get_header("X-gateway-auth") == herkunft
        ergebnis = versuche.pop(0)
        if isinstance(ergebnis, Exception):
            raise ergebnis
        return ergebnis

    monkeypatch.setattr(loesungen_pruefen.urllib.request, "urlopen", urlopen)
    with mock.patch.object(loesungen_pruefen.time, "sleep") as schlaf:
        aufgaben = loesungen_pruefen._aufgaben_abwarten()
    assert aufgaben == [{"id": "1", "title": "Summe"}]
    assert schlaf.call_count == 2
    assert versuche == []


def test_abwarten_reicht_http_fehler_durch(monkeypatch):
    # Ein 401 ist eine Antwort der API, Warten änderte daran nichts. Die
    # Schleife darf ihn nicht 60 Sekunden lang wiederholen.
    fehler = urllib.error.HTTPError("http://backend:8000/tasks", 401, "", {}, None)
    monkeypatch.setattr(
        loesungen_pruefen.urllib.request, "urlopen", mock.Mock(side_effect=fehler)
    )
    # Die Uhr springt je Aufruf um 100 Sekunden. Wiederholte die Schleife den
    # 401, liefe sie so in die Frist und endete mit SystemExit statt HTTPError.
    uhr = iter(range(0, 1000, 100))
    monkeypatch.setattr(loesungen_pruefen.time, "monotonic", lambda: next(uhr))
    with mock.patch.object(loesungen_pruefen.time, "sleep") as schlaf:
        with pytest.raises(urllib.error.HTTPError):
            loesungen_pruefen._aufgaben_abwarten()
    assert schlaf.call_count == 0


def test_abwarten_gibt_nach_der_frist_auf(monkeypatch):
    # Nach der Frist endet der Lauf mit einer Meldung statt einer Ausnahme
    # aus urllib, damit kubectl logs den Grund zeigt.
    monkeypatch.setattr(
        loesungen_pruefen.urllib.request,
        "urlopen",
        mock.Mock(side_effect=urllib.error.URLError(ConnectionRefusedError())),
    )
    uhr = iter([0, 0, 61])
    monkeypatch.setattr(loesungen_pruefen.time, "monotonic", lambda: next(uhr))
    monkeypatch.setattr(loesungen_pruefen.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="nicht erreichbar"):
        loesungen_pruefen._aufgaben_abwarten(frist=60)
