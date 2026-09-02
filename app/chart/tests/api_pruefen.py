"""Prüft die API am Service, ausgeführt als helm-test-Job (#19).

Gesprochen wird der Service im Cluster, nicht die Adresse hinter der Ingress.
Dort antwortet die Auth-Kette auf jede Anfrage ohne Browser-Session mit 401,
und die API selbst bliebe ungeprüft. HEAD auf die Health-Pfade gehört dazu,
seit #127 beantwortet die API sie mit 200.

Nur Standardbibliothek, das Skript kommt als ConfigMap in das Backend-Image.
"""

import os
import sys
import time
import urllib.error
import urllib.request

API = os.getenv("API_URL", "http://backend:8000")

# Dieselben Header wie in app/chart/lastgenerator.py. Die Identität liest die API
# aus den Headern, die sonst das Gateway setzt, X-Gateway-Auth weist die
# Herkunft nach (#79).
KOPFZEILEN = {
    "X-Auth-Request-User": "helm-test",
    "X-Auth-Request-Preferred-Username": "helm-test",
    "X-Gateway-Auth": os.environ["GATEWAY_SECRET"],
}


def status(pfad, methode="GET", kopfzeilen=None):
    """HTTP-Status einer Anfrage, auch wenn er ein Fehlerstatus ist."""
    anfrage = urllib.request.Request(
        f"{API}{pfad}", method=methode, headers=kopfzeilen or {}
    )
    try:
        with urllib.request.urlopen(anfrage, timeout=10) as antwort:
            return antwort.status
    except urllib.error.HTTPError as fehler:
        return fehler.code


def main():
    # helm test kann direkt nach dem Upgrade laufen. Bis der erste Pod im
    # Service steht, endet jede Anfrage mit einem Verbindungsfehler. Gewartet
    # wird deshalb höchstens 60 Sekunden, dasselbe Fenster, das die
    # startupProbe im Chart dem Start der API gibt.
    frist = time.monotonic() + 60
    while True:
        try:
            status("/healthz")
            break
        except OSError as fehler:
            if time.monotonic() >= frist:
                print(f"{API} nicht erreichbar, {fehler}")
                return 1
            time.sleep(5)

    pruefungen = (
        ("GET /healthz", lambda: status("/healthz"), 200),
        ("HEAD /healthz", lambda: status("/healthz", "HEAD"), 200),
        ("GET /readyz", lambda: status("/readyz"), 200),
        ("HEAD /readyz", lambda: status("/readyz", "HEAD"), 200),
        # Ohne die Header muss die Herkunftsprüfung greifen (#79). Ein 200
        # hieße, jeder Pod im Cluster kann im Namen jedes Benutzers handeln.
        ("GET /tasks ohne Gateway-Header", lambda: status("/tasks"), 401),
        (
            "GET /tasks mit Gateway-Header",
            lambda: status("/tasks", kopfzeilen=KOPFZEILEN),
            200,
        ),
    )

    fehler = 0
    for name, aufruf, erwartet in pruefungen:
        try:
            ist = aufruf()
        except OSError as ausnahme:
            print(f"FEHLGESCHLAGEN {name}, {ausnahme}")
            fehler += 1
            continue
        if ist == erwartet:
            print(f"ok {name} antwortet {ist}")
        else:
            print(f"FEHLGESCHLAGEN {name} antwortet {ist} statt {erwartet}")
            fehler += 1

    if fehler:
        print(f"{fehler} von {len(pruefungen)} Prüfungen fehlgeschlagen")
        return 1
    print(f"alle {len(pruefungen)} Prüfungen bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
