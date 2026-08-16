# Online-Judge-Cloud

## Problem

Programmieraufgaben von Hand zu korrigieren skaliert nicht. Bei mehreren
hundert Einreichungen je Aufgabe entscheidet die Korrekturkapazität
darüber, wie oft Studierende überhaupt abgeben dürfen.

Der Online Judge führt eingereichten Code automatisch gegen hinterlegte
Testfälle aus und gibt das Ergebnis zurück.

Zwei Eigenschaften der Domäne prägen die Infrastruktur. Die Last ist
stoßweise: Der Judge wird in Prüfungen eingesetzt. Während einer Klausur
arbeitet ein ganzer Kurs im selben Zeitfenster, zwischen den
Prüfungsterminen liegt der Betrieb nahe null. Und der ausgeführte Code
ist fremd: Endlosschleifen, Speicherfresser und Zugriffsversuche auf das
Netz sind der Normalfall, nicht die Ausnahme.

Über Container-Images können für jede Sprache (Python, C++, Java, Rust) maßgeschneiderte, schlanke Laufzeitumgebungen bereitgestellt werden, ohne den Host-Worker mit Abhängigkeiten zu überladen.

## Architektur

Terraform legt die VMs im Kursprojekt an, Ansible rollt darauf mit der
k3s-Rolle den Cluster aus, die Anwendung läuft als Pods auf den
Worker-Nodes. Von außen führt ein einziger Weg hinein: Die DNS-Zone
zeigt auf die öffentliche IPv6 der Nodes, dort nimmt Traefik jede
Anfrage entgegen und lässt sie erst nach geprüfter Anmeldung zur API
durch.

### Aufbau

<!-- Quellen der Diagramme: docs/diagramme/*.mmd, von Hand gepflegt, ein
     Generator kann den Datenfluss nicht aus den Manifesten ableiten.
     Nach einer Änderung scripts/diagramme.sh laufen lassen und die SVGs
     mitcommitten. -->

![Aufbau von der VM bis zum Pod](docs/diagramme/aufbau.svg)

Die dicken Pfeile sind der Weg einer Einreichung, die gestrichelten
sind alles darum herum: Provisionierung, Anmeldung, Skalierung und
Rückholung. Terraform und Ansible laufen von außen und sind zur
Laufzeit nicht beteiligt.

### Datenfluss einer Einreichung

![Datenfluss einer Einreichung](docs/diagramme/datenfluss.svg)

Bei der Übergabe an den Worker entscheidet sich, ob eine Einreichung
verloren gehen kann. Der Worker übernimmt sie mit einem bedingten
Update, das Token und Frist setzt, und schreibt das Urteil nur mit
gültigem Token. Stirbt ein Worker-Pod nach der Übernahme, läuft die
Frist ab und der Durchlauf reiht die Einreichung erneut ein, bis
MAX_VERSUCHE erreicht ist. Sie läuft dann schlimmstenfalls mehrfach.
Stirbt der Pod zwischen dem Lesen aus der Liste und der Übernahme,
bleibt die Einreichung auf PENDING liegen, ohne dass je eine Frist zu
laufen beginnt (#85). Der Durchlauf holt auch das zurück: Bleibt eine
PENDING-Einreichung länger als REENQUEUE_AFTER_SECONDS ohne neuen
Queue-Eintrag, reiht er sie erneut ein, bis MAX_VERSUCHE erreicht ist
(#113).

## Betrieb

Terraform legt die VMs an, Ansible baut darauf den k3s-Cluster samt der
Datendienste (MongoDB, Redis), dem Judge-Worker und dem Seed der Aufgaben und
rollt die eigene API als Helm-Release aus (`app/chart`). Die Images baut
`images.yml` nach ghcr.io, sie sind öffentlich und lassen sich ohne Zugangsdaten
ziehen.

VPN an für Terraform, VPN aus für alles andere. Terraform spricht mit der
OpenStack-API und braucht den Tunnel. SSH, Ansible und kubectl erreichen die
Nodes über deren öffentliches IPv6 aus dem Internet, und der Full-Tunnel
kappt genau das. Voraussetzung ist IPv6 am eigenen Anschluss, sonst bleibt
nur der Campus.

Einmal je Person einrichten:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
cp ansible/dns-credentials.yaml.example ansible/dns-credentials.yaml
cp ansible/auth-credentials.yaml.example ansible/auth-credentials.yaml
cp ansible/files/mongodb-password.yaml.example ansible/files/mongodb-password.yaml
direnv allow
```

Alle vier Kopien ausfüllen, die Kommentare darin sagen, woher die Werte kommen.
`auth-credentials.yaml` trägt die Secrets der Auth-Kette (Keycloak-Admin,
OIDC-Client-Secret, Plugin-Cookie-Secret, Test-Benutzer). Ohne direnv
stattdessen `source .envrc`, und zwar im Wurzelverzeichnis: die Datei setzt
KUBECONFIG relativ zum aktuellen Verzeichnis.

Cluster hochbringen:

```bash
# VPN an
cd terraform && terraform init && terraform apply

# VPN aus
cd ../ansible && ansible-galaxy install -r requirements.yml --force
ansible-playbook -i inventory/generated-inventory.yml \
                 -i dns-credentials.yaml deploy.yaml
kubectl get nodes
```

`terraform apply` schreibt dabei `ansible/inventory/generated-inventory.yml`,
das Playbook legt die kubeconfig daneben. Wird das Ubuntu-Image auf newstack
neu hochgeladen, bekommt es eine neue ID: den Wert aus `openstack image list`
in die tfvars eintragen.

### Anwendung

Das Chart `app/chart` rollt nur die eigene API (`backend`) aus. MongoDB, die
Redis-Queue, der Judge-Worker und der Seed der Aufgaben gehören zur
Infrastruktur und stehen schon im Cluster; das Chart verbindet sich nur, über
`externe` in den values: die Queue über den Service-Namen, MongoDB über das
Operator-Secret mit der URI samt Zugangsdaten. Das Play mit dem Tag `app`
kopiert den Chart auf den Server und ruft `helm upgrade --install`:

```bash
# nach dem Cluster-Deploy, VPN aus
ansible-playbook -i inventory/generated-inventory.yml \
                 -i dns-credentials.yaml deploy.yaml --tags app
```

Der ausgerollte Stand steht in `ansible/vars/app.yaml`: `app_image_tag` wählt
den Image-Tag (gebaut von `images.yml` bei einem Git-Tag), `app_values_env`
zwischen den Overlays `values-prod.yaml` (zwei API-Replicas) und
`values-dev.yaml` (eine, kleinere Grenzen). `values.schema.json` bricht das
Ausrollen ab, wenn der Image-Tag oder die Anbindung der Datendienste fehlt.

Prüfen: `kubectl get pods` zeigt `backend` als Running.

### Judge

Das Play mit dem Tag `judge` spielt die Manifeste aus `app/k8s` ein (Worker,
Rückhol-CronJob, KEDA-ScaledObject) und führt den Seed der Aufgaben als Job
aus. Der Job nutzt das Worker-Image, `laden.py` und die Aufgaben-JSONs kommen
als ConfigMap in den Cluster:

```bash
# nach dem Cluster-Deploy, VPN aus
ansible-playbook -i inventory/generated-inventory.yml \
                 -i dns-credentials.yaml deploy.yaml --tags judge
```

Prüfen: `kubectl get cronjob,scaledobject` zeigt `durchlauf` und
`code-worker-python`, und der Job `aufgaben-seed` steht auf Completed. Das
Worker-Deployment hat ohne wartende Einreichungen null Replicas, KEDA startet
es bei Last.

### Authentifizierung

Die Anmeldung passiert am Gateway, nicht in der Anwendung (Issue #20). Eine
Anfrage an `app.<zone>` läuft durch Traefik, das den OIDC-Flow über das Plugin
[traefik-oidc-auth](https://github.com/sevensolutions/traefik-oidc-auth) selbst
ausführt -- ohne zweiten Dienst. Ohne gültige Session leitet das Plugin zur
Keycloak-Anmeldung um; nach der Anmeldung füllt es die Identität aus den
Token-Claims in `X-Auth-Request-*`-Header, die es an die API weiterreicht. Die
API prüft keine Tokens mehr, sie liest nur diese Header (`app/backend/auth.py`)
und weist eine Anfrage ohne sie mit 401 ab. Damit bleibt die Anwendung frei von
Login-Seite und Token-Austausch (zero-code).

Keycloak läuft als einzelner Pod mit einem PVC auf `/opt/keycloak/data`, sodass
Realm und Benutzer einen Pod-Neustart überleben. Realm, OIDC-Client und ein
Test-Benutzer kommen als Code über `--import-realm`; die Vorlage liegt in
`ansible/templates/keycloak-realm.json.j2`, von Hand in der Konsole geklickte
Änderungen überschreibt der nächste Import. Das Plugin wird in der statischen
Traefik-Konfiguration aktiviert (`tasks/traefik-plugin.yaml`, per
`HelmChartConfig`), wobei Traefik einmal neu startet. Keycloak und die
Traefik-Anbindung (Middleware + Ingress) rollt das Play mit dem Tag `auth` aus:

```bash
# nach dem Cluster-Deploy, VPN aus
ansible-playbook -i inventory/generated-inventory.yml \
                 -i dns-credentials.yaml deploy.yaml --tags auth
```

Prüfen: `https://keycloak.<zone>` zeigt den Realm `judge`, ein Aufruf von
`https://app.<zone>` leitet unangemeldet zur Anmeldung um, und nach der
Anmeldung mit dem Test-Benutzer aus `auth-credentials.yaml` ist die API
erreichbar. Ein direkter Aufruf des `backend`-Service im Cluster (ohne
Gateway-Header) endet mit 401.

Vor dem Push:

```bash
./scripts/check.sh
```

Das Skript ruft die Prüfungen nacheinander auf und läuft auch nach einem
Fehlschlag weiter. Am Ende steht, welcher Schritt gescheitert ist und womit
er sich beheben lässt. Es prüft selbst nichts, die Einzelaufrufe bleiben
gültig:

```bash
./scripts/infra-check.sh   # terraform fmt und validate, ansible-lint, Syntax, helm lint
ruff check . && ruff format --check .
./scripts/diagramme.sh     # nur nach Änderung an docs/diagramme/*.mmd
```

Dieselben Prüfungen laufen in `lint.yml`, dort einzeln und nicht über
`check.sh`. Der Diagramm-Job vergleicht die gerenderten SVGs mit dem Commit:
eine geänderte `.mmd` ohne mitcommittete SVG macht den Pull Request rot.
`check.sh` rendert dafür nicht selbst, es vergleicht die Zeitstempel und
meldet, wenn eine `.mmd` neuer ist als ihr SVG.

## Entscheidungen
Je Pflicht- und Wahlthema ein Absatz: Wahl, Alternative, Begründung
## Grenzen
Was die Lösung nicht kann und was Sie unter echter Last erwarten

Der eingereichte Code läuft als Subprozess im Judge-Worker, unter einem eigenen
User und mit eigenen Grenzen für Rechenzeit, Speicher, Ausgabemenge und
Prozesszahl. Drei Lücken bleiben.

Im Cluster bekommt der eingereichte Code über einen User-Namespace ein eigenes,
leeres Netz. Das setzt voraus, dass die Laufzeit den Aufruf `unshare` mit
`CLONE_NEWUSER` zulässt -- containerd tut das, ein gesetztes seccomp-Profil
(wie Dockers Standard) blockiert ihn. `SANDBOX_NETZ_ERZWINGEN=1` am Worker lässt
ihn gar nicht erst starten, wenn die Trennung nicht zustande kommt, statt sie
still wegfallen zu lassen. Wo sie ausfiele, bliebe nur eine NetworkPolicy als
Begrenzung.

Die Speichergrenze von 128 MiB gilt für jeden Prozess einzeln. Startet eine
Einreichung weitere Prozesse, bekommt jeder von ihnen erneut 128 MiB. Bei den
erlaubten 64 Prozessen sind das zusammen 8 GiB. Das Speicherlimit des
Worker-Containers (`resources.limits.memory`) deckelt diese Summe: wird sie
überschritten, trifft der OOM-Killer den Pod.

Begrenzt ist, was ein Programm verbraucht, nicht wohin es schreibt. Eine
Einreichung kann außerhalb ihres Arbeitsverzeichnisses Dateien anlegen, etwa in
`/tmp`, und das Aufräumen danach kennt nur ihr eigenes Verzeichnis. Läuft der
Platz voll, scheitern alle folgenden Einreichungen mit einem Systemfehler, bis
jemand aufräumt oder den Container ersetzt. In unserer lokalen Umgebung waren
5,4 GB aus einer einzelnen Einreichung erreichbar, die Menge hängt aber an
Datenträger und Auslastung.

Das Limit für die Ausgabe begrenzt die Größe der Ausgabedatei, nicht die Menge
der geschriebenen Daten. Wer die Datei zwischendurch verkleinert, gibt in Summe
mehr aus. Der Speicher des Workers bleibt davon unberührt, die Schreiblast auf
dem Node nicht.

Außerhalb der Sandbox liegt eine Grenze bei der Verfügbarkeit der API. Die
readinessProbe fragt `/readyz`, und dieser Endpunkt prüft MongoDB. Fällt die
Datenbank aus, haben alle Replicas dieselbe Ursache und werden nach etwa 15
Sekunden gemeinsam aus dem Service genommen, bei `periodSeconds` 5 und
`failureThreshold` 3. Der Aufrufer bekommt dann die Standardseite von Traefik
statt einer Meldung der Anwendung. Der Tausch ist bewusst: Ohne MongoDB kann ein
Pod weder eine Aufgabe ausliefern noch eine Einreichung annehmen. Valkey prüft
die Probe nicht, denn ohne die Queue scheitert allein `/submit`, während
`/tasks` und `/submission` weiter antworten.
## Bonus
Nur falls Sie Bonuspunkte beanspruchen, sonst weglassen
