"""Tests zum Start der API (#123).

uvicorn bindet über --host nur eine Adressfamilie. Auf :: nahm der Prozess
nichts über IPv4 an, und kubectl port-forward erreichte ihn im Pod-Netz nicht.
Der erste Test prüft den Socket aus start.py, der zweite startet den echten
Einstieg in einem eigenen Prozess, ruft /healthz über 127.0.0.1 und über ::1
auf und beendet ihn mit SIGTERM, so wie kubelet es tut.
"""

import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import start


def _verbindet(familie, adresse, port):
    with socket.socket(familie, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex((adresse, port)) == 0


def _freier_port():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.bind(("::", 0))
        return s.getsockname()[1]


def _status(url):
    frist = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as antwort:
                return antwort.status
        except OSError:
            if time.monotonic() > frist:
                raise
            time.sleep(0.2)


def test_socket_nimmt_ipv4_und_ipv6_an():
    with start.lausch_socket(0) as sock:
        sock.listen(1)
        port = sock.getsockname()[1]
        assert _verbindet(socket.AF_INET6, "::1", port)
        assert _verbindet(socket.AF_INET, "127.0.0.1", port)


def test_einstieg_antwortet_auf_beiden_familien_und_endet_auf_sigterm():
    port = _freier_port()
    prozess = subprocess.Popen(
        [sys.executable, "-c", f"import start; start.starten({port})"],
        cwd=pathlib.Path(start.__file__).parent,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _status(f"http://[::1]:{port}/healthz") == 200
        assert _status(f"http://127.0.0.1:{port}/healthz") == 200
        prozess.send_signal(signal.SIGTERM)
        _, log = prozess.communicate(timeout=10)
        # uvicorn fährt herunter, stellt den Handler zurück und löst das
        # Signal danach erneut aus, der Prozess endet deshalb mit SIGTERM.
        # Als PID 1 im Container verwirft der Kernel ein Signal ohne Handler,
        # dort wird es Exit-Code 0, gemessen mit docker stop.
        assert prozess.returncode in (0, -signal.SIGTERM)
        assert "Application shutdown complete." in log
    finally:
        if prozess.poll() is None:
            prozess.kill()
