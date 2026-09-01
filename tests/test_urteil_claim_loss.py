"""Test zur verlorenen Übernahme vor dem ersten Testfall (#137).

Der Platzhalter-Schreibzugriff für test_results in _urteil prüfte sein
matched_count bisher nicht. War die Übernahme zu diesem Zeitpunkt schon
verloren, lief der erste Testfall trotzdem noch durch die Sandbox, bevor die
Prüfung nach dem ersten Fall die verlorene Übernahme überhaupt bemerkte.
"""

from unittest import mock

import worker


def test_urteil_bricht_vor_erstem_testfall_ab_wenn_uebernahme_schon_verloren():
    fall = {"input": "", "expected_output": "x"}
    task = {"test_cases": [fall, fall], "time_limit_seconds": 60}

    with (
        mock.patch.object(worker, "db") as db,
        mock.patch.object(worker, "run_code_in_sandbox") as sandbox_lauf,
    ):
        db.submissions.update_one.return_value = mock.Mock(matched_count=0)
        status, text = worker._urteil("6" * 24, "token", {"code": ""}, task)

    assert (status, text) == (None, None)
    sandbox_lauf.assert_not_called()
