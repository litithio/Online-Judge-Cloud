"""Prüflauf gegen die Beispiellösungen, ausgeführt als helm-test-Job (#19).

Je Aufgabe liegt im Chart eine Lösung, die bestehen muss, und eine, die an
einem Limit scheitern muss. Der Dateiname sagt, welches Urteil der Judge
fällen muss, damit prüft der Judge sich selbst. Eingereicht wird über /submit
wie von echten Benutzern, ein Lauf deckt damit API, Queue, das Anlaufen der
Worker aus null Replicas und die Bewertung in einem ab. Die Einreichungen
bleiben danach in der Datenbank stehen, unter dem Benutzer helm-test.

Die Lösungen kommen als ConfigMap aus dem Chart, mit Schlüsseln der Form
verzeichnis__datei.py. Die Zuordnung von Verzeichnis zu Aufgabe läuft über
die Aufgaben-JSONs aus der ConfigMap des Seed. Das Verzeichnis heißt wie der
Dateistamm der JSON, deren Titel ist der Schlüssel in der Datenbank, wie in
app/lastgenerator.py.
"""

import json
import os
import pathlib
import sys
import time
import urllib.request

API = os.getenv("API_URL", "http://backend:8000")
LOESUNGEN = pathlib.Path(os.getenv("LOESUNGEN_PFAD", "/loesungen"))
AUFGABEN = pathlib.Path(os.getenv("AUFGABEN_PFAD", "/aufgaben"))

# Dieselben Header wie in app/lastgenerator.py, siehe api_pruefen.py.
KOPFZEILEN = {
    "X-Auth-Request-User": "helm-test",
    "X-Auth-Request-Preferred-Username": "helm-test",
    "X-Gateway-Auth": os.environ["GATEWAY_SECRET"],
    "Content-Type": "application/json",
}

# Welches Urteil der Dateiname verlangt. Bei den Limit-Verletzern muss das
# passende Testfall-Urteil dabeistehen, ein FAILED wegen falscher Ausgabe
# wäre sonst ein Bestehen dieses Laufs für eine Lösung, die falsch rechnet.
ERWARTET = {
    "akzeptiert.py": ("SUCCESS", None),
    "zeitlimit.py": ("FAILED", "TLE"),
    "speicherlimit.py": ("FAILED", "MLE"),
}

# Obere Schranke für den ganzen Lauf, hergeleitet für einen einzelnen Worker,
# mehr startet KEDA nur zusätzlich. Anlaufen bis zur ersten Übernahme höchstens
# 90 Sekunden, 30 für das Aktivierungs-Polling von KEDA (Vorgabe) und 60 für
# den Worker-Start (startupProbe-Fenster im Chart). Die zeitlimit-Lösungen
# brechen im ersten Testfall am Aufgabenlimit ab, die Limits im Repo sind 2, 2,
# 3 und 5 Sekunden, mit dem Sandbox-Zuschlag von 1,5 zusammen 18. Die
# akzeptierten Lösungen rechnen je Testfall unter einer Sekunde, drei Fälle je
# fünf Aufgaben sind 15, das Speicherlimit reißt im ersten Fall nach wenigen
# Sekunden. Dazu Rüstzeit je Lauf für Übernahme, Arbeitsverzeichnis und die
# MongoDB-Zugriffe, angesetzt mit 10 Sekunden je Lauf, also 100. Zusammen rund
# 225 Sekunden, die 480 lassen das Doppelte.
FRIST_SEKUNDEN = 480
ABFRAGE_ABSTAND = 5


def anfrage(pfad, daten=None):
    """GET oder, mit daten, POST gegen die API, Antwort als JSON."""
    koerper = json.dumps(daten).encode() if daten is not None else None
    aufruf = urllib.request.Request(f"{API}{pfad}", data=koerper, headers=KOPFZEILEN)
    with urllib.request.urlopen(aufruf, timeout=10) as antwort:
        return json.load(antwort)


def _titel_je_stamm():
    """Ordnet den Dateistamm jeder Aufgaben-JSON ihrem Titel zu."""
    return {
        datei.stem: json.loads(datei.read_text())["title"]
        for datei in AUFGABEN.glob("*.json")
    }


def _abweichung(schluessel, einreichung, status_soll, verdict_soll):
    """Der Fehlertext, wenn das Urteil nicht zum Dateinamen passt, sonst None."""
    status_ist = einreichung["status"]
    if status_ist != status_soll:
        return (
            f"{schluessel} endet als {status_ist} statt {status_soll}, "
            f"result {einreichung.get('result')!r}"
        )
    if verdict_soll:
        urteile = [t.get("verdict") for t in einreichung.get("test_results") or []]
        if verdict_soll not in urteile:
            return (
                f"{schluessel} scheitert ohne {verdict_soll}, "
                f"Testfall-Urteile {urteile}, result {einreichung.get('result')!r}"
            )
    return None


def _fehlschlag(fehler, text):
    fehler.append(text)
    print(f"FEHLGESCHLAGEN {text}")


def main():
    titel_je_stamm = _titel_je_stamm()
    if not titel_je_stamm:
        print(f"keine Aufgaben-JSONs unter {AUFGABEN}")
        return 1

    # helm test kann direkt nach dem Upgrade laufen, und kube-router braucht
    # nach dem Start eines Pods einen Moment, bis dessen Adresse in den
    # Regeln der NetworkPolicy steht (#62). Bis dahin wird die erste
    # Verbindung abgewiesen, gemessen am 02.09. mit einem Testjob: erster
    # Versuch abgewiesen, acht Sekunden später verbunden. Gewartet wird wie
    # in api_pruefen.py höchstens 60 Sekunden.
    frist = time.monotonic() + 60
    while True:
        try:
            anfrage("/tasks")
            break
        except OSError as fehler:
            if time.monotonic() >= frist:
                print(f"{API} nicht erreichbar, {fehler}")
                return 1
            time.sleep(5)

    id_je_titel = {t["title"]: t["id"] for t in anfrage("/tasks")}

    fehler = []

    # Erst die Vollständigkeit, dann die Urteile. Jede Aufgabe braucht eine
    # Lösung, die besteht, und eine, die an einem Limit scheitert. Ohne diese
    # Prüfung endete der Lauf grün, obwohl eine Aufgabe ganz ohne Lösungen
    # dasteht und damit ungeprüft bleibt.
    vorhanden = {datei.name for datei in LOESUNGEN.glob("*__*.py")}
    for stamm in sorted(titel_je_stamm):
        if f"{stamm}__akzeptiert.py" not in vorhanden:
            _fehlschlag(fehler, f"{stamm} ohne akzeptiert.py, Aufgabe ungeprüft")
        if not vorhanden & {f"{stamm}__zeitlimit.py", f"{stamm}__speicherlimit.py"}:
            _fehlschlag(fehler, f"{stamm} ohne Limit-Verletzer, Aufgabe ungeprüft")

    offen = []
    for datei in sorted(LOESUNGEN.glob("*__*.py")):
        stamm, name = datei.name.split("__", 1)
        if name not in ERWARTET:
            _fehlschlag(fehler, f"{datei.name} verlangt kein bekanntes Urteil")
            continue
        titel = titel_je_stamm.get(stamm)
        if titel is None:
            _fehlschlag(fehler, f"{datei.name} hat keine Aufgaben-JSON {stamm}.json")
            continue
        task_id = id_je_titel.get(titel)
        if task_id is None:
            _fehlschlag(
                fehler,
                f"{datei.name} findet die Aufgabe {titel!r} nicht, Seed gelaufen?",
            )
            continue
        antwort = anfrage(
            "/submit",
            {"task_id": task_id, "code": datei.read_text(), "sprache": "python"},
        )
        offen.append((datei.name, antwort["submission_id"]))
        print(f"eingereicht {datei.name} als {antwort['submission_id']}")

    if not offen and not fehler:
        print(f"keine Lösungen unter {LOESUNGEN}")
        return 1

    frist = time.monotonic() + FRIST_SEKUNDEN
    while offen and time.monotonic() < frist:
        time.sleep(ABFRAGE_ABSTAND)
        for schluessel, sub_id in list(offen):
            einreichung = anfrage(f"/submission/{sub_id}")
            if einreichung["status"] in ("PENDING", "RUNNING"):
                continue
            offen.remove((schluessel, sub_id))
            status_soll, verdict_soll = ERWARTET[schluessel.split("__", 1)[1]]
            abweichung = _abweichung(schluessel, einreichung, status_soll, verdict_soll)
            if abweichung:
                _fehlschlag(fehler, abweichung)
            else:
                print(f"ok {schluessel} endet wie erwartet als {status_soll}")

    for schluessel, sub_id in offen:
        _fehlschlag(
            fehler,
            f"{schluessel} ({sub_id}) nach {FRIST_SEKUNDEN} Sekunden ohne Urteil",
        )

    if fehler:
        print(f"{len(fehler)} Abweichungen im Prüflauf")
        return 1
    print("alle Lösungen enden mit dem Urteil aus ihrem Dateinamen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
