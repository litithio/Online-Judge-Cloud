"""Tests zum geordneten Auslauf des Judge-Workers bei SIGTERM (#199).

In den Tests, die eine Endlosschleife des geprüften Standes treffen können,
bricht eine eigene BaseException als side_effect die Schleife ab. Das except
Exception in process_queue fängt sie nicht, der Test endet also mit einem
Fehler statt zu hängen. KeyboardInterrupt taugt dafür nicht, den werten die
Testrunner als Abbruch des ganzen Laufs.
"""

import signal
from unittest import mock

import pytest
import worker

EINTRAG = (b"judge:python", b"a" * 24)


class SchleifeAbgebrochen(BaseException):
    """Bricht die Endlosschleife eines fehlerhaften Standes im Test ab."""


@pytest.fixture(autouse=True)
def flag_zuruecksetzen():
    """Setzt das Beenden-Flag vor jedem Test zurück.

    Das Flag lebt am Modul und überdauert sonst den Test, der es setzt, und
    jeder folgende Test sähe einen Worker, der sich schon beendet.
    """
    worker._beenden = False
    yield
    worker._beenden = False


def test_handler_setzt_das_flag():
    worker._sigterm(signal.SIGTERM, None)
    assert worker._beenden


def test_handler_ist_beim_import_registriert():
    # Beim Import und nicht erst unter __main__. Ein SIGTERM während der
    # Modul-Initialisierung verpuffte sonst, der Worker ist im Container PID 1.
    assert signal.getsignal(signal.SIGTERM) is worker._sigterm


def test_handler_meldet_sich_ohne_print():
    # Ein print aus einem Signal-Handler kann in einen laufenden print geraten
    # und bricht dann reentrant mit RuntimeError ab. Der Handler muss auch
    # durchlaufen, wenn print genau das tut.
    with mock.patch(
        "builtins.print",
        side_effect=RuntimeError("reentrant call inside <_io.BufferedWriter>"),
    ):
        worker._sigterm(signal.SIGTERM, None)
    assert worker._beenden


def test_handler_uebersteht_ein_kaputtes_stdout():
    # Deskriptor 1 kann zu sein, etwa bei abgerissener Log-Pipe. Die Ausnahme
    # aus os.write darf nicht in den laufenden Pfad schlagen.
    with mock.patch.object(
        worker.os, "write", side_effect=OSError(9, "Bad file descriptor")
    ):
        worker._sigterm(signal.SIGTERM, None)
    assert worker._beenden


def test_nach_dem_signal_kein_blpop_mehr():
    worker._beenden = True
    with mock.patch.object(worker, "redis_client") as rc:
        rc.blpop.side_effect = SchleifeAbgebrochen
        worker.process_queue()
        rc.blpop.assert_not_called()


def test_signal_waehrend_leerem_blpop_beendet_ohne_zuruecklegen():
    # Das Signal trifft ein, während blpop leer wartet. Nach dem Timeout kommt
    # None zurück, es gibt nichts zurückzulegen, die Schleife endet.
    def blpop(key, timeout):
        worker._beenden = True
        return None

    with mock.patch.object(worker, "redis_client") as rc:
        rc.blpop.side_effect = blpop
        worker.process_queue()
        rc.lpush.assert_not_called()
        assert rc.blpop.call_count == 1


def test_gezogener_eintrag_geht_zurueck_an_den_kopf_der_queue():
    # Das Signal trifft ein, während blpop wartet. Der schon gezogene Eintrag
    # geht per lpush zurück, eine Übernahme findet nicht statt.
    def blpop(key, timeout):
        worker._beenden = True
        return EINTRAG

    with (
        mock.patch.object(worker, "redis_client") as rc,
        mock.patch.object(
            worker, "_uebernehmen", side_effect=SchleifeAbgebrochen
        ) as uebernehmen,
    ):
        rc.blpop.side_effect = blpop
        worker.process_queue()
        rc.lpush.assert_called_once_with(worker.QUEUE_KEY, b"a" * 24)
        uebernehmen.assert_not_called()


def test_scheiterndes_zuruecklegen_beendet_den_worker_trotzdem():
    # Ist Valkey beim Zurücklegen schon weg, darf die Ausnahme den Auslauf
    # nicht abbrechen. Die Einreichung holt der Durchlauf über den LPOS-Check
    # zurück, der Worker beendet sich trotzdem geordnet.
    def blpop(key, timeout):
        worker._beenden = True
        return EINTRAG

    with (
        mock.patch.object(worker, "redis_client") as rc,
        mock.patch.object(
            worker, "_uebernehmen", side_effect=SchleifeAbgebrochen
        ) as uebernehmen,
    ):
        rc.blpop.side_effect = blpop
        rc.lpush.side_effect = ConnectionError("Valkey nicht erreichbar")
        worker.process_queue()
        uebernehmen.assert_not_called()


def test_laufende_bewertung_rechnet_zu_ende():
    # Das Signal trifft nach der Übernahme ein. Das Urteil wird noch
    # geschrieben, danach endet die Schleife ohne weiteres blpop.
    submission = {"run_token": "t", "task_id": "6" * 24, "code": ""}

    def uebernehmen(sub_id):
        worker._beenden = True
        return submission

    with (
        mock.patch.object(worker, "redis_client") as rc,
        mock.patch.object(worker, "_uebernehmen", side_effect=uebernehmen),
        mock.patch.object(worker, "db") as db,
        mock.patch.object(worker, "_urteil", return_value=("SUCCESS", "x")),
        mock.patch.object(worker, "_ergebnis_schreiben") as schreiben,
    ):
        rc.blpop.side_effect = [EINTRAG, SchleifeAbgebrochen]
        db.tasks.find_one.return_value = {"test_cases": [{}]}
        worker.process_queue()
        schreiben.assert_called_once()
        assert rc.blpop.call_count == 1


def test_signal_im_fenster_vor_der_uebernahme_bewertet_noch():
    # Trifft das Signal zwischen der Flag-Prüfung und der Übernahme ein, wird
    # die Einreichung noch übernommen und ganz bewertet. Das Fenster ist an
    # _sigterm in worker.py als Grenze dokumentiert, dieser Test pinnt das
    # Verhalten fest. Ein Abbruch nach der Übernahme ließe die Einreichung auf
    # RUNNING stehen und kostete einen Versuch ohne Urteil.
    submission = {"run_token": "t", "task_id": "6" * 24, "code": ""}

    def sub_id_lesen(item):
        worker._sigterm(signal.SIGTERM, None)
        return item

    with (
        mock.patch.object(worker, "redis_client") as rc,
        mock.patch.object(worker, "_sub_id_lesen", side_effect=sub_id_lesen),
        mock.patch.object(
            worker, "_uebernehmen", return_value=submission
        ) as uebernehmen,
        mock.patch.object(worker, "db") as db,
        mock.patch.object(worker, "_urteil", return_value=("SUCCESS", "x")),
        mock.patch.object(worker, "_ergebnis_schreiben") as schreiben,
    ):
        rc.blpop.side_effect = [EINTRAG, SchleifeAbgebrochen]
        db.tasks.find_one.return_value = {"test_cases": [{}]}
        worker.process_queue()
        uebernehmen.assert_called_once()
        schreiben.assert_called_once()
        assert rc.blpop.call_count == 1
