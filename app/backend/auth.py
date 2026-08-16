from fastapi import Header, HTTPException

# Zero-Code-Anbindung ans Gateway: Die Identität prüft nicht mehr die App,
# sondern die Auth-Kette davor (Traefik mit dem OIDC-Plugin traefik-oidc-auth ->
# Keycloak, siehe ansible/tasks/traefik-auth.yaml). Die App bekommt nur die
# bereits geprüfte Identität in Headern, die das Plugin aus den Token-Claims
# füllt. Gleichnamige Header aus der eingehenden Anfrage überschreibt das
# Gateway, sie sind hier also vertrauenswürdig.
#
# Fehlt der Header, kam die Anfrage nicht durch das Gateway (direkter Aufruf des
# Service im Cluster). Dann 401 statt anonymem Zugriff.
#
# FastAPI leitet den Header-Namen aus dem Parameternamen ab: aus
# x_auth_request_user wird X-Auth-Request-User. Die Namen sind frei gewählt und
# im Plugin (Headers-Block der Middleware) genau so gesetzt.


def get_current_user(
    x_auth_request_user: str | None = Header(default=None),
    x_auth_request_preferred_username: str | None = Header(default=None),
):
    """Identität aus den Gateway-Headern lesen.

    Gibt dieselbe Struktur zurück wie zuvor die Token-Prüfung: ``sub`` als
    stabile Benutzer-ID und ``preferred_username`` als Anzeigename, damit die
    Endpunkte in ``main.py`` unverändert bleiben.
    """
    if not x_auth_request_user:
        raise HTTPException(
            status_code=401,
            detail="Keine Identität vom Gateway; Zugriff nur über das Gateway.",
        )

    return {
        "sub": x_auth_request_user,
        "preferred_username": x_auth_request_preferred_username or x_auth_request_user,
    }
