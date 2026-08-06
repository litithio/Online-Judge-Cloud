# Spec: Zero-Code-Authentifizierung über Keycloak, Traefik und oauth2-proxy

## Problem

Der Online Judge validiert Tokens heute in der Anwendung selbst
(`app/backend/auth.py`, `verify_jwt`) und setzt einen Keycloak im
Entwicklungsmodus voraus (`start-dev`, Realm `master`). Für den Cluster-Betrieb
soll die Authentifizierung stattdessen **am Gateway** stattfinden: Ein
nicht angemeldeter Nutzer wird zur Keycloak-Anmeldung geleitet, und erst eine
geprüfte Identität erreicht die API. Die Anwendung soll dabei möglichst
unverändert bleiben ("zero-code") — sie bekommt keine eigene Login-Seite und
keine Token-Austausch-Logik, sondern vertraut der vom Gateway gelieferten
Identität.

Das k3s-Cluster wird bereits von Terraform und Ansible erzeugt. Die Rolle
`k3s-dhbw-cloud-role` bringt Traefik (im k3s-Bundle), cert-manager und
external-dns gegen die DNS-Zone `judge.<kennung>...dhbw.site` mit. Dieses Spec
erweitert das Ansible-Playbook um **Keycloak**, **oauth2-proxy** und die
nötige **Traefik-Konfiguration**, und passt die App minimal an, damit sie die
Gateway-Identität ohne eigene Token-Prüfung übernimmt.

Bezug: Issue #20 (Auth am Gateway), #62 (Namespace/NetworkPolicy, angrenzend).

## Ziele und Nicht-Ziele

### Ziele
- Anmeldung über Keycloak, erzwungen am Gateway, ohne Login-UI in der App.
- Keycloak im Cluster als einzelner Pod mit persistentem Speicher.
- Realm, OIDC-Client und ein Test-Benutzer als Code (Realm-Import).
- oauth2-proxy als OIDC-Client, eingebunden als Traefik-ForwardAuth-Middleware.
- Traefik (das k3s-Bundle) so konfiguriert, dass die App-Route durch die
  ForwardAuth-Middleware geschützt ist.
- Die bestehende Python-App wird so angebunden, dass sie die vom Gateway
  gelieferte Identität (Header) übernimmt, statt selbst JWTs zu prüfen.
- Secrets nach dem bestehenden Muster (`*.example` + `.gitignore`).

### Nicht-Ziele
- Kein Umbau von MongoDB/Redis oder des Workers (#61, #24 bleiben offen).
- Keine RBAC-im-Cluster-Umsetzung (#3), keine NetworkPolicy-Ausarbeitung (#62)
  — nur so weit erwähnt, wie es die Auth-Kette betrifft.
- Kein eigenes Keycloak-Theme (nur Funktion, nicht Optik; #20-Kommentar).
- Keine externe Keycloak-Datenbank und kein Operator.

## Entscheidungen (aus Rückfragen)

| Thema | Entscheidung |
|---|---|
| Traefik | Bundled k3s-Traefik konfigurieren (HelmChartConfig / Middleware-CRs), keinen zweiten Traefik installieren |
| Scope | Auth-Komponenten **und** Anbindung der bestehenden App (IngressRoute schützt den Backend-Service) |
| Keycloak | Einzelner Pod + PVC, H2 auf `/opt/keycloak/data` |
| Realm | Realm-as-Code mit OIDC-Client **und** einem Test-Benutzer (Realm-Import-JSON) |
| Installation | Helm über `kubernetes.core.helm`, eigene Subdomains je Dienst |
| oauth2-proxy | Als Traefik-**ForwardAuth**-Middleware; nicht angemeldete Nutzer werden zu Keycloak umgeleitet |
| Zero-Code | App wird minimal angepasst, damit sie die Gateway-Identität (Header) zero-code übernimmt |
| Secrets | Beispiel-Datei + `.gitignore` (Muster wie `dns-credentials.yaml`) |

## Anforderungen

### 1. Keycloak (Cluster)
- Deployment als **einzelner Pod** im eigenen Namespace (Vorschlag: `auth`).
- **PersistentVolumeClaim** gemountet auf `/opt/keycloak/data`, damit die
  H2-Datenbank Pod-Neustarts überlebt.
- Betriebsmodus, der hinter dem Reverse-Proxy (Traefik/TLS-Terminierung)
  korrekt funktioniert (`KC_PROXY`/`--proxy-headers`, `KC_HOSTNAME` auf die
  Subdomain, `KC_HTTP_ENABLED=true` intern).
- Admin-Zugangsdaten aus einem Secret (bootstrap admin).
- Erreichbar unter eigener Subdomain, z. B. `keycloak.<zone>`, mit TLS über
  cert-manager (wie die vorhandenen Routen der Rolle) und DNS über
  external-dns.
- **Realm-Import beim Start**: Ein Realm (z. B. `judge`) mit
  - einem OIDC-Client für oauth2-proxy (confidential, `code`-Flow,
    Redirect-URIs auf die oauth2-proxy-Callback-URL),
  - einem seeded **Test-Benutzer** (Benutzername/Passwort dokumentiert),
    bereitgestellt als ConfigMap-gemountetes Realm-JSON und
    `--import-realm`.

### 2. oauth2-proxy (Cluster)
- Deployment + Service im selben Namespace.
- Konfiguriert als **OIDC-Client** gegen den Keycloak-Realm
  (`oidc-issuer-url` auf `https://keycloak.<zone>/realms/judge`).
- Betrieb im **ForwardAuth-Modus**: Endpunkt `/oauth2/auth` für die
  Middleware-Prüfung, `/oauth2/start` und `/oauth2/callback` für den
  Login-Flow.
- **Cookie-Secret**, **Client-Secret** aus Kubernetes-Secrets.
- Setzt bei erfolgreicher Prüfung Identitäts-**Header** (mind. Benutzer-ID/
  `sub` und Benutzername) an die Upstream-Anfrage, die die App auswerten kann.
- JWKS/Session so konfiguriert, dass Keycloak nicht bei jeder API-Anfrage im
  Pfad steht (Session-Cookie).

### 3. Traefik (bundled, konfigurieren)
- **ForwardAuth-Middleware** (Traefik `Middleware`-CR) zeigt auf den
  oauth2-proxy-`/oauth2/auth`-Endpunkt und reicht die von oauth2-proxy
  gesetzten Identitäts-Header via `authResponseHeaders` an die App weiter.
- **IngressRoute** für die App-Subdomain (z. B. `app.<zone>`), die diese
  Middleware anwendet und auf den bestehenden `backend`-Service (Port 8000)
  routet.
- Separate **IngressRoute** für die oauth2-proxy `/oauth2/*`-Pfade (Login,
  Callback) ohne die Middleware.
- IngressRoute für `keycloak.<zone>` auf den Keycloak-Service.
- TLS über den bestehenden cert-manager-Pfad der Rolle.
- Konfiguration des Bundles bei Bedarf über `HelmChartConfig`
  (`kube-system/traefik`); Middleware/IngressRoute als CRs via
  `kubernetes.core.k8s`.

### 4. App-Anpassung (zero-code Identität)
- Die App bekommt **keine** eigene Login-Seite und **keinen** Token-Austausch.
- `app/backend/auth.py`/`main.py` werden so angepasst, dass die Identität aus
  den vom Gateway gesetzten Headern (z. B. `X-Forwarded-User` /
  `X-Forwarded-Preferred-Username` bzw. konfigurierte Header) gelesen wird,
  statt selbst ein JWT zu prüfen.
- Verhalten der Endpunkte (`/tasks`, `/submit`, …) bleibt gleich; `user.sub`
  und `preferred_username` in `/submit` werden weiterhin befüllt, jetzt aus den
  Headern.
- Fehlt der Identitäts-Header (direkter Aufruf unter Umgehung des Gateways),
  antwortet die App mit 401.

### 5. Ansible-Integration
- Neue Task-Dateien unter `ansible/tasks/` (z. B. `keycloak.yaml`,
  `oauth2-proxy.yaml`, `traefik-auth.yaml`) und Einbindung in `deploy.yaml`
  als eigene Play(s) mit passendem Tag (z. B. `auth`).
- Installation der Helm-Releases über `kubernetes.core.helm`
  (`kubeconfig: /etc/rancher/k3s/k3s.yaml`), CRs über `kubernetes.core.k8s`.
- Chart-Versionen und Image-Tags **gepinnt** (Konsistenz mit dem
  Pinning-Prinzip in `requirements.yml`).
- Neue Collection-/Rollenabhängigkeiten falls nötig in `requirements.yml`
  ergänzen (gepinnt).

### 6. Secrets und Konfiguration
- Neue Beispiel-Datei (z. B. `ansible/auth-credentials.yaml.example`) mit:
  Keycloak-Admin-Passwort, OIDC-Client-Secret, oauth2-proxy-Cookie-Secret,
  Test-Benutzer-Passwort.
- Reale Datei per `.gitignore` ausgeschlossen (wie `dns-credentials.yaml`).
- Die Zonen-/Hostnamen leiten sich aus der vorhandenen
  `dns-credentials.yaml` (`rfc2136_zone`) ab bzw. werden als Variable geführt.

## Akzeptanzkriterien

1. `ansible-playbook … deploy.yaml` (bzw. `--tags auth`) läuft fehlerfrei
   durch und legt Keycloak, oauth2-proxy und die Traefik-CRs an.
2. Keycloak ist unter `https://keycloak.<zone>` mit gültigem TLS erreichbar und
   zeigt den importierten Realm `judge` samt Client und Test-Benutzer.
3. Aufruf von `https://app.<zone>` **ohne** Anmeldung leitet auf die
   Keycloak-Anmeldeseite um.
4. Nach Anmeldung mit dem Test-Benutzer ist die API erreichbar und
   `/submit` speichert `user_id`/`username` aus der Gateway-Identität.
5. Ein direkter Aufruf des `backend`-Service unter Umgehung des Gateways (ohne
   Identitäts-Header) wird von der App mit 401 abgewiesen.
6. Ein Neustart des Keycloak-Pods verliert weder Realm noch Test-Benutzer
   (PVC-Persistenz).
7. `./scripts/infra-check.sh` (terraform fmt/validate, ansible-lint, Syntax)
   ist grün; `ruff check .` / `ruff format --check .` für die App-Änderung grün.
8. Keine echten Secrets im Repo; nur die `*.example`-Datei ist eingecheckt.

## Umsetzungsschritte

1. **Namespace & Secrets-Gerüst**: Namespace `auth` anlegen;
   `auth-credentials.yaml.example` erstellen, `.gitignore` erweitern,
   Secret-Erzeugung in Ansible aus den Credential-Vars.
2. **Keycloak deployen**: Helm-Release (gepinnt) mit Single-Pod, PVC auf
   `/opt/keycloak/data`, Proxy-/Hostname-Env, Admin-Secret; Realm-JSON als
   ConfigMap + `--import-realm`.
3. **Realm-Import erstellen**: Realm `judge` mit oauth2-proxy-Client
   (Redirect-URIs) und Test-Benutzer als versioniertes JSON.
4. **oauth2-proxy deployen**: Helm-Release (gepinnt) als OIDC-Client gegen
   Keycloak, ForwardAuth-Endpunkte, Cookie-/Client-Secret aus Secret,
   Identitäts-Header konfigurieren.
5. **Traefik-CRs anlegen**: ForwardAuth-`Middleware` (mit
   `authResponseHeaders`), IngressRoutes für `app.<zone>` (mit Middleware),
   `/oauth2/*` (ohne Middleware) und `keycloak.<zone>`; TLS über cert-manager.
6. **App zero-code anbinden**: `auth.py`/`main.py` auf Header-basierte
   Identität umstellen, 401 bei fehlendem Header; Env/Manifest
   (`app/k8s/backend.yaml`) entsprechend anpassen.
7. **Ansible verdrahten**: Task-Dateien einbinden, `deploy.yaml`-Play(s) mit
   Tag `auth`, `requirements.yml` bei neuen Abhängigkeiten ergänzen.
8. **Verifizieren**: Login-Flow gegen `app.<zone>` durchspielen, direkten
   Bypass testen (401), Keycloak-Pod-Neustart prüfen, Lints laufen lassen.
9. **Doku**: README-Abschnitt "Betrieb"/"Entscheidungen" um die Auth-Kette und
   die neue Credentials-Datei ergänzen.

## Offene Annahmen

- Subdomains liegen unterhalb der bestehenden `rfc2136_zone`
  (`judge.<kennung>...dhbw.site`); external-dns legt die Records automatisch an.
- Genaue Chart-Quellen/Versionen für Keycloak (z. B. Bitnami/codecentric) und
  oauth2-proxy werden bei der Umsetzung gewählt und gepinnt.
- Identitäts-Header-Namen werden zwischen oauth2-proxy
  (`--set-xauthrequest`/`pass-*`) und App-Auswertung konsistent festgelegt.
