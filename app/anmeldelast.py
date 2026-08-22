#!/usr/bin/env python3
"""Lastgenerator für echte Anmeldungen an Keycloak, Gegenstück zu
lastgenerator.py.

Der andere Generator hilft für die Anmeldung nicht. Er setzt die
X-Auth-Request-Header selbst und spricht den backend-Service direkt an,
Keycloak sieht davon nichts. Für die Herleitung der Keycloak-Grenzen im README
braucht es aber Anmeldungen, die wirklich durch Keycloak laufen.

Gefahren wird der Authorization-Code-Flow mit PKCE, also das, was ein Browser
auch tut. Die Direktvergabe steht nicht zur Verfügung, der Client judge-gateway
hat directAccessGrantsEnabled=false (templates/keycloak-realm.json.j2). Ein
Durchlauf sind drei Anfragen.

    1. GET  /protocol/openid-connect/auth   Login-Formular holen
    2. POST login-actions/authenticate      Formular absenden, 302 mit code
    3. POST /protocol/openid-connect/token  code gegen Access-Token tauschen

Aufruf aus dem Repo, gegen den Cluster über die Ingress:

    python3 app/anmeldelast.py --parallel 5 --dauer 120

Host, Realm und Zugangsdaten kommen aus ansible/dns-credentials.yaml und
ansible/auth-credentials.yaml, damit sie an einer Stelle gepflegt bleiben. Wie
lastgenerator.py nur Standardbibliothek, damit der Aufruf keine eigene Umgebung
braucht.

Beim Messen zwei Fallstricke beachten. Eine Sonde per kubectl exec zählt in
cpu.stat mit, ein jcmd je Stichprobe waren rund 30m. Und nr_throttled sowie
throttled_usec laufen seit dem Start des Pods, sie sagen nur als Differenz vor
und nach dem Lauf etwas über den Lauf.
"""

import argparse
import base64
import hashlib
import http.cookiejar
import os
import pathlib
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

WURZEL = pathlib.Path(__file__).resolve().parent.parent
DNS = WURZEL / "ansible" / "dns-credentials.yaml"
ZUGANG = WURZEL / "ansible" / "auth-credentials.yaml"

# Der Realm heißt judge und der Client judge-gateway, beide aus
# ansible/vars/auth.yaml. Sie stehen hier fest, weil das Skript ohne
# YAML-Bibliothek auskommt und nur die Werte liest, die sich je Installation
# unterscheiden.
REALM = "judge"
CLIENT = "judge-gateway"

# action="..." des Login-Formulars. Keycloak legt dort session_code, execution
# und tab_id hinein, ohne die weist der POST die Anmeldung ab.
FORMULAR = re.compile(r'action="([^"]+)"')


def lies_wert(pfad, schluessel):
    """Holt einen skalaren Wert aus einer Ansible-Vars-Datei ohne PyYAML."""
    muster = re.compile(r"^\s*" + re.escape(schluessel) + r'\s*:\s*"?(.*?)"?\s*$')
    try:
        with open(pfad, encoding="utf-8") as datei:
            for zeile in datei:
                treffer = muster.match(zeile)
                if treffer:
                    return treffer.group(1)
    except FileNotFoundError:
        # Beide Dateien stehen in .gitignore, ein frischer Clone hat sie nicht.
        raise SystemExit(
            f"{pfad} fehlt. Die Datei bleibt lokal, daneben liegt eine "
            f"{pfad.name}.example zum Kopieren."
        ) from None
    raise SystemExit(f"{schluessel} steht nicht in {pfad}")


class HaltWeiterleitung(urllib.request.HTTPRedirectHandler):
    """Hält die 302 nach dem Login an.

    Der code steht im Location-Header. Folgt urllib der Weiterleitung, landet
    sie auf app_host/oidc/callback und der code ist verbraucht, bevor das
    Skript ihn lesen kann.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def oeffner():
    """Eine eigene Sitzung je Anmeldung, damit kein Cookie den Login überspringt.

    Keycloak reicht zwischen Schritt 1 und 2 eine AUTH_SESSION_ID mit, der
    CookieJar hält sie. Ein wiederverwendeter Jar brächte dagegen eine schon
    bestehende SSO-Sitzung mit, und Keycloak überspränge das Formular.
    """
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        HaltWeiterleitung,
    )


def hole(opener, url, daten=None):
    """Eine Anfrage. daten als dict schickt sie als Formular per POST."""
    rumpf = urllib.parse.urlencode(daten).encode() if daten is not None else None
    with opener.open(urllib.request.Request(url, data=rumpf), timeout=60) as antwort:
        return (
            antwort.status,
            antwort.headers,
            antwort.read().decode("utf-8", "replace"),
        )


def eine_anmeldung(basis, umleitung, geheimnis, benutzer, passwort):
    """Ein vollständiger Durchlauf. Wirft bei jedem Fehlschlag."""
    pruefer = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    aufgabe = (
        base64.urlsafe_b64encode(hashlib.sha256(pruefer.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    opener = oeffner()

    frage = urllib.parse.urlencode(
        {
            "client_id": CLIENT,
            "redirect_uri": umleitung,
            "response_type": "code",
            "scope": "openid",
            "state": base64.urlsafe_b64encode(os.urandom(12)).decode(),
            "nonce": base64.urlsafe_b64encode(os.urandom(12)).decode(),
            "code_challenge": aufgabe,
            "code_challenge_method": "S256",
        }
    )
    _, _, seite = hole(opener, f"{basis}/protocol/openid-connect/auth?{frage}")
    treffer = FORMULAR.search(seite)
    if not treffer:
        raise RuntimeError("kein Login-Formular in der Antwort")

    # Der POST auf das Formular endet als 302 und damit als HTTPError, weil
    # HaltWeiterleitung nicht folgt. Der code steht im Location-Header.
    try:
        hole(
            opener,
            treffer.group(1).replace("&amp;", "&"),
            {"username": benutzer, "password": passwort, "credentialId": ""},
        )
    except urllib.error.HTTPError as fehler:
        # HTTPError ist dateiaehnlich und haelt den Socket, bis er zu ist.
        with fehler:
            if fehler.code not in (302, 303):
                raise
            ziel = fehler.headers.get("Location", "")
    else:
        raise RuntimeError("Login endete ohne Weiterleitung, Passwort falsch?")

    felder = urllib.parse.parse_qs(urllib.parse.urlparse(ziel).query)
    if "code" not in felder:
        raise RuntimeError(f"kein code in der Weiterleitung, {ziel[:80]}")

    _, _, antwort = hole(
        opener,
        f"{basis}/protocol/openid-connect/token",
        {
            "grant_type": "authorization_code",
            "code": felder["code"][0],
            "redirect_uri": umleitung,
            "client_id": CLIENT,
            "client_secret": geheimnis,
            "code_verifier": pruefer,
        },
    )
    if "access_token" not in antwort:
        raise RuntimeError("kein access_token in der Antwort")


def main():
    zerleger = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    zerleger.add_argument(
        "--parallel", type=int, default=5, help="gleichzeitige Anmeldungen, Vorgabe 5"
    )
    zerleger.add_argument(
        "--dauer", type=int, default=120, help="Laufzeit in Sekunden, Vorgabe 120"
    )
    argumente = zerleger.parse_args()

    zone = lies_wert(DNS, "rfc2136_zone")
    basis = f"https://auth.{zone}/realms/{REALM}"
    umleitung = f"https://app.{zone}/oidc/callback"
    geheimnis = lies_wert(ZUGANG, "oidc_client_secret")
    benutzer = lies_wert(ZUGANG, "test_user_username")
    passwort = lies_wert(ZUGANG, "test_user_password")

    zaehler = {"ok": 0, "fehler": 0}
    gruende = {}
    schloss = threading.Lock()

    def arbeiter(ende):
        while time.monotonic() < ende:
            try:
                eine_anmeldung(basis, umleitung, geheimnis, benutzer, passwort)
                with schloss:
                    zaehler["ok"] += 1
            except Exception as fehler:  # noqa: BLE001 - Fehlerarten nur zählen
                grund = f"{type(fehler).__name__}: {fehler}"[:120]
                with schloss:
                    zaehler["fehler"] += 1
                    gruende[grund] = gruende.get(grund, 0) + 1

    print(f"Ziel {basis}")
    print(f"{argumente.parallel} gleichzeitig, {argumente.dauer}s", flush=True)
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=argumente.parallel) as pool:
        for _ in range(argumente.parallel):
            pool.submit(arbeiter, start + argumente.dauer)
    gelaufen = time.monotonic() - start

    print(f"\nLaufdauer {gelaufen:.0f}s")
    print(f"erfolgreich {zaehler['ok']}, fehlgeschlagen {zaehler['fehler']}")
    print(f"Rate {zaehler['ok'] / gelaufen:.1f} Anmeldungen je Sekunde")
    for grund, anzahl in sorted(gruende.items(), key=lambda paar: -paar[1]):
        print(f"  {anzahl}x {grund}")
    return 0 if zaehler["ok"] and not zaehler["fehler"] else 1


if __name__ == "__main__":
    sys.exit(main())
