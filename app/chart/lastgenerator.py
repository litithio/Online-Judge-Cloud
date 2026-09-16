#!/usr/bin/env python3
"""Lastgenerator aus #12: schickt Einreichungen mit einstellbarer Rate und
Dauer gegen /submit.

Im Cluster läuft das Skript als Pod aus dem Chart, siehe
templates/lastgenerator.yaml (#275). Die backend-Policy aus #62 lässt Ingress
nur von benannten Pods zu, ein Aufruf vom Steuerrechner oder vom Server-Node
fällt unter die Sperre. Der Pod trägt das Label app: lastgenerator, das die
Policy nennt. Einen Lauf startet ein Job aus dem angehaltenen CronJob, Rate
und Dauer stehen in values.yaml unter lastgenerator.

    kubectl create job -n judge --from=cronjob/lastgenerator lastgenerator-1
    kubectl logs -n judge -f job/lastgenerator-1

Die Wirkung der Last zeigt das Dashboard aus #11, nicht dieses Skript. Es
zählt nur, was die API geantwortet hat.

Lokal läuft es aus dem Repo gegen den Compose-Stand. Der Wert ist der aus
app/docker-compose.yml:

    GATEWAY_SECRET=nur-lokal-ohne-gateway-kein-geheimnis \
        python3 app/chart/lastgenerator.py --rate 5 --dauer 60

Die Identität kommt als X-Auth-Request-Header mit, so wie das Gateway sie
setzen würde. Der Weg über das Gateway selbst bräuchte eine Browser-Session
des OIDC-Plugins, und die Last soll die Judge-Kette messen, nicht die
Anmeldung.

Dazu kommt X-Gateway-Auth. Seit #79 lehnt die API einen Aufruf ohne diesen
Header ab, auch aus dem Cluster heraus. Den Wert nimmt das Skript aus
GATEWAY_SECRET, im Pod setzt ihn das Chart aus dem Secret gateway-auth. Nur
Standardbibliothek, damit der Aufruf keine eigene Umgebung braucht und das
Skript im Backend-Image läuft.
"""

import argparse
import collections
import concurrent.futures
import http.client
import json
import math
import os
import pathlib
import random
import sys
import time
import urllib.error
import urllib.request

# Nur python. Die API nimmt über AKTIVE_SPRACHEN in app/backend/main.py auch
# nur python an und lehnt jede andere Sprache mit 400 ab, ein Worker-Image
# gibt es ebenfalls nur dafür.
SPRACHE = "python"

# Die Aufgaben-JSONs und die Lösungen. Im Repo liegen sie unter app/aufgaben
# und neben diesem Skript unter loesungen/, der Prüflauf aus #19 nutzt
# dieselben Dateien. Im Pod kommen beide als ConfigMap unter /aufgaben und
# /loesungen an, das Chart setzt die beiden Variablen.
AUFGABEN = pathlib.Path(
    os.getenv("AUFGABEN_PFAD", pathlib.Path(__file__).resolve().parents[1] / "aufgaben")
)
LOESUNGEN = pathlib.Path(
    os.getenv("LOESUNGEN_PFAD", pathlib.Path(__file__).resolve().parent / "loesungen")
)

# Höchstens so lange wartet der Lauf auf die erste Antwort der API. Unter der
# default-deny-Policy weist sie die erste Verbindung eines frisch gestarteten
# Pods ab, kube-router braucht nach dem Start einen Moment, bis dessen Adresse
# in den Regeln steht. Gemessen am 02.09. in #62 mit acht Sekunden. Dasselbe
# Fenster wie in tests/api_pruefen.py.
WARTEFRIST = 60

# Die zwei Sorten, die nicht als Datei unter loesungen/ liegen: eine, die
# terminiert und falsch rechnet, und eine, die den Interpreter schon beim
# Parsen scheitern lässt. Beide lesen bewusst nichts ein, ein falsches
# Ergebnis braucht die Eingabe nicht.
FALSCH = "print(42)\n"
KAPUTT = "def kaputt(:\n"

# Die Ausgänge zählen getrennt, denn im Screencast ist der Unterschied die
# Aussage: abgelehnt (4xx) heißt, die API lebt und wehrt sich, serverfehler
# (5xx) heißt, hinter ihr klemmt etwas, keine Verbindung heißt, sie ist weg.
# unbekannt steht für den Client-Timeout: Die API kann die Einreichung danach
# trotzdem noch angenommen haben, der Ausgang ist von hier aus nicht zu sehen.
ERGEBNISSE = (
    "angenommen",
    "abgelehnt",
    "serverfehler",
    "unbekannt",
    "keine_verbindung",
)


def _herkunftswert():
    """Der Wert, mit dem das Gateway sich bei der API ausweist (#79).

    Nur aus der Umgebung. Im Pod setzt ihn das Chart aus dem Secret, lokal der
    Aufruf, ein Lesen aus ansible/auth-credentials.yaml gibt es nicht mehr,
    das Skript läuft nicht mehr vom Steuerrechner gegen den Cluster.
    """
    wert = os.getenv("GATEWAY_SECRET")
    if not wert:
        raise SystemExit(
            "GATEWAY_SECRET fehlt. Im Cluster setzt es das Chart aus dem Secret "
            "gateway-auth, lokal der Aufruf, der Wert für den Compose-Stand "
            "steht in app/docker-compose.yml."
        )
    return wert


def _kopfzeilen(nutzer):
    """Die Header, wie das Gateway sie setzen würde.

    Dieselben Namen wie im Headers-Block der Middleware
    (ansible/tasks/traefik-auth.yaml), gelesen von app/backend/auth.py. Ohne
    X-Gateway-Auth antwortet die API auf jede Route mit 401, denn ein direkter
    Aufruf des Service umgeht das Gateway.
    """
    return {
        "X-Auth-Request-User": nutzer,
        "X-Auth-Request-Preferred-Username": nutzer,
        "X-Gateway-Auth": _herkunftswert(),
    }


def _aufgaben_holen(api, kopfzeilen):
    anfrage = urllib.request.Request(f"{api}/tasks", headers=kopfzeilen)
    with urllib.request.urlopen(anfrage, timeout=10) as antwort:
        return json.load(antwort)


def _aufgaben_abwarten(api, kopfzeilen, frist=WARTEFRIST):
    """Holt die Aufgaben und wartet dabei bis zu frist Sekunden auf die API.

    Nur Verbindungsfehler lösen einen neuen Versuch aus. Eine HTTP-Antwort wie
    401 ist eine Antwort der API, sie würde sich beim Warten nicht ändern.
    """
    ende = time.monotonic() + frist
    while True:
        try:
            return _aufgaben_holen(api, kopfzeilen)
        except urllib.error.HTTPError:
            raise
        except OSError as fehler:
            if time.monotonic() >= ende:
                raise SystemExit(f"{api} nicht erreichbar, {fehler}")
            time.sleep(5)


def _loesungen_je_titel():
    """Ordnet Aufgabentitel den Dateien unter loesungen/ zu.

    Der Umweg über die JSONs, weil /tasks nur Titel und ID liefert, die
    Verzeichnisse unter loesungen/ aber nach dem Dateistamm der JSON heißen.
    """
    zuordnung = {}
    for datei in AUFGABEN.glob("*.json"):
        titel = json.loads(datei.read_text())["title"]
        verzeichnis = LOESUNGEN / datei.stem
        akzeptiert = verzeichnis / "akzeptiert.py"
        # langsam ist der vorhandene Limit-Verletzer der Aufgabe. Welches
        # Limit er reißt, ist für die Last gleich: Er hält einen Worker
        # für die Dauer des Limits fest.
        langsam = next(
            (
                p
                for p in (
                    verzeichnis / "zeitlimit.py",
                    verzeichnis / "speicherlimit.py",
                )
                if p.exists()
            ),
            None,
        )
        zuordnung[titel] = {
            "korrekt": akzeptiert.read_text() if akzeptiert.exists() else None,
            "langsam": langsam.read_text() if langsam else None,
        }
    return zuordnung


def _plan_bauen(aufgaben, mix, anzahl):
    """Baut die Liste der Einreichungen: je Eintrag Aufgabe und Code.

    Gewichtete Zufallswahl statt fester Reihenfolge, damit die Sorten über
    die Dauer gemischt ankommen und nicht als Blöcke. Sorten, für die keine
    Aufgabe eine Datei mitbringt, fallen mit Meldung aus dem Mix.
    """
    loesungen = _loesungen_je_titel()
    quellen = {sorte: [] for sorte in mix}
    for aufgabe in aufgaben:
        dateien = loesungen.get(aufgabe["title"], {})
        for sorte in mix:
            if sorte == "falsch":
                quellen[sorte].append((aufgabe, FALSCH))
            elif sorte == "kaputt":
                quellen[sorte].append((aufgabe, KAPUTT))
            elif dateien.get(sorte):
                quellen[sorte].append((aufgabe, dateien[sorte]))

    for sorte in list(mix):
        if not quellen[sorte]:
            print(f"Sorte {sorte} hat keine Quelle, fällt aus dem Mix")
            del mix[sorte]
    if not mix:
        raise SystemExit("Keine Sorte im Mix hat eine Quelle")

    sorten = random.choices(list(mix), weights=mix.values(), k=anzahl)
    return [random.choice(quellen[sorte]) + (sorte,) for sorte in sorten]


def _einreichen(api, kopfzeilen, aufgabe, code):
    daten = json.dumps(
        {"task_id": aufgabe["id"], "code": code, "sprache": SPRACHE}
    ).encode()
    anfrage = urllib.request.Request(
        f"{api}/submit",
        data=daten,
        headers={**kopfzeilen, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(anfrage, timeout=10):
            return "angenommen"
    except urllib.error.HTTPError as fehler:
        return "abgelehnt" if fehler.code < 500 else "serverfehler"
    except TimeoutError:
        return "unbekannt"
    except urllib.error.URLError as fehler:
        # Der Timeout kommt je nach Phase auch als URLError verpackt an.
        if isinstance(fehler.reason, TimeoutError):
            return "unbekannt"
        return "keine_verbindung"
    except (http.client.HTTPException, OSError):
        # Abbruch mitten in der Antwort, etwa RemoteDisconnected. urllib
        # verpackt nur Fehler beim Verbindungsaufbau als URLError, ein Reset
        # während der Antwort kommt roh an, und ein einzelner Reset darf nicht
        # den ganzen Lauf abreißen.
        return "keine_verbindung"


def _mix_lesen(text):
    try:
        mix = {}
        for teil in text.split(","):
            sorte, gewicht = teil.split("=")
            if sorte not in ("korrekt", "langsam", "falsch", "kaputt"):
                raise ValueError(f"Unbekannte Sorte: {sorte}")
            if int(gewicht) < 0:
                raise ValueError(f"Gewicht von {sorte} ist negativ")
            mix[sorte] = int(gewicht)
    except ValueError as fehler:
        raise SystemExit(f"--mix nicht lesbar: {fehler}")
    if sum(mix.values()) <= 0:
        raise SystemExit("--mix braucht mindestens ein Gewicht über 0")
    return mix


def lauf(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rate", type=float, required=True, help="Einreichungen je Sekunde"
    )
    parser.add_argument("--dauer", type=float, required=True, help="Sekunden")
    parser.add_argument(
        "--api", default="http://localhost:8000", help="Basis-URL der API"
    )
    parser.add_argument(
        "--mix",
        default="korrekt=6,langsam=2,falsch=1,kaputt=1",
        help="Gewichte je Sorte, Sorten: korrekt, langsam, falsch, kaputt",
    )
    args = parser.parse_args(argv)
    for name, wert in (("--rate", args.rate), ("--dauer", args.dauer)):
        # math.isfinite fängt inf und nan aus type=float, bevor daraus unten
        # eine endlose Schleife oder ein ValueError beim Runden wird.
        if not math.isfinite(wert) or wert <= 0:
            parser.error(f"{name} muss über 0 liegen, ist {wert}")

    # Ein fester Name genügt: Die Einreichungen sollen einem Benutzer gehören,
    # welcher, ist für die Last gleich. LAST_NUTZER, falls die Einreichungen im
    # Screencast unter dem Test-Benutzer erscheinen sollen.
    kopfzeilen = _kopfzeilen(os.getenv("LAST_NUTZER", "lastgenerator"))
    aufgaben = _aufgaben_abwarten(args.api, kopfzeilen)
    if not aufgaben:
        raise SystemExit("Die API kennt keine Aufgaben, erst laden.py ausführen")

    anzahl = max(1, round(args.rate * args.dauer))
    plan = _plan_bauen(aufgaben, _mix_lesen(args.mix), anzahl)
    # Eine Zeile vor dem Lauf, damit kubectl logs -f am Pod etwas zeigt, bevor
    # nach der Dauer der Bericht kommt.
    print(f"{len(plan)} Einreichungen mit Soll-Rate {args.rate}/s gegen {args.api}")
    zaehler = collections.Counter()
    start = time.monotonic()

    # Feste Sollzeiten (start + i/rate) statt sleep(1/rate) je Runde: Eine
    # langsame Antwort verschöbe sonst alle folgenden Einreichungen nach
    # hinten, und die Ist-Rate fiele unbemerkt unter die verlangte. Der Pool
    # entkoppelt das Senden von der Antwortzeit, seine Größe deckelt nur die
    # offenen Verbindungen.
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        offen = []
        for i, (aufgabe, code, sorte) in enumerate(plan):
            pause = start + i / args.rate - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            offen.append(pool.submit(_einreichen, args.api, kopfzeilen, aufgabe, code))
        for fertig in concurrent.futures.as_completed(offen):
            zaehler[fertig.result()] += 1

    # Die Ist-Rate steht mit im Bericht, weil sie unter die Soll-Rate fällt,
    # sobald die API langsamer antwortet, als der Pool Verbindungen offen
    # halten kann. Der Lauf bleibt dann pünktlich in der Schleife, aber die
    # Anfragen starten verspätet, und genau das soll der Bericht zeigen.
    dauer = time.monotonic() - start
    print(
        f"{len(plan)} Einreichungen in {dauer:.1f}s, "
        f"Soll-Rate {args.rate}/s, Ist-Rate {len(plan) / dauer:.1f}/s"
    )
    for ergebnis in ERGEBNISSE:
        print(f"  {ergebnis}: {zaehler[ergebnis]}")
    # Exit-Code für die Verwendung in Skripten: 0 nur, wenn alles ankam.
    return 0 if zaehler["angenommen"] == len(plan) else 1


if __name__ == "__main__":
    sys.exit(lauf())
