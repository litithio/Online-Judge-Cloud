import hmac
import os

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse

# Zero-Code-Anbindung ans Gateway: Die Identität prüft nicht mehr die App,
# sondern die Auth-Kette davor (Traefik mit dem OIDC-Plugin traefik-oidc-auth ->
# Keycloak, siehe ansible/tasks/traefik-auth.yaml). Die App bekommt nur die
# bereits geprüfte Identität in Headern, die das Plugin aus den Token-Claims
# füllt.
#
# Wer den backend-Service direkt erreicht, kann diese Header aber selbst setzen
# und damit im Namen jedes Benutzers handeln (#79). Deshalb setzt das Gateway
# zusätzlich X-Gateway-Auth mit einem Wert, den nur es und die API kennen, und
# herkunft_pruefen unten weist jede Anfrage ohne diesen Wert ab. Die API prüft
# damit die Herkunft der Anfrage und weiterhin kein Token. Die Token-Prüfung
# bleibt am Gateway, so verlangt es das Wahlthema W6.
#
# Der Wert steht im Kubernetes-Secret und im Middleware-Objekt im Cluster. Wer
# eines von beiden lesen darf, kann die Prüfung umgehen. Den zweiten Riegel auf
# der Netzebene führt #62.
#
# FastAPI leitet den Header-Namen aus dem Parameternamen ab: aus
# x_auth_request_user wird X-Auth-Request-User. Die Namen sind frei gewählt und
# im Plugin (Headers-Block der Middleware) genau so gesetzt.

GATEWAY_HEADER = "X-Gateway-Auth"

# Pfade, die ohne Herkunftsprüfung antworten. Das kubelet ruft die Probes direkt
# am Pod auf, an der Auth-Kette vorbei, und schickt deshalb keinen Gateway-Header.
# Verglichen wird der ganze Pfad und nicht sein Anfang. Mit startswith stünde
# unter /healthz jeder weitere Pfad ebenfalls offen.
OFFENE_PFADE = frozenset({"/healthz", "/readyz"})

# Untere Grenze für den Herkunftswert. Der Befehl in
# ansible/auth-credentials.yaml.example erzeugt 44 Zeichen aus 32 zufälligen
# Bytes. Die Grenze fängt einen versehentlich gekürzten Wert ab, mehr nicht. Ob
# der Wert zufällig ist, sieht die API nicht, das entscheidet, wer ihn einträgt.
MINDESTLAENGE = 32


def _wert_aus_umgebung():
    """Den Herkunftswert beim Start lesen und prüfen.

    Beim Import und nicht im lifespan-Hook, anders als die Indizes in main.py.
    Eine fehlende Konfiguration kommt nicht später nach, der Pod soll damit gar
    nicht erst starten. Geprüft wird nicht nur, ob der Wert existiert. Bei einem
    leeren Wert verglichen mit einem leeren Header meldet hmac.compare_digest
    eine Übereinstimmung, und die Prüfung wäre still abgeschaltet.
    """
    wert = os.getenv("GATEWAY_SECRET", "").strip()
    if not wert:
        raise RuntimeError(
            "GATEWAY_SECRET fehlt oder ist leer. Ohne den Wert nimmt die API "
            "jede Anfrage an, die die Identitäts-Header selbst setzt."
        )
    if wert.startswith("aendern"):
        raise RuntimeError(
            "GATEWAY_SECRET steht auf dem Platzhalter aus "
            "ansible/auth-credentials.yaml.example."
        )
    if len(wert) < MINDESTLAENGE:
        raise RuntimeError(f"GATEWAY_SECRET ist kürzer als {MINDESTLAENGE} Zeichen.")
    if not wert.isascii():
        raise RuntimeError("GATEWAY_SECRET enthält Zeichen außerhalb von ASCII.")
    if "{" in wert or "}" in wert:
        # Das Plugin wertet jeden Header-Wert als Go-Template aus. Bei einer
        # geschweiften Klammer im Wert schickte das Gateway etwas anderes, als
        # die API hier erwartet, und jede Anfrage endete mit 401.
        raise RuntimeError("GATEWAY_SECRET enthält geschweifte Klammern.")
    return wert


GATEWAY_SECRET = _wert_aus_umgebung()


def _als_bytes(wert):
    """Für den Vergleich in Bytes wandeln.

    hmac.compare_digest wirft bei Zeichenketten mit Zeichen oberhalb von ASCII
    einen TypeError. Starlette dekodiert eingehende Header als latin-1, ein
    einzelnes Byte über 127 im Header würde die Prüfung also mit 500 beenden
    statt mit 401.
    """
    return wert.encode("latin-1", "replace")


async def herkunft_pruefen(request, call_next):
    """Jede Anfrage außerhalb der Probes muss durch das Gateway gekommen sein.

    Als Middleware und nicht als Depends an den Routen. FastAPI bedient
    /openapi.json, /docs und /redoc selbst, und die hängen an keiner
    Abhängigkeit. Ohne diese Middleware beantwortet der Pod sie jedem im
    Cluster. Sie greift für jede HTTP-Anfrage, auch für eine spätere Route, die
    Depends vergisst. Ein WebSocket-Endpunkt liefe daran vorbei, die API hat
    keinen.
    """
    if request.url.path in OFFENE_PFADE:
        return await call_next(request)

    mitgeschickt = request.headers.get(GATEWAY_HEADER, "")
    if not hmac.compare_digest(_als_bytes(mitgeschickt), _als_bytes(GATEWAY_SECRET)):
        return JSONResponse(status_code=401, content={"detail": "Nicht autorisiert"})

    return await call_next(request)


def get_current_user(
    x_auth_request_user: str | None = Header(default=None),
    x_auth_request_preferred_username: str | None = Header(default=None),
    x_auth_request_roles: str | None = Header(default=None),
):
    """Identität aus den Gateway-Headern lesen.

    Gibt dieselbe Struktur zurück wie zuvor die Token-Prüfung: ``sub`` als
    stabile Benutzer-ID und ``preferred_username`` als Anzeigename, damit die
    Endpunkte in ``main.py`` unverändert bleiben. ``roles`` kommt für #240
    dazu: die Rollen aus Keycloaks realm_access.roles, kommagetrennt vom
    Plugin gesetzt (traefik-auth.yaml). main.py prüft darin nur auf "dozent",
    die Liste selbst bleibt hier ungeprüft, sie ist reine Identität, keine
    Berechtigung.

    Die Meldung nennt weder den fehlenden Header noch die Auth-Kette. Eine
    genauere Auskunft sagt einem Aufrufer, was ihm zum Treffer noch fehlt.
    """
    if not x_auth_request_user:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")

    return {
        "sub": x_auth_request_user,
        "preferred_username": x_auth_request_preferred_username or x_auth_request_user,
        "roles": [
            rolle.strip()
            for rolle in (x_auth_request_roles or "").split(",")
            if rolle.strip()
        ],
    }
