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

Über Container-Images bekommt jede Sprache (Python, C++, Java, Rust) ihre
eigene schlanke Laufzeitumgebung, ohne den Host-Worker mit Abhängigkeiten zu
überladen.

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

Auf dem eigenen Rechner liegen Python 3.12, Terraform, Helm und Docker.
`terraform` und `helm` ruft `scripts/infra-check.sh` auf, `docker` ruft
`scripts/diagramme.sh` auf. Die Versionen von Terraform und Helm stehen in
`.github/workflows/infra.yml`, die Python-Version in beiden Workflows. venv
erzwingt sie nicht, es übernimmt das `python3` aus der PATH.

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

Das Chart `app/chart` rollt die eigenen Dienste aus, die API (`backend`) und
die Judge-Kette aus Worker, ScaledObject und Rückhol-CronJob. MongoDB, Valkey
und der Seed der Aufgaben gehören zur Infrastruktur und stehen schon im
Cluster. Das Chart verbindet sich mit ihnen über `externe` in den values, mit
der Queue über den Service-Namen und mit MongoDB über das Operator-Secret, das
die URI samt Zugangsdaten hält. Das Play mit dem Tag `app` kopiert den Chart
auf den Server und ruft `helm upgrade --install`.

```bash
# nach dem Cluster-Deploy, VPN aus
ansible-playbook -i inventory/generated-inventory.yml \
                 -i dns-credentials.yaml deploy.yaml --tags app
```

Einmalig beim Umstieg auf diesen Stand, nur auf einem Cluster, der den alten
schon gefahren hat. Worker, Rückhol-CronJob und ScaledObject kamen vorher aus
Ansible und gehören damit nicht Helm. `helm upgrade` übernimmt keine fremden
Objekte und bricht mit `invalid ownership metadata` ab, sie müssen deshalb
vorher weg. Helm legt sie sofort neu an.

```bash
kubectl delete deployment/code-worker cronjob/durchlauf scaledobject/code-worker-python
```

Der ausgerollte Stand steht in `ansible/vars/app.yaml`. `app_image_tag` wählt
den Image-Tag, gebaut von `images.yml` bei einem Git-Tag, und `app_values_env`
wählt zwischen den Overlays `values-prod.yaml` mit zwei API-Replicas und
`values-dev.yaml` mit einer, kleineren Grenzen und höchstens zwei Workern.
`values.schema.json` bricht das Ausrollen ab, wenn der Image-Tag, die Anbindung
der Datendienste oder ein Eintrag unter `judge` fehlt.

Eine weitere Sprache ist ein Eintrag unter `judge.sprachen` in den values, samt
eigenem Worker-Image. Das Chart erzeugt daraus Deployment und ScaledObject. Die
API führt ihre eigene Liste, `AKTIVE_SPRACHEN` in `app/backend/main.py`.
Fehlt die Sprache dort, lehnt `/submit` jede Einreichung dafür mit 400 ab.

Prüfen mit `kubectl get pods`, dort steht `backend` auf Running. `kubectl get
cronjob,scaledobject` zeigt `durchlauf` und `code-worker-python`. Das
Worker-Deployment hat ohne wartende Einreichungen null Replicas, KEDA startet
es bei Last.

### Aufgaben laden

Das Play mit dem Tag `seed` führt den Seed der Aufgaben als Job aus. Der Job
nutzt das Worker-Image, `laden.py` und die Aufgaben-JSONs kommen als ConfigMap
in den Cluster. Der Seed bleibt in Ansible, weil er Dateien aus dem Repo
braucht.

```bash
# nach dem Cluster-Deploy, VPN aus
ansible-playbook -i inventory/generated-inventory.yml \
                 -i dns-credentials.yaml deploy.yaml --tags seed
```

Prüfen mit `kubectl get jobs`, dort steht `aufgaben-seed` auf Completed.

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
Test-Benutzer kommen als Code über `--import-realm`, die Vorlage liegt in
`ansible/templates/keycloak-realm.json.j2`. Der Import greift nur, solange es
den Realm noch nicht gibt. Einen vorhandenen überspringt Keycloak mit der
Strategie IGNORE_EXISTING, eine geänderte Vorlage erreicht den laufenden
Cluster also erst nach einem leeren PVC (#146). Das Plugin wird in der statischen
Traefik-Konfiguration aktiviert (`tasks/traefik-plugin.yaml`, per
`HelmChartConfig`), wobei Traefik einmal neu startet. Keycloak und die
Traefik-Anbindung (Middleware + Ingress) rollt das Play mit dem Tag `auth` aus:

```bash
# nach dem Cluster-Deploy, VPN aus
ansible-playbook -i inventory/generated-inventory.yml \
                 -i dns-credentials.yaml deploy.yaml --tags auth
```

Prüfen: `https://auth.<zone>` zeigt den Realm `judge`, ein Aufruf von
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

### Ressourcen der Judge-Worker

Jeder Judge-Worker bekommt einen Kern, als Request und als Limit, dazu 64Mi
Speicher als Request und 320Mi als Limit. Die Alternative war ein kleinerer
Request von etwa 250m, dessen Spitzen das Limit auffängt. Dann passen mehr
Worker auf einen Node und der Judge skaliert weiter, bevor die Kerne ausgehen.

Den Ausschlag gibt, wie der Judge urteilt. Das Zeitlimit einer Aufgabe gilt
doppelt. Es begrenzt die Rechenzeit über `RLIMIT_CPU`, und dieselbe Zahl gilt
noch einmal als Frist auf die vergangene Zeit, weil eine Einreichung, die auf
eine Eingabe wartet statt zu rechnen, sonst nie ablaufen würde. Die Aufgaben im
Repo setzen 2 bis 4 Sekunden, eine ohne eigenes Limit fällt auf die 5
Sekunden aus dem Worker zurück. Bekommt ein Worker seinen Kern nicht, weil
andere Pods auf demselben Node rechnen, wächst nur die vergangene Zeit. Die
Rechenzeit bleibt unter dem Limit, die Frist reißt trotzdem, und eine korrekte
Einreichung bekommt TLE. Das Urteil hinge dann an der Belegung des Nodes statt
an der Lösung. Deshalb Request gleich Limit. Der Preis dafür sind fünf
gebundene Kerne bei fünf Workern, und über fünf hinaus skaliert der Judge
erst, wenn Nodes dazukommen. Gemessen sind unter Last 897m bis 1009m CPU und
44 bis 48 MiB je Worker, das Speicherlimit deckt zusätzlich die 256 MB ab, die
eine Aufgabe für den Kindprozess fordern darf.

### Deckel für /tmp der Judge-Worker

Das `/tmp` jedes Judge-Workers ist ein emptyDir mit `sizeLimit` 64Mi. Die
Alternative war ein `ephemeral-storage`-Limit am Container. Das würde neben
`/tmp` auch `/var/tmp` und den Container-Layer erfassen, setzt dafür aber ein
gemeinsames Budget für alles, was der Pod schreibt, die Logs des Workers
eingeschlossen. Aus dem Bedarf eines Laufs herleiten lässt sich so ein Budget
nicht, und einer Überschreitung sieht man nicht an, wer sie verursacht hat.

Den Ausschlag gibt dieser Bedarf. Ein Lauf legt in `/tmp` die Lösung ab, deren
Obergrenze die 16-MB-Dokumentgrenze von MongoDB ist, die Eingabe des
Testfalls, im Repo höchstens 372 KB bei `zweisumme`, und zwei Ausgabedateien,
die `RLIMIT_FSIZE` auf je 1 MiB begrenzt. Zusammen rund 20 MiB, aufgeräumt
nach jedem Lauf. 64Mi sind gut das Dreifache, als Luft für Dateien, die eine
Einreichung zulässig in ihrem Arbeitsverzeichnis anlegt. Ohne Deckel waren aus
einer einzelnen Einreichung 5,4 GB gemessen, siehe Grenzen.

Überschreitet die Summe den Deckel, räumt kubelet den Pod ab. Die laufende
Einreichung endet damit, der Durchlauf reiht sie später neu ein, und der
Ersatz-Pod startet mit leerem `/tmp`. Vorher blieb ein vollgeschriebenes
`/tmp` stehen und ließ jede folgende Einreichung scheitern, bis jemand
aufräumte oder den Pod ersetzte.

Zum emptyDir gehört ein Init-Container, der `/tmp` auf die üblichen Rechte
1777 setzt, kubelet legt das Volume ohne Sticky-Bit an. Er bekommt 50m und
16Mi als Request, unterhalb der Werte des Workers, denn Kubernetes bildet den
Request des Pods als Maximum aus Init- und Hauptcontainern, so bleibt er
unverändert. Eine Messung gibt es zu ihm nicht, er führt ein einzelnes chmod
aus und ist in derselben Sekunde fertig, in der er startet.

### Ressourcen der API

Die API bekommt 100m CPU und 64Mi Speicher als Request, dazu 500m und 256Mi als
Limit. Die Alternative war ein CPU-Request an der Last, also 10m bis 20m. Der
reserviert ein Fünftel und lässt die Spitzen vom Limit auffangen.

Den Ausschlag gibt hier der Start und nicht der Betrieb. Im Leerlauf braucht die
API 3m, unter Last 8m bis 9m, beim Start dagegen rund 55m für etwa 15
Sekunden, weil sie dabei ihre Indizes in MongoDB anlegt. Ein Request unterhalb
dieses Werts drosselt genau die Startphase. Das Startfenster der Probe liefe
damit langsamer ab, und beim Rolling Update fehlte eine Replica länger. Der
Preis sind 100m je Replica, also 200m für die beiden, die im Betrieb fast nie
gebraucht werden. Der Speicher steht bei 46 MiB, konstant im Leerlauf, unter
Last und beim Start.

### Startfenster der API

Die startupProbe gibt dem Start 60 Sekunden, periodSeconds 5 und
failureThreshold 12. Vorher waren es 120 Sekunden, hergeleitet aus der
Index-Erstellung im lifespan-Hook, die je Index bis zu 30 Sekunden auf MongoDB
wartete. Seit #108 entstehen die Indizes in einem eigenen Thread, der Start
wartet auf keinen anderen Dienst mehr, und das alte Fenster hatte damit keine
Grundlage mehr.

Gemessen am 23.08.2026 mit dem Image aus app/backend/Dockerfile unter docker
run mit CPU-Grenze, Zeit vom Containerstart bis zur ersten 200 auf /healthz, je
drei Läufe: bei 0,5 CPU rund 1 Sekunde, bei 0,25 rund 2, bei 0,1 rund 8, bei
0,05 rund 26. Die 0,05 entsprechen dem Request aus values-dev.yaml, also dem
Anteil, den ein voll ausgelasteter Knoten dem Pod noch garantiert. Das Fenster
braucht es also wirklich, nur eben für 26 Sekunden statt 120.

60 Sekunden sind gut das Doppelte des Messwerts, als Reserve dafür, dass die
Messung auf dem Mac lief und ein Cluster-Knoten je Kern langsamer sein kann.
Verworfen: 30 Sekunden lägen zu dicht am Messwert. Die startupProbe ganz zu
streichen hieße, dass die liveness mit ihren rund 35 Sekunden (initialDelay 5
plus drei Fehlversuche im 10-Sekunden-Takt) den Start allein abdecken müsste,
ohne Reserve. Die Folge des kleineren Fensters: ein Start, der wirklich hängt,
wird nach spätestens 60 Sekunden neu gestartet statt nach 120.

### Ressourcen von MongoDB

Ein `mongod` bekommt 150m CPU und 512Mi Speicher als Request, dazu 500m und 1Gi
als Limit. Der Sidecar `mongodb-agent` bekommt 100m und 128Mi, die beiden
Init-Container je 50m und 64Mi, der Operator 50m und 64Mi. Die Alternative war,
es bei den Vorgaben des Operators und seines Charts zu belassen. Die stehen
nirgends im Repo und setzen für jeden Sidecar, jeden Init-Container und den
Operator 500m an. Ein mongodb-Pod forderte damit 600m, die 100m von `mongod`
plus die 500m des Sidecars, und mit dem Operator kamen die drei Pods auf 2300m.

Den Ausschlag gibt, dass Sidecar und Operator ihre Spitze nicht unter
Anwendungslast haben. Gemessen mit `app/lastgenerator.py`, 15 Einreichungen je
Sekunde über 180 Sekunden, bleibt der Sidecar bei 17m und der Operator bei 1m.
Ihre Arbeit hängt am Abgleich der Replica-Set-Konfiguration, und der fällt beim
Ausrollen an. Dort sind 30m für den Sidecar und 10m für den Operator gemessen,
danach fallen beide zurück. Nur `mongod` folgt der Last, sein Primary trägt die
Schreiblast und kommt auf 129m. Der bisherige Request von 100m lag darunter und
ist deshalb mitgewachsen.

Die Init-Container stehen mit im Repo, weil die Änderung sonst wirkungslos
bliebe. Kubernetes bildet den Request eines Pods als Maximum aus der Summe der
laufenden Container und dem größten Init-Container. `mongod-posthook` und
`mongodb-agent-readinessprobe` fordern von sich aus 500m, jeder mongodb-Pod
hielte damit weiter 500m fest, obwohl `mongod` und Sidecar zusammen nur 250m
fordern. Beide kopieren je eine Binärdatei und sind in derselben Sekunde fertig,
in der sie starten, eine Messung mit `kubectl top` gibt es zu ihnen deshalb
nicht, ihre Zahl folgt der Arbeit. Der Preis ist, dass die Vorgaben des
Operators nun an vier Stellen überschrieben werden und bei einem Versionssprung
des Charts nachzusehen sind. Dafür fordern die drei Pods und der Operator 800m
statt 2300m.

### Ressourcen von Keycloak

Keycloak bekommt 250m CPU und 832Mi Speicher als Request, dazu 1000m und 1152Mi
als Limit. Die Alternative war ein Speicher-Request am Leerlauf, also rund
560Mi. Der deckt den Normalfall, und die Spitze beim Anmelden fängt das Limit
auf.

Den Ausschlag gibt, dass die Spitze nicht zurückgeht. Gemessen mit
`app/anmeldelast.py`, fünf Läufe mit zusammen 13970 Anmeldungen und bis zu 18
je Sekunde, steigt der Pod von 560Mi auf 735Mi und bleibt dort. Der Bedarf
wächst dabei kaum mit. Vom Heap sind im Spitzenwert 312Mi belegt, gebraucht
werden davon nach einer erzwungenen Bereinigung 92Mi, dazu kommen 157Mi
Metaspace und 33Mi Code-Cache. Zurück ging der Wert im beobachteten Zeitraum
nicht, auch die Bereinigung holte ihn nicht herunter. Ein Request am Leerlauf
läge damit schon nach der ersten Anmeldewelle unter dem Verbrauch, und der
Scheduler plante den Pod zu klein ein. Das Skript ist ein zweites neben
`app/lastgenerator.py`, weil jenes die Anmeldung überspringt. Es setzt die
`X-Auth-Request`-Header selbst und spricht den `backend`-Service direkt an,
Keycloak sieht davon nichts.

Der Request hält 832Mi auf dem Node fest, auch wenn sich niemand anmeldet. Seit
der Heap-Änderung steht der Pod im Spitzenwert bei 707Mi statt bei 735Mi und
bleibt auch nach den Läufen bei rund 706Mi. Die Heap-Decke steht über
`JAVA_OPTS_KC_HEAP` fest auf 512Mi, sonst leitete Keycloak sie über
`-XX:MaxRAMPercentage=70` aus dem Limit ab. Zusammen mit den 256Mi aus
`MaxMetaspaceSize`, dem Code-Cache-Höchststand von 73Mi und 214Mi daneben
ergibt die Decke 1055Mi, und das Limit von 1152Mi deckt diese Rechnung. Weil
die Decke fest ist, hebt ein größeres Limit den Heap nicht mit an, und
reserviert wird auf dem Node nichts davon (#165). Bei der CPU liegt der
Leerlauf bei 2m, eine vollständige Anmeldung kostet 59 Kern-Millisekunden,
rechnerisch entsprechen die 250m damit gut vier Anmeldungen je Sekunde.


### Liveness-Probe am Judge-Worker

Auf den Worker zeigt kein Service, er holt seine Arbeit selbst aus
`judge:<sprache>`, und stirbt sein Prozess, startet Kubernetes ihn ohnehin neu.
Eine Probe fängt deshalb genau einen Fall, einen Worker, der lebt und nicht mehr
arbeitet. Die Alternative war, ohne Probe zu bleiben. Sie trug, solange sich ein
untätiger Worker nicht von einem wartenden unterscheiden ließ, denn `blpop`
wartete ohne Zeitlimit.

Der Worker schreibt nach jedem abgeschlossenen Schritt einen Heartbeat, die
mtime von `/run/heartbeat`, und nicht nur je Schleifenrunde. Nur so lässt sich
eine Frist herleiten. `GRENZE_ZEIT_MAX` deckelt 60 Sekunden je Testfall, die
Zahl der Testfälle deckelt nichts, über einen ganzen Lauf gibt es also keine
Obergrenze. Die längste Lücke zwischen zwei Schritten ist ein Sandbox-Lauf, er
endet nach `zeit + 1 + ZEITFRIST_PUFFER` und damit nach 61,5 Sekunden, dazu 1,0
Sekunde `REST_FRIST` für das Aufräumen. Über diese 62,5 berechenbaren Sekunden
hinaus lässt die Frist von 120 Platz für das, was keine eigene Grenze hat, etwa
das `rmtree`.

Die Probe kann einen Worker treffen, der noch arbeitet. Seine Einreichung bleibt
dann auf RUNNING stehen, der Durchlauf holt sie zurück und verbraucht einen
ihrer drei Versuche.


### Herkunftsprüfung an der API

Die API liest die Identität aus den Gateway-Headern und prüft kein Token. Wer
den `backend`-Service im Cluster direkt erreicht, konnte diese Header selbst
setzen und unter jedem Namen einreichen. Das Gateway setzt deshalb zusätzlich
`X-Gateway-Auth` mit einem festen Wert, den die API vergleicht. Die Alternative
war, das Access-Token weiterzureichen und in der API gegen die JWKS von Keycloak
zu prüfen. Sie träfe auch einen Angreifer, der an den festen Wert kommt. Gegen
den, der hier zählt, wirken beide gleich, denn ein aus der Sandbox
ausgebrochener Worker hält weder ein Token noch das Secret. Den Ausschlag gibt
W6, das die Token-Prüfung am Gateway verlangt und nicht in der Anwendung. Der
feste Wert läuft nie ab und steht im Secret wie im Middleware-Objekt, wer eines
davon lesen darf, kommt an der Prüfung vorbei.


## Grenzen

Der eingereichte Code läuft als Subprozess im Judge-Worker, unter einer je Lauf
eigenen UID und mit eigenen Grenzen für Rechenzeit, Speicher, Ausgabemenge und
Prozesszahl. Vier Lücken bleiben.

Im Cluster bekommt der eingereichte Code über einen User-Namespace ein eigenes,
leeres Netz. Das setzt voraus, dass die Laufzeit den Aufruf `unshare` mit
`CLONE_NEWUSER` zulässt -- containerd tut das, ein gesetztes seccomp-Profil
(wie Dockers Standard) blockiert ihn. `SANDBOX_NETZ_ERZWINGEN=1` am Worker lässt
ihn gar nicht erst starten, wenn die Trennung nicht zustande kommt, statt sie
still wegfallen zu lassen. Wo sie ausfiele, bliebe nur eine NetworkPolicy als
Begrenzung.

`RLIMIT_NPROC` steht auf 0 und begrenzt die Prozesse neben der Einreichung,
ihr eigener ist nicht gemeint. Sie startet also weder einen zweiten Prozess
noch einen zweiten Thread, Threads zählen mit, und beide Laufzeiten verhalten
sich gleich. Eine Lösung mit einem Thread oder einem Hilfsprozess nimmt der
Judge damit nicht mehr an, sie bekommt RE mit einer eigenen Meldung. Mit 1
statt 0 liefe sie durch, dafür bekäme die Einreichung unter runsc einen
zweiten Prozess. Die Speichergrenze gilt je Prozess, und zweimal die für eine
Aufgabe erlaubten 256 MiB liegen über den 320Mi des Worker-Containers.
Gemessen trifft der OOM-Kill dann den Pod und nicht die Einreichung. Ein
Prozess, der einen Lauf übersteht, belegt das Kontingent des nächsten nicht,
denn der Kernel führt es je UID und jeder Lauf bekommt eine eigene. Der Worker
räumt die UID vor der Vergabe trotzdem leer und weicht auf die nächste aus,
solange dort noch etwas läuft. Erst wenn keine UID mehr frei ist, wertet er
das als Fehler der Umgebung. Die Einreichung bleibt dann auf RUNNING stehen
und kostet einen Versuch.

Begrenzt ist, was ein Programm verbraucht, nicht wohin es schreibt. Eine
Einreichung kann außerhalb ihres Arbeitsverzeichnisses Dateien anlegen, und das
Aufräumen danach kennt nur ihr eigenes Verzeichnis. In unserer lokalen Umgebung
waren so 5,4 GB aus einer einzelnen Einreichung erreichbar. Zwei Deckel fangen
das im Cluster. Ihr Arbeitsverzeichnis liegt unter `/work`, dort greift das
`sizeLimit` des emptyDir mit 64Mi, kubelet räumt den Pod bei Überschreitung ab
und der Ersatz startet leer, siehe Entscheidungen. Ihr `/tmp` ist ein eigenes
tmpfs je Lauf mit 16 MiB und 4096 Dateien, und es verschwindet mit dem letzten
Prozess, der seinen Namespace hält. Vier Reste bleiben. Übersteht ein Prozess
das Aufräumen nach dem Lauf, hält er den Namespace und damit das tmpfs, dessen
Speicher bleibt dann belegt, bis er endet. kubelet erhebt die Belegung der
Volumes nur etwa im Minutenabstand (`volumeStatsAggPeriod`), bis dahin passt
deutlich mehr auf den Datenträger, mit der Eviction verschwindet es wieder. Der
Scan zählt zudem nur, was im Verzeichnis steht. Eine Datei, die eine Einreichung
löscht und offen behält, belegt weiter Platz am Limit vorbei, und frei wird er
erst, wenn der haltende Prozess endet. Und `/var/tmp` liegt außerhalb beider
Volumes im Container-Layer, ist genauso weltbeschreibbar, und was dort landet,
zählt kein Deckel.

Das Limit für die Ausgabe begrenzt die Größe der Ausgabedatei, nicht die Menge
der geschriebenen Daten. Wer die Datei zwischendurch verkleinert, gibt in Summe
mehr aus. Der Speicher des Workers bleibt davon unberührt, die Schreiblast auf
dem Node nicht.

Was ein Ausbruch aus der Sandbox erreicht, hängt am Worker-Pod. Er hält die
Zugangsdaten für MongoDB, `MONGO_URI` kommt aus dem Secret in genau den Pod, der
fremden Code ausführt. Die Einreichung selbst erreicht die Datenbank nicht, sie
läuft in einem leeren Netz-Namespace. Offen bleibt der Worker-Prozess davor. Wer
aus der Sandbox ausbricht, liest und schreibt alle Einreichungen und Aufgaben,
nicht nur die eigene. Die default-deny-Policy begrenzt den Radius, MongoDB und
Valkey muss sie ihm erlauben. Ihn stattdessen über die API schreiben zu lassen,
nähme die Zugangsdaten aus dem Pod. Dagegen steht der Aufwand. Die Übernahme nur
auf `status: PENDING` und der Schreibvorgang nur bei passendem `run_token`
stecken heute in je einer Operation und wären über HTTP neu zu bauen, und die
API läge im Judge-Pfad, ihr Ausfall träfe jeden Lauf.

Außerhalb der Sandbox liegt eine Grenze bei der Verfügbarkeit der API. Die
readinessProbe fragt `/readyz`, und dieser Endpunkt prüft MongoDB. Fällt die
Datenbank aus, haben alle Replicas dieselbe Ursache und werden nach etwa 15
Sekunden gemeinsam aus dem Service genommen, bei `periodSeconds` 5 und
`failureThreshold` 3. Der Aufrufer bekommt dann die Standardseite von Traefik
statt einer Meldung der Anwendung. Der Tausch ist bewusst: Ohne MongoDB kann ein
Pod weder eine Aufgabe ausliefern noch eine Einreichung annehmen. Valkey prüft
die Probe nicht, denn ohne die Queue scheitert allein `/submit`, während
`/tasks` und `/submission` weiter antworten.

Bei der Skalierung bleibt eine Lücke. Der Worker nimmt seine Einreichung aus der
Warteschlange, bevor er rechnet. KEDA misst nur die Länge dieser Warteschlange
und sieht laufende Arbeit deshalb nicht. Ist die Warteschlange leer, fährt KEDA
das Worker-Deployment auf null, auch wenn ein Pod noch rechnet. Einen eigenen
Wert für die Wartezeit davor setzt das Chart nicht, es gilt der Standard von 300
Sekunden, gerechnet ab dem Leerwerden der Warteschlange. Die betroffene
Einreichung bleibt auf RUNNING stehen, bis ihre Frist abläuft, danach reiht der
Durchlauf sie erneut ein und verbraucht einen ihrer drei Versuche. Nach dem
dritten endet sie auf UNRESOLVED, also ohne fachliches Urteil.

Mit den Aufgaben im Repo tritt der Fall nicht ein. Je Testfall wartet der Worker
höchstens das Zeitlimit der Aufgabe plus 1,5 Sekunden, weil die Wall-Clock-Frist
über dem harten RLIMIT_CPU-Limit liegt. Bei `editierdistanz` mit 3 Testfällen à
4 Sekunden sind das gut 16 Sekunden. In die Nähe der Wartezeit käme erst eine
Aufgabe, deren Testläufe zusammen Minuten dauern. `GRENZE_ZEIT_MAX` deckelt das
Zeitlimit auf 60 Sekunden je Testfall, eine Obergrenze für die Zahl der
Testfälle prüft `app/aufgaben/laden.py` nicht. Wer eine solche Aufgabe anlegt,
steht vor dieser Wahl neu.

Der Judge-Worker hat keine readinessProbe. Auf ihn zeigt kein Service, und für
den Rollout wartet schon die startupProbe, denn bis sie durchläuft, gilt der
Container als nicht gestartet. Eine Readiness auf denselben Heartbeat mit
derselben Frist sagte nichts Neues. Mit einer kürzeren fiele ein Worker heraus,
während er rechnet, und unter Dauerlast käme der Rollout nicht mehr durch.

Zwei Fälle beenden einen Worker, der arbeitet. Das `rmtree` beim Aufräumen hat
keine Frist, und eine Einreichung darf im Rahmen des 64Mi-Deckels sehr viele
Dateien anlegen. Der Worker setzt davor einen Heartbeat, damit das Aufräumen mit
der vollen Frist beginnt, dauert es länger als sie, stirbt er trotzdem. Und die
Probe rechnet mit der Wanduhr. Einen Sprung nach hinten lehnt der Test über
`-ge 0` ab, ein Sprung nach vorn über 120 Sekunden trifft einen gesunden Worker.
Dagegen hälfe nur eine monotone Quelle wie `/proc/uptime` samt einem atomar
geschriebenen Zeitwert in der Datei.

Ein längerer Ausfall von MongoDB kostet Einreichungen ihre Versuche. Seit die
Clients Zeitlimits haben, hängt der Worker nicht mehr, sondern stirbt, denn
`_uebernehmen` steht ohne eigenes try in `process_queue`. Jeder Neustart zieht
einen weiteren Eintrag aus der Warteschlange, den der Durchlauf zurückholt und
dabei einen der drei Versuche verbraucht. Der Tausch ist gewollt, ein hängender
Worker zählt für KEDA weiter als Kapazität.
