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
sind Provisionierung und Umgebung. Terraform und Ansible laufen von
außen und sind zur Laufzeit nicht beteiligt.

### Datenfluss einer Einreichung

![Datenfluss einer Einreichung](docs/diagramme/datenfluss.svg)

Der heikle Punkt des Flusses ist die Übergabe an den Worker. Das XACK
kommt erst, nachdem das Ergebnis in MongoDB steht. Eine Einreichung
geht darum beim Verlust eines Worker-Pods nicht verloren, sie wird
schlimmstenfalls ein zweites Mal ausgeführt.

## Betrieb

Terraform legt die VMs an, Ansible baut darauf den k3s-Cluster. Das Ausrollen
der Anwendung in den Cluster fehlt noch: es entsteht mit dem Chart (#15) und
dem Deployment-Ablauf (#17). Die Images baut `images.yml` bereits nach
ghcr.io, sie sind öffentlich und lassen sich ohne Zugangsdaten ziehen.
Solange läuft die Anwendung lokal über `app/docker-compose.yml`.

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
direnv allow
```

Beide Kopien ausfüllen, die Kommentare darin sagen, woher die Werte kommen.
Ohne direnv stattdessen `source .envrc`, und zwar im Wurzelverzeichnis: die
Datei setzt KUBECONFIG relativ zum aktuellen Verzeichnis.

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

Vor dem Push:

```bash
./scripts/check.sh
```

Das Skript ruft die Prüfungen nacheinander auf und läuft auch nach einem
Fehlschlag weiter. Am Ende steht, welcher Schritt gescheitert ist und womit
er sich beheben lässt. Es prüft selbst nichts, die Einzelaufrufe bleiben
gültig:

```bash
./scripts/infra-check.sh   # terraform fmt und validate, ansible-lint, Syntax
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

Im Cluster bekommt der eingereichte Code einen eigenen Netz-Namespace und damit
kein Netz. Lokal im Compose-Stand greift das nicht, weil Dockers seccomp-Profil
den nötigen Aufruf blockiert. Dort teilt er das Netz des Workers und erreicht
MongoDB und Redis direkt, kann also die erwarteten Ausgaben lesen und Ergebnisse
verändern, solange die Dienste keine Anmeldung verlangen. Der Worker schreibt
diesen Zustand beim Start ins Log.

Für den Cluster heißt das: Ein grüner lokaler Lauf sagt über diesen Punkt
nichts. Und wo der Namespace nicht zustande kommt, etwa weil jemand ein
seccomp-Profil setzt, bleibt nur die NetworkPolicy als Begrenzung.

Die Speichergrenze von 128 MiB gilt für jeden Prozess einzeln. Startet eine
Einreichung weitere Prozesse, bekommt jeder von ihnen erneut 128 MiB. Bei den
erlaubten 64 Prozessen sind das zusammen 8 GiB. Lokal deckelt das Speicherlimit
des Containers diese Summe, im Kubernetes-Manifest fehlt die Grenze bisher.

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
## Bonus
Nur falls Sie Bonuspunkte beanspruchen, sonst weglassen
