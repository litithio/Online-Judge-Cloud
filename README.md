# Online-Judge-Cloud

## Problem

Programmieraufgaben von Hand zu korrigieren skaliert nicht. Bei mehreren
hundert Einreichungen je Aufgabe entscheidet die Korrekturkapazität
darüber, wie oft Studierende überhaupt abgeben dürfen.

Der Online Judge führt eingereichten Code automatisch gegen hinterlegte
Testfälle aus und gibt das Ergebnis zurück.

Zwei Eigenschaften der Domäne prägen die Infrastruktur. Die Last ist
stoßweise: Kurz vor einer Abgabefrist treffen fast alle Einreichungen
gleichzeitig ein, davor liegt der Betrieb nahe null. Und der ausgeführte
Code ist fremd: Endlosschleifen, Speicherfresser und Zugriffsversuche
auf das Netz sind der Normalfall, nicht die Ausnahme.

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
Wie man den Stack hochbringt, exakte Kommandos
## Entscheidungen
Je Pflicht- und Wahlthema ein Absatz: Wahl, Alternative, Begründung
## Grenzen
Was die Lösung nicht kann und was Sie unter echter Last erwarten
## Bonus
Nur falls Sie Bonuspunkte beanspruchen, sonst weglassen
