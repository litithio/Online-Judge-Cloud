# Kritische Reflexion: Online-Judge-Cloud (Sturm Teil)

## Architekturreflexion

Der Online Judge korrigiert Programmieraufgaben automatisch gegen hinterlegte Testfälle. Zwei Eigenschaften der Domäne bestimmen die Bewertung der Architekturentscheidung: Die Last ist stoßweise, ein ganzer Kurs reicht im selben Prüfungsfenster ein. Dazwischen kommen nur vereinzelte Einreichungen. Der ausgeführte Code ist grundsätzlich als feindlich zu betrachten. Endlosschleifen, Speicherfresser und Netzzugriffe dürfen sich nicht auf das Gesamtsystem auswirken.

**Vorteil 1: Elastische Skalierung genau an der Stelle, die Last erzeugt.**
KEDA skaliert das Worker-Deployment über die Länge der Valkey-Warteschlange `judge:python` von null auf bis zu sechs Replicas. Gemessen am 11.09.2026 stieg die Warteschlange bei 360 Einreichungen in einer Minute auf 207. Nach 58 Sekunden standen alle sechs Worker , nach 147 Sekunden war die Schlange leer. Ohne Klausur laufen null Worker-Pods, die CPU-Reservierung von sechs vollen Kernen entsteht nur bei Bedarf. Ein Monolith mit fest laufenden Worker-Threads müsste diese Kapazität permanent vorhalten oder würde bei Prüfungsbeginn überrannt.

**Vorteil 2: Isolation entlang der eigentlichen Gefahrenquelle.**
Die Judge-Nodes tragen ein eigenes Taint (`online-judge/sandbox=runsc`), nur die RuntimeClass `gvisor` und der agent-plan des system-upgrade-controller tolerieren es, die RuntimeClass bindet die Worker-Pods dorthin. Auf diesen Nodes läuft sonst nichts außer den unvermeidbaren k3s-Addons. Ein Ausbruch aus der gVisor-Sandbox trifft damit auf Node-Ebene weder die Pods noch die Secrets von MongoDB und Keycloak, weil diese auf anderen Nodes liegen. Offen bleibt die Netzverbindung des Workers zu MongoDB. In einer klassischen VM wäre Betriebssystem-Isolation die einzige Grenze zwischen fremdem Code und den übrigen Diensten.


**Nachteil 1: Betriebliche Komplexität, die im Zeitfenster zwischen zwei Klausuren kaum Nutzen stiftet.** 
Der Stack besteht aus sechs VMs, einem k3s-Cluster, dem MongoDB-Operator, CloudNativePG, KEDA, Traefik mit einem OIDC-Plugin, Longhorn, cert-manager, ExternalDNS und dem system-upgrade-controller. Für einen Dienst, der überwiegend im Leerlauf steht, ist das eine beachtliche Zahl an Komponenten mit eigenen Upgrade-Zyklen und Fehlermodi (etwa das k3s-Upgradefenster, das laufende Judge-Worker per `drain.force` trifft). Ein einzelner Verantwortlicher hält Terraform-State, Application Credential und DNS-Zone, ein Wechsel dieser Person heißt Übergabe von State und Zugangsdaten, ein Neuaufbau des Clusters Downtime für die gesamte Gruppe.


**Nachteil 2: Die Trennung von Queue und Datenbank kostet einen zweiten Zugriff und eine zweite Frist.**
Ein Worker-Pod kann zwischen Übernahme und Abschluss sterben, dann geht die Einreichung verloren. Das gilt für jeden Prozess, auch für einen Monolithen, und braucht dort dieselbe Rückholung. Sie ist als Mechanismus mit Token, Frist und Wiedereinreihung bis `MAX_VERSUCHE` gebaut (Issues #85 und #113). Was die Trennung zusätzlich kostet, ist ein zweiter Zugriff auf MongoDB je Einreichung und die Abstimmung der Fristen von Queue und Datenbank.


**Alternative: ein Dienst auf einer VM ohne Orchestrierung** 
Ein Prozess mit API und Anmeldung, daneben ein fester Pool von Worker-Prozessen, der eingereichte Code läuft wie heute als Subprozess unter runsc, die Datenbank auf derselben Maschine. Wegfallen würden KEDA, das Taint, die Operatoren für MongoDB und PostgreSQL, Longhorn und das Upgrade-Fenster, es bliebe ein Deployment, ein Log, ein Neustart über systemd. Bleiben würden die Sandbox und die Rückholung bei Prozessabsturz. Verloren gingen die elastische Skalierung (der Pool ist auf die Klausurspitze zu dimensionieren und läuft dann dauerhaft) und die Trennung auf Node-Ebene, fremder Code und Datenbank teilen sich einen Kernel. Innerhalb der gewählten Architektur wurde zusätzlich der Container je Testfall verworfen, gemessen 1,2 Sekunden Start je Testfall gegen 20 Millisekunden für den Subprozess (P1).

**Einordnung:**  Für die Kernaufgabe, elastisch auf Klausurlast zu reagieren und fremden Code von den übrigen Diensten zu trennen, ist die cloud-native Lösung angemessen, weil beide Anforderungen aus der Domäne selbst kommen, nicht aus einem allgemeinen Skalierbarkeitsanspruch. Unverhältnismäßig komplex wird sie dort, wo Betrieb und Wartung an einer einzelnen Person hängen.

## Cloud-Native Patterns
**Dynamic Scheduling (Bereich: Infrastructure & Cloud)**: Das Pattern ersetzt die feste Zuordnung "diese Anwendung läuft auf diesem Server" durch einen Orchestrator, der Platzierung, Skalierung und Selbstheilung automatisch übernimmt. In der Anwendung skaliert KEDA die Judge-Worker anhand der Länge der Valkey-Warteschlange `judge:python` von 0 auf bis zu 6 Replicas. Taint und RuntimeClass `gvisor` binden sie an die Judge-Nodes, und Probes sowie der Rückhol-CronJob `durchlauf` holen Ausfälle zurück. *Alternativen* wären eine feste Zahl Worker für die Spitzenlast, ein CPU-basierter Autoscaler oder eine Skalierung der Nodes über Magnum. Der CPU-Trigger scheidet aus, weil er ohne Pod nichts messen und nicht von null starten kann, die Node-Skalierung, weil ein vom Autoscaler erzeugter Node das per Ansible installierte runsc nicht hätte. *Der Trade-off*: Die Nodezahl ist fest, unter Last kommen keine Nodes dazu, der erste Worker braucht von null aus 32 Sekunden, und sechs Worker binden sechs Kerne. 

**Network Isolation (Bereich: Operations)**: Das Pattern sperrt zuerst allen Verkehr zwischen Diensten und gibt nur den erwarteten gezielt frei, in Kubernetes über Network Policies. Im Namespace `judge` gilt ein default-deny in beide Richtungen, dazu je Pod-Art eine eigene Policy. Der Worker erreicht nur MongoDB und Valkey (plus kube-dns), und `scripts/policycheck.sh` prüft je Pod eine erlaubte und eine verbotene Verbindung. Das löst ein Problem der Domäne: Der Worker führt fremden Code aus, und ohne Policy darf jeder Pod jeden anderen erreichen. *Alternativen* wären Freigaben nach Absenderadresse (nach einem Neuaufbau ungeprüft) oder allein die OpenStack Security Group, die nur von außen filtert. *Der Trade-off*: Jede neue Komponente braucht eine eigene Policy, die Freigaben eines neuen Pods kommen gemessen 2 bis 8 Sekunden verzögert an, seine erste Verbindung kann scheitern, und es bleibt ein Restrisiko durch die MongoDB-Zugangsdaten des Workers (siehe Verbleibende Schwachstelle). Das optionale Logging blockierten Verkehrs aus dem Pattern ist nicht umgesetzt. 


## Datenschutz und Datensicherheit

**Welche Daten verarbeitet die Anwendung?** 
Personenbezogen sind die Keycloak-Konten (Benutzername, E-Mail, Vor- und Nachname, Rolle) und jede Einreichung selbst, sie speichert `user_id` und Benutzername aus Keycloak dauerhaft in MongoDB. Fachlich verarbeitet werden eingereichter Quellcode, Bewertungsergebnisse (Urteil, Laufzeit, Speicherverbrauch) und bei Beispieltestfällen zusätzlich Ein- und Ausgabedaten der Aufgaben. Eingereichter Code ist eine Prüfungsleistung und damit sensibel.

**Technisch umgesetzte Schutzmaßnahmen**

- Geheimnisse (Service-Passwörter, TSIG-Key der DNS-Zone, Admin-kubeconfig) liegen mit `sops` und `age` verschlüsselt im Repository, mit eigenem Schlüsselpaar je Person (#77).
- Die Security Group der Nodes lässt über IPv6 nur die Ports 22, 80, 443 und 6443 zu, kubelet (10250) und rpcbind (111) waren vorher offen (#213).
- Im Namespace `judge` gilt Default-Deny in beide Richtungen, ergänzt um punktgenaue Freigaben je Pod-Rolle.
- Der Prüfcode läuft unter gVisor (`runsc`) mit eigener UID je Lauf, begrenzter Rechenzeit, Speicher, Ausgabemenge und Prozesszahl (`RLIMIT_NPROC=0`) und eigenem Netzwerk-Namespace.
- Der Ergebnis-Endpunkt (`/einreichung/{sub_id}`) zeigt je Testfall Urteil, Laufzeit und Speicher. Bei nicht-öffentlichen Testfällen bleiben Eingabe, erwartete und erhaltene Ausgabe verborgen, bei einem Fehlschlag steht dort „Testfall nicht einsehbar“ (#208).
- Der lesende Cluster-Zugriff ist über RBAC auf die Rolle `view` beschränkt, Secrets sind ausgenommen.


**Was vor einem produktiven Einsatz zu ergänzen wäre**
Der feste, nie ablaufende Herkunftswert `X-Gateway-Auth` zwischen Gateway und API ist im produktiven Betrieb ein Schwachpunkt, ein Secret-Leak macht ihn dauerhaft nutzbar, ohne Rotation oder Ablauf. SSH und die Kubernetes-API stehen jeder IPv6-Adresse offen, eine Eingrenzung der Quelle oder ein Jump Host fehlt. Ebenso fehlt eine Verschlüsselung ruhender Daten in MongoDB, eingereichter Code und Ergebnisse liegen dort unverschlüsselt, ein Zugriff auf das Datenvolumen (etwa über einen kompromittierten Dienste-Node) legt alle Einreichungen offen, nicht nur die eines Kurses.

**Relevanz der DSGVO**
Personenbezogen im Sinne der DSGVO sind die Keycloak-Nutzerdaten (Name, E-Mail, Rolle) und ihre Verknüpfung mit Einreichungen. Die Zweckbindung ist im Kern gegeben, die Daten dienen nur der Leistungsbewertung und gehen nicht an Dritte. Der Zugriffsschutz besteht über OIDC-Anmeldung und Header-Prüfung an der API, die Rolle dozent darf aber weiter zugreifen als Studierende, ohne differenzierte Zugriffsprotokollierung. Kritisch ist die Speicherung: Es gibt keine erkennbare Löschfrist für Einreichungen und Nutzerkonten, und ein Realm-Import, der den Keycloak-Stand aus einer Vorlage überschreibt, ist kein datenschutzkonformer Löschprozess für einzelne Betroffene. Die OpenStack-Umgebung ist die der DHBW (newstack.dhbw.cloud, Region DHBW-MA), die Verarbeitung ist damit hochschulintern. Vor einem produktiven Einsatz mit echten Studierendendaten wären eine Aufbewahrungsfrist mit automatisierter Löschung und ein Eintrag im Verzeichnis der Verarbeitungstätigkeiten nach Art. 30 DSGVO nötig, mit Zuständigkeiten, Berechtigungen und technisch-organisatorischen Maßnahmen. Eine Vereinbarung nach Art. 28 gibt es nur zwischen getrennten Rechtspersonen und wird erst mit einem externen Anbieter nötig.

**Verbleibende Schwachstelle**
Wer aus der gVisor-Sandbox ausbricht, erhält über den Worker-Pod direkten Zugriff auf die MongoDB-Zugangsdaten und kann damit alle Einreichungen und Aufgaben lesen und schreiben, nicht nur die eigene Sitzung. Die NetworkPolicy begrenzt den Radius auf MongoDB und Valkey, nicht den Zugriff darin. Diese Schwachstelle ist bewusst in Kauf genommen (siehe W7), bleibt aber vor einem produktiven Einsatz mit echten Prüfungsdaten eine offene Maßnahme. Ein möglicher Weg, den das README unter „Grenzen“ nennt: Der Worker schreibt über die API und hält kein Datenbank-Passwort mehr. Dafür liegt die API im Judge-Pfad, ihr Ausfall trifft jeden Lauf, und Übernahme und Schreiben (bisher je eine Datenbankoperation) wären über HTTP neu zu bauen. 
