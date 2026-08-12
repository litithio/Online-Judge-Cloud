#!/usr/bin/env python3
"""Lastgenerator aus #12: schickt Einreichungen mit einstellbarer Rate und
Dauer gegen /submit.

Läuft lokal aus dem Repo, nicht im Cluster: Die Screencast-Szene verlangt ein
Live-Terminal, und die Wirkung der Last zeigt das Dashboard aus #11, nicht
dieses Skript. Es zählt deshalb nur, was die API geantwortet hat.

Aufruf, lokal gegen den Compose-Stand:

    python3 lastgenerator.py --rate 5 --dauer 60

Gegen den Cluster dieselben Argumente plus --api und die Keycloak-Variablen
aus der Umgebung. Nur Standardbibliothek, damit der Aufruf keine eigene
Umgebung braucht.
"""

import argparse
import collections
import concurrent.futures
import json
import math
import os
import pathlib
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Nur python: Die API nimmt vier Sprachen an, Worker gibt es nur für Python,
# jede andere Einreichung bliebe für immer PENDING (#107).
SPRACHE = "python"

AUFGABEN = pathlib.Path(__file__).parent / "aufgaben"

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

# So viele Sekunden vor dem Ablauf wird das Token erneuert. Größer als die
# zehn Sekunden Timeout je Anfrage, damit kein schon abgeschicktes, altes
# Token während seiner Anfrage abläuft.
TOKEN_RESERVE = 15


def _token_holen(keycloak_url, realm, nutzer, passwort):
    """Holt ein Token über den Password Grant, Client admin-cli.

    admin-cli, weil der Compose-Stand Keycloak ohne eigenen Client startet
    und dieser eingebaute Client den Password Grant im Realm master erlaubt.

    Gibt auch die Gültigkeit in Sekunden zurück: Sie liegt unter der Dauer
    eines längeren Laufs, das Token muss also währenddessen erneuert werden.
    Sonst zählte der Rest des Laufs lauter 401 als abgelehnt.
    """
    daten = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": nutzer,
            "password": passwort,
        }
    ).encode()
    url = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token"
    with urllib.request.urlopen(urllib.request.Request(url, data=daten)) as antwort:
        inhalt = json.load(antwort)
    return inhalt["access_token"], inhalt["expires_in"]


def _aufgaben_holen(api, token):
    anfrage = urllib.request.Request(
        f"{api}/tasks", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(anfrage) as antwort:
        return json.load(antwort)


def _loesungen_je_titel():
    """Ordnet Aufgabentitel den Dateien unter loesungen/ zu.

    Der Umweg über die JSONs, weil /tasks nur Titel und ID liefert, die
    Verzeichnisse unter loesungen/ aber nach dem Dateistamm der JSON heißen.
    """
    zuordnung = {}
    for datei in AUFGABEN.glob("*.json"):
        titel = json.loads(datei.read_text())["title"]
        verzeichnis = AUFGABEN / "loesungen" / datei.stem
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


def _einreichen(api, token, aufgabe, code):
    daten = json.dumps(
        {"task_id": aufgabe["id"], "code": code, "sprache": SPRACHE}
    ).encode()
    anfrage = urllib.request.Request(
        f"{api}/submit",
        data=daten,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
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

    keycloak = (
        os.getenv("KEYCLOAK_URL", "http://localhost:8080"),
        os.getenv("KEYCLOAK_REALM", "master"),
        os.getenv("KEYCLOAK_NUTZER", "admin"),
        os.getenv("KEYCLOAK_PASSWORT", "admin"),
    )
    if os.getenv("TOKEN"):
        # Ein fest übergebenes Token lässt sich nicht erneuern. Der Lauf muss
        # dann in dessen Gültigkeit passen.
        token, erneuern_ab = os.environ["TOKEN"], None
    else:
        token, gilt = _token_holen(*keycloak)
        erneuern_ab = time.monotonic() + gilt - TOKEN_RESERVE
    aufgaben = _aufgaben_holen(args.api, token)
    if not aufgaben:
        raise SystemExit("Die API kennt keine Aufgaben, erst laden.py ausführen")

    anzahl = max(1, round(args.rate * args.dauer))
    plan = _plan_bauen(aufgaben, _mix_lesen(args.mix), anzahl)
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
            if erneuern_ab is not None and time.monotonic() >= erneuern_ab:
                token, gilt = _token_holen(*keycloak)
                erneuern_ab = time.monotonic() + gilt - TOKEN_RESERVE
            offen.append(pool.submit(_einreichen, args.api, token, aufgabe, code))
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
