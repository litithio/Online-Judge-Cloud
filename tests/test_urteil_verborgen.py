"""Tests dazu, was das Urteil bei WA über einen Testfall preisgibt (#208).

Vor #208 nannte detail bei jedem nicht bestandenen Fall die erwartete und
die erhaltene Ausgabe, seit #252 dazu eingabe, erwartet und erhalten als
eigene Felder. Eine Einreichung, die ihre Eingabe ausgibt, bekam damit je
Einreichung Eingabe und Sollwert eines verborgenen Falls. Bei RE und MLE
stand stderr oder stdout der Einreichung in detail, derselbe Weg für die
Eingabe. Seither trägt nur ein Beispiel diese Werte, die Aufgabenseite zeigt
sie dort ohnehin.
"""

import signal
from unittest import mock

import worker

EINGABE = "-5 5\n"
ERWARTET = "0"
ERHALTEN = "-5 5"
# sample false ausdrücklich statt weggelassen: laden.py erlaubt beides, und
# eine Prüfung auf das bloße Vorhandensein des Feldes hielte false für ein
# Beispiel.
VERBORGEN = {"input": EINGABE, "expected_output": ERWARTET, "sample": False}
BEISPIEL = {**VERBORGEN, "sample": True}
VERBOTEN = {"eingabe", "erwartet", "erhalten"}


def _urteil_mit(fall, lauf=("OK", ERHALTEN, 10, 100)):
    task = {"test_cases": [fall], "time_limit_seconds": 60}
    geschrieben = []

    def update_one(filter, update):
        geschrieben.append(update["$set"])
        return mock.Mock(matched_count=1)

    with (
        mock.patch.object(worker, "db") as db,
        # Die Einreichung gibt ihre Eingabe aus, der Angriff aus #208.
        mock.patch.object(worker, "run_code_in_sandbox", return_value=lauf),
    ):
        db.submissions.update_one.side_effect = update_one
        status, _ = worker._urteil("6" * 24, "token", {"code": ""}, task)

    assert status == "FAILED"
    return geschrieben[-1]["test_results.0"]


def test_verborgener_testfall_nennt_weder_eingabe_noch_sollwert():
    ergebnis = _urteil_mit(VERBORGEN)

    assert ergebnis["verdict"] == "WA"
    assert not VERBOTEN & ergebnis.keys()
    # detail als Ganzes, nicht nur die Felder: der Sollwert stand vor #252
    # als Satz darin, und die erhaltene Ausgabe ist hier die Eingabe. Als
    # Literal statt worker.VERBORGEN, damit der Test gegen einen Worker
    # ohne die Konstante am Inhalt fällt und nicht am Attribut.
    assert ergebnis["detail"] == "Testfall nicht einsehbar"


def test_fehlendes_sample_zaehlt_als_verborgen():
    ergebnis = _urteil_mit({"input": EINGABE, "expected_output": ERWARTET})

    assert not VERBOTEN & ergebnis.keys()
    assert ergebnis["detail"] == "Testfall nicht einsehbar"


def test_beispiel_traegt_eingabe_und_sollwert_weiter():
    ergebnis = _urteil_mit(BEISPIEL)

    assert ergebnis["verdict"] == "WA"
    assert ergebnis["eingabe"] == EINGABE.removesuffix("\n")
    assert ergebnis["erwartet"] == ERWARTET
    assert ergebnis["erhalten"] == ERHALTEN
    assert ERWARTET in ergebnis["detail"]


# stderr einer Einreichung, die ihre Eingabe dorthin schreibt und mit einem
# Fehler endet. run_code_in_sandbox gibt bei RE und MLE stderr als text
# zurück, bei leerem stderr sogar stdout.
TRACEBACK = f"{ERHALTEN}\nTraceback (most recent call last):\nValueError"


def test_laufzeitfehler_am_verborgenen_testfall_nennt_die_eingabe_nicht():
    ergebnis = _urteil_mit(VERBORGEN, lauf=("RE", TRACEBACK, 10, 100))

    assert ergebnis["verdict"] == "RE"
    # Genau der feste Satz, nicht nur ohne die Eingabe: Was die Einreichung
    # schreibt, darf auch gekürzt oder umgeformt nicht durchkommen.
    assert ergebnis["detail"] == "Testfall nicht einsehbar"
    assert not VERBOTEN & ergebnis.keys()


def test_speicherfehler_am_verborgenen_testfall_nennt_die_eingabe_nicht():
    ergebnis = _urteil_mit(VERBORGEN, lauf=("MLE", TRACEBACK, 10, 100))

    assert ergebnis["verdict"] == "MLE"
    assert ergebnis["detail"] == "Testfall nicht einsehbar"
    assert not VERBOTEN & ergebnis.keys()


def test_laufzeitfehler_am_beispiel_traegt_den_traceback_weiter():
    ergebnis = _urteil_mit(BEISPIEL, lauf=("RE", TRACEBACK, 10, 100))

    assert ergebnis["detail"] == TRACEBACK


def test_zeitlimit_und_ausgabegrenze_am_verborgenen_testfall_behalten_den_judge_text():
    # _urteil reicht die Texte für TLE und OLE durch. Dass sie vom Judge
    # stammen, prüft der Test darunter an _urteil_nach_signal.
    for verdict, meldung in (
        ("TLE", "Rechenzeit von 2 Sekunden überschritten"),
        ("OLE", "mehr als 64 KiB ausgegeben"),
    ):
        ergebnis = _urteil_mit(VERBORGEN, lauf=(verdict, meldung, 3000, 100))

        assert ergebnis["verdict"] == verdict
        assert ergebnis["detail"] == meldung


def test_tle_und_ole_texte_kommen_ohne_ausgabe_der_einreichung():
    # Die Texte für TLE und OLE entstehen in _urteil_nach_signal aus Zeit
    # und Grenze, ohne stdout oder stderr der Einreichung. Der zweite Weg
    # zu OLE (EFBIG statt SIGXFSZ) und zu TLE (SIGKILL am Hard-Limit) liegt
    # in run_code_in_sandbox und braucht einen echten Lauf, er ist hier
    # nicht abgedeckt.
    rusage = mock.Mock(ru_utime=0.0, ru_stime=0.0)

    assert worker._urteil_nach_signal(signal.SIGXCPU, 2, rusage) == (
        "TLE",
        "Rechenzeit von 2 Sekunden überschritten",
    )
    assert worker._urteil_nach_signal(signal.SIGXFSZ, 2, rusage) == (
        "OLE",
        f"mehr als {worker.SANDBOX_AUSGABE_BYTES // 1024} KiB ausgegeben",
    )
