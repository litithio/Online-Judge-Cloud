"""Test zur Frist vor dem ersten Testfall (#217).

_uebernehmen setzt nur einen Platzhalter, denn die Aufgabe ist dort noch
nicht gelesen. Sobald _urteil ihre Grenzen kennt, muss die Frist den ersten
Testfall decken. Sonst kann der Durchlauf die Einreichung mitten im ersten
Lauf zurückholen, wenn CLAIM_FRIST_PUFFER_SEKUNDEN unter dessen Laufzeit
gesetzt ist.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import worker

# Unter der Laufzeit eines Falls am Zeitlimit, wie im Fehlerbild aus #217.
# Mit dem Vorgabewert 90 bestünde der Test auch, wenn die Frist nur aus dem
# Puffer bestünde, denn 90 Sekunden decken einen 60-Sekunden-Fall zufällig.
PUFFER = 5


def test_frist_deckt_den_ersten_testfall_vor_dem_lauf():
    zeit = 60
    fall = {"input": "", "expected_output": "x"}
    # Drei Fälle, damit eine Frist über die Summe aller Fälle an der oberen
    # Schranke scheitert. Die Verlängerung gilt je Fall, nicht vorab für alle.
    task = {"test_cases": [fall, fall, fall], "time_limit_seconds": zeit}
    geschriebene_fristen = []
    fristen_vor_dem_lauf = []

    def update_one(filter, update):
        frist = update.get("$set", {}).get("frist")
        if frist is not None:
            geschriebene_fristen.append(frist)
        return mock.Mock(matched_count=1)

    def lauf(code, eingabe, zeit, speicher):
        # Hält fest, welche Fristen beim Start des Falls schon geschrieben
        # sind. Genau darauf kommt es an, eine Verlängerung nach dem Lauf
        # käme für einen Fall am Zeitlimit zu spät.
        fristen_vor_dem_lauf.append(list(geschriebene_fristen))
        return "OK", "x", 10, 100

    vorher = datetime.now(timezone.utc)
    with (
        mock.patch.object(worker, "db") as db,
        mock.patch.object(worker, "run_code_in_sandbox", side_effect=lauf),
        mock.patch.object(worker, "CLAIM_FRIST_PUFFER_SEKUNDEN", PUFFER),
    ):
        db.submissions.update_one.side_effect = update_one
        status, _ = worker._urteil("6" * 24, "token", {"code": ""}, task)
    nachher = datetime.now(timezone.utc)

    assert status == "SUCCESS"
    assert fristen_vor_dem_lauf[0], "keine Frist vor dem ersten Testfall geschrieben"
    frist = fristen_vor_dem_lauf[0][-1]
    # Die Untergrenze verlangt auch den Puffer. Er deckt Rüstzeit, Aufräumen
    # und den Schreibzugriff, ohne ihn reicht die Frist nur für die Sandbox.
    ein_fall = zeit + 1 + worker.ZEITFRIST_PUFFER
    assert frist >= vorher + timedelta(seconds=ein_fall + PUFFER)
    assert frist <= nachher + timedelta(seconds=ein_fall + PUFFER)
