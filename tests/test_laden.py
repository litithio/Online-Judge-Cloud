"""Tests zur Prüfung der Aufgaben-Dateien in laden.py (#71).

laden.py liegt nicht am Pfad aus tests/conftest.py, der zeigt auf app/worker.
Der Import bekommt deshalb hier seinen eigenen Eintrag. pymongo ist im
Worker-Image vorhanden, der Import von laden verbindet sich noch nicht.
"""

import json
import pathlib
import sys

import pytest

AUFGABEN = pathlib.Path(__file__).resolve().parents[1] / "app" / "aufgaben"
sys.path.insert(0, str(AUFGABEN))

import laden  # noqa: E402

GUELTIG = {
    "title": "Testaufgabe",
    "description": "Lies nichts und gib nichts aus.",
    "input_format": "Keine Eingabe.",
    "output_format": "Keine Ausgabe.",
    "difficulty": "leicht",
    "test_cases": [
        {"name": "Leere Eingabe", "sample": True, "input": "", "expected_output": ""}
    ],
}


def _datei(tmp_path, aufgabe):
    datei = tmp_path / "aufgabe.json"
    datei.write_text(json.dumps(aufgabe), encoding="utf-8")
    return datei


def test_die_aufgaben_im_repo_bestehen_die_pruefung():
    # Hält die Dateien unter app/aufgaben und die Regeln in laden.py
    # zusammen. Ein neues Pflichtfeld fällt so im CI auf, nicht erst im Seed.
    for datei in sorted(AUFGABEN.glob("*.json")):
        laden.gelesen(datei)


def test_fehlende_difficulty_wird_abgelehnt(tmp_path):
    aufgabe = {k: v for k, v in GUELTIG.items() if k != "difficulty"}
    with pytest.raises(SystemExit, match="difficulty"):
        laden.gelesen(_datei(tmp_path, aufgabe))


def test_unbekannte_difficulty_wird_abgelehnt(tmp_path):
    aufgabe = GUELTIG | {"difficulty": "extrem"}
    with pytest.raises(SystemExit, match="difficulty"):
        laden.gelesen(_datei(tmp_path, aufgabe))


def test_testfall_ohne_namen_wird_abgelehnt(tmp_path):
    aufgabe = GUELTIG | {
        "test_cases": [{"sample": True, "input": "", "expected_output": ""}]
    }
    with pytest.raises(SystemExit, match="Namen"):
        laden.gelesen(_datei(tmp_path, aufgabe))


def test_leerer_testfall_name_wird_abgelehnt(tmp_path):
    aufgabe = GUELTIG | {
        "test_cases": [{"name": "", "sample": True, "input": "", "expected_output": ""}]
    }
    with pytest.raises(SystemExit, match="Namen"):
        laden.gelesen(_datei(tmp_path, aufgabe))


def test_fehlendes_input_format_wird_abgelehnt(tmp_path):
    aufgabe = {k: v for k, v in GUELTIG.items() if k != "input_format"}
    with pytest.raises(SystemExit, match="input_format"):
        laden.gelesen(_datei(tmp_path, aufgabe))


def test_aufgabe_ohne_beispiel_wird_abgelehnt(tmp_path):
    aufgabe = GUELTIG | {
        "test_cases": [{"name": "Fall", "input": "", "expected_output": ""}]
    }
    with pytest.raises(SystemExit, match="sample"):
        laden.gelesen(_datei(tmp_path, aufgabe))


def test_sample_als_zeichenkette_wird_abgelehnt(tmp_path):
    # "false" wäre wahr, der Fall würde ungewollt zum sichtbaren Beispiel.
    aufgabe = GUELTIG | {
        "test_cases": [
            {"name": "Fall", "sample": "false", "input": "", "expected_output": ""}
        ]
    }
    with pytest.raises(SystemExit, match="sample"):
        laden.gelesen(_datei(tmp_path, aufgabe))
