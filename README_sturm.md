# Kritische Reflexion: Online-Judge-Cloud (Sturm Teil)

## Architekturreflexion

Der Online Judge korrigiert Programmieraufgaben automatisch gegen hinterlegte
Testfälle. Zwei Eigenschaften der Domäne bestimmen die Bewertung der
Architekturentscheidung: Die Last ist stoßweise, ein ganzer Kurs reicht im
selben Prüfungsfenster ein, dazwischen liegt der Betrieb nahe null. Und der
ausgeführte Code ist grundsätzlich feindlich, Endlosschleifen, Speicherfresser
und Netzzugriffe sind der Normalfall.

**Vorteil 1: Elastische Skalierung genau an der Stelle, die Last erzeugt.**
KEDA skaliert das Worker-Deployment über die Länge der Valkey-Warteschlange
`judge:python` von null auf bis zu sechs Replicas. Gemessen am 11.09.2026 baute
der Cluster aus einer Warteschlange von 207 Einreichungen innerhalb von 58
Sekunden alle sechs Worker auf und leerte die Schlange nach 147 Sekunden. Ohne
Klausur laufen null Worker-Pods, die CPU-Reservierung von sechs vollen Kernen
entsteht nur bei Bedarf. Ein Monolith mit fest laufenden Worker-Threads müsste
diese Kapazität permanent vorhalten oder würde bei Prüfungsbeginn überrannt.

**Vorteil 2: Isolation entlang der eigentlichen Gefahrenquelle.**
Die Judge-Nodes tragen ein eigenes Taint (`online-judge/sandbox=runsc`), nur
die RuntimeClass `gvisor` toleriert es und bindet die Worker-Pods dorthin. Auf
diesen Nodes läuft sonst nichts außer den unvermeidbaren k3s-Addons. Ein
Ausbruch aus der gVisor-Sandbox trifft damit weder MongoDB- noch
Keycloak-Pods, weil diese physisch auf anderen Nodes liegen. Diese Trennung
wäre in einer klassischen VM mit allen Diensten auf einer Maschine so nicht
herstellbar, dort wäre Isolation auf Betriebssystemebene die einzige Grenze
zwischen fremdem Code und der eigenen Datenbank.

**Nachteil 1: Betriebliche Komplexität, die im Zeitfenster zwischen zwei
Klausuren kaum Nutzen stiftet.** Der Stack besteht aus sechs VMs, einem
k3s-Cluster, dem MongoDB-Operator, CloudNativePG, KEDA, Traefik mit einem
OIDC-Plugin, Longhorn, cert-manager, ExternalDNS und dem
system-upgrade-controller. Für einen Dienst, der überwiegend im Leerlauf
steht, ist das eine beachtliche Zahl an Komponenten mit eigenen
Upgrade-Zyklen und Fehlermodi (etwa das k3s-Upgradefenster, das laufende
Judge-Worker per `drain.force` trifft, oder die Heap-Grenze von Keycloak, die
im Lastversuch beobachtet wurde). Ein einzelner Verantwortlicher hält
Terraform-State, Application Credential und DNS-Zone, ein Wechsel dieser
Person oder ein Neuaufbau des Clusters bedeutet Downtime für die gesamte
Gruppe.

**Nachteil 2: Der verteilte Zustand erzwingt eigene Wiederanlauf-Logik statt
sie dem Laufzeit-Framework zu überlassen.** Weil Warteschlange (Valkey) und
Zustand (MongoDB) getrennte Systeme sind, kann eine Einreichung zwischen
Übernahme und Abschluss verloren gehen, wenn ein Worker-Pod stirbt. Die
Lösung ist ein eigens gebauter Rückhol-Mechanismus mit Token, Frist und
Wiedereinreihung bis `MAX_VERSUCHE`, dokumentiert unter anderem in den
Issues #85 und #113. Dieser Mechanismus ist komplexer als ein simples
Try/Except in einem monolithischen Prozess und war nur nötig, weil die
Aufteilung in Queue und Datenhaltung selbst diese Lücke erst schafft.

**Alternative: Ein monolithischer Dienst mit Container-je-Testlauf oder ein
klassisches VM-Setup ohne Orchestrierung.** Konkret wurde intern bereits
gegen einen naheliegenden Ansatz abgewogen: ein Job-Stream mit vollständigem
Auftrag und Ausführung in einem frischen Container je Testfall, statt eines
Subprozesses im dauerhaft laufenden Worker. Der Container-Ansatz kostete
gemessen 1,2 Sekunden Startzeit je Testfall gegenüber 20 Millisekunden für den
Subprozess, bei mehreren hundert Einreichungen mit je mehreren Testfällen ein
Faktor, der in der Klausursituation direkt die Rückmeldezeit an Studierende
verzögert. Eine vollständig monolithische Alternative, ein Prozess auf einer
VM, der Anmeldung, API und Codeausführung in sich vereint, hätte demgegenüber
weder die Lastspitzen elastisch abgefangen noch fremden Code von der
eigentlichen Datenhaltung physisch getrennt, sie wäre aber im Betrieb massiv
einfacher: ein Deployment, ein Log, kein Cluster-Upgrade-Fenster.

**Einordnung:** Für die Kernaufgabe, elastisch auf Klausurlast zu reagieren
und fremden Code von Prüfungsdaten zu isolieren, ist die cloud-native Lösung
angemessen, weil beide Anforderungen aus der Domäne selbst kommen, nicht aus
einem allgemeinen Skalierbarkeitsanspruch. Unverhältnismäßig komplex wird sie
dort, wo Betrieb und Wartung an einer einzelnen Person hängen, denn genau
diese Rolle (Terraform-State, DNS, Credentials) ist der Punkt, an dem
Kubernetes seine Komplexität am wenigsten kompensiert: Ein Cluster, der nur
tageweise läuft, bekommt keinen Automatisierungsvorteil aus ständigem Betrieb,
sondern trägt die volle Betriebskomplexität für seltene Nutzung.

## Cloud-Native Patterns

**Pattern 1: Autoscaling nach externer Metrik (KEDA, ScaledObject).**
Eingesetzt wird das ScaledObject `code-worker-python`, das die
Worker-Replicas nicht an CPU-Auslastung, sondern an der Länge der
Valkey-Liste `judge:python` bemisst. Das Problem, das dieses Pattern in der
Domäne löst: Eine CPU-basierte Skalierung würde erst reagieren, wenn Worker
bereits ausgelastet sind, aber weil jeder Worker mit `Request = Limit` läuft,
steigt seine CPU-Auslastung nie über das Limit, ein CPU-Trigger bliebe blind
für den eigentlichen Engpass, die wachsende Warteschlange. Die Alternative
wäre eine statische Zahl an Worker-Replicas, dimensioniert auf die erwartete
Spitzenlast einer Klausur. Der Trade-off: KEDA fragt die Warteschlange nur
alle 30 Sekunden ab und aktiviert erst über `activationListLength`, wodurch
der erste Anstieg gemessen 32 Sekunden braucht, bis der erste zusätzliche
Worker bereitsteht. Eine statisch vorgehaltene Kapazität hätte diese
Anlaufzeit nicht, kostet aber durchgehend Ressourcen, auch wenn zwischen zwei
Prüfungsterminen keine einzige Einreichung eintrifft.

**Pattern 2: Sidecar-freie Authentifizierung am Edge / Ambassador-artiges
Gateway-Pattern (OIDC-Terminierung in Traefik statt in der Anwendung).**
Die Anmeldung läuft vollständig am Gateway: Traefik führt den OIDC-Flow über
das Plugin `traefik-oidc-auth` gegen Keycloak aus und reicht die Identität
nur noch als Header (`X-Auth-Request-*`) an die API weiter, die selbst keine
Tokens prüft. Das Problem, das dieses Pattern löst, ist die Trennung von
Zugriffskontrolle und Fachlogik, die API bleibt frei von
Token-Validierungscode und OIDC-Bibliotheken, und dieselbe Kennung lässt sich
zusätzlich für lesenden `kubectl`-Zugriff über RBAC wiederverwenden, ohne eine
zweite Identität zu pflegen. Die Alternative wäre ein separater
`oauth2-proxy` als eigener Dienst mit ForwardAuth oder eine Token-Prüfung
direkt in der API gegen die JWKS von Keycloak. Der Trade-off ist konkret
benannt: Die API vertraut einem festen, nie ablaufenden Herkunftswert
(`X-Gateway-Auth`), der im Secret und im Middleware-Objekt steht. Wer diesen
Wert oder das Secret lesen kann, kommt an der Prüfung vorbei, das ist eine
geringere Sicherheitsgarantie als eine kryptographische Signaturprüfung pro
Anfrage, aber dafür bleibt der Judge-Worker, der fremden Code ausführt, von
jeder Kenntnis eines gültigen Tokens ausgeschlossen (er hält weder Token noch
Herkunftswert, `automountServiceAccountToken` steht zusätzlich auf `false`).
Der Tausch ist bewusst: Ein Ausbruch aus der Sandbox soll keine Anmeldedaten
erbeuten können, im Gegenzug wird ein statischer Header als schwächeres,
aber netzwerkintern verbleibendes Vertrauensmerkmal akzeptiert.

## Datenschutz und Datensicherheit

**Welche Daten verarbeitet die Anwendung?** Personenbezogen sind die
Keycloak-Konten (Benutzername, E-Mail, Vor- und Nachname, Rolle) sowie
implizit die Zuordnung von Einreichung zu Person über die Anmeldesitzung.
Fachlich verarbeitet werden eingereichter Quellcode, Bewertungsergebnisse
(Urteil, Laufzeit, Speicherverbrauch) und bei Beispieltestfällen zusätzlich
Ein- und Ausgabedaten der Aufgaben. Eingereichter Code ist potenziell
sensibel, weil er geistige Leistung einer Prüfung darstellt und weil sein
Inhalt bei falscher Handhabung Rückschlüsse auf verborgene Testfälle
zulassen könnte.

**Technisch umgesetzte Schutzmaßnahmen.** Geheimnisse (Servicepasswörter,
TSIG-Key der DNS-Zone, Admin-kubeconfig) liegen mit `sops` und `age`
verschlüsselt im Repository statt im Klartext, mit individuellem
Schlüsselpaar je Person (#77). Im Namespace `judge` gilt ein
Default-Deny für Netzwerkverkehr in beide Richtungen, ergänzt um
punktgenaue Freigaben je Pod-Rolle. Der ausgeführte Prüfcode läuft unter
gVisor (`runsc`) mit eigener UID je Lauf, begrenzter Rechenzeit, Speicher,
Ausgabemenge und Prozesszahl (`RLIMIT_NPROC=0`), zusätzlich isoliert durch
einen eigenen Netzwerk-Namespace. Der Ergebnis-Endpunkt
(`/einreichung/{sub_id}`) gibt bei nicht-öffentlichen Testfällen bewusst
nur "Testfall nicht einsehbar" zurück, um keine Eingabe- oder
Ausgabedaten verborgener Testfälle preiszugeben (#208). Der lesende
Cluster-Zugriff ist über RBAC auf die Rolle `view` beschränkt, Secrets
bleiben davon ausgenommen.

**Was vor einem produktiven Einsatz zu ergänzen wäre.** Der feste,
nie ablaufende Herkunftswert `X-Gateway-Auth` zwischen Gateway und API
ist im produktiven Betrieb ein Schwachpunkt, ein Secret-Leak macht ihn
dauerhaft nutzbar, ohne Rotation oder Ablauf. Ebenso fehlt eine
Verschlüsselung ruhender Daten in MongoDB, eingereichter Code und
Ergebnisse liegen dort unverschlüsselt, ein Zugriff auf das
Datenvolumen (etwa über einen kompromittierten Dienste-Node) legt alle
Einreichungen offen, nicht nur die eines Kurses. Es fehlt außerdem ein
dokumentierter Lösch- oder Aufbewahrungsprozess für Einreichungen nach
Abschluss einer Prüfung, sowie ein Protokoll, wer wann auf welche
Einreichung zugegriffen hat (Audit-Log), was insbesondere bei einer
angezweifelten Bewertung entscheidend wäre.

**Relevanz der DSGVO.** Die in Keycloak gespeicherten Nutzerdaten
(Name, E-Mail, Rolle) sowie die Verknüpfung von Einreichung und Person
sind personenbezogene Daten im Sinne der DSGVO. Die Zweckbindung ist
im Kern gegeben, die Daten dienen ausschließlich der
Leistungsbewertung, eine Weitergabe an Dritte findet nicht statt. Der
Zugriffsschutz ist über OIDC-Anmeldung und Header-Prüfung an der API
grundsätzlich vorhanden, für die Rolle `dozent` aber weiterreichend als
für Studierende, ohne dass eine differenzierte Zugriffsprotokollierung
existiert. Kritisch ist die Speicherung: Es gibt keine erkennbare
Löschfrist für Einreichungen und Nutzerkonten, ein Realm-Import
überschreibt den Keycloak-Stand vollständig anhand einer Vorlage, was
zwar reproduzierbar, aber kein datenschutzkonformer Löschprozess für
einzelne Betroffene ist. Vor einem produktiven Einsatz mit echten
Studierendendaten wäre eine Aufbewahrungsfrist mit automatisierter
Löschung, eine Auftragsverarbeitungsvereinbarung mit dem
Infrastrukturbetreiber (OpenStack-Anbieter) sowie eine Dokumentation
der Verarbeitungstätigkeiten nach Art. 30 DSGVO zwingend erforderlich.

**Verbleibende Schwachstelle.** Wer aus der gVisor-Sandbox ausbricht,
erhält über den Worker-Pod direkten Zugriff auf die
MongoDB-Zugangsdaten und kann damit alle Einreichungen und Aufgaben
lesen und schreiben, nicht nur die eigene Sitzung. Die
NetworkPolicy begrenzt den Ausbruchsradius zwar auf MongoDB und
Valkey, verhindert aber nicht den vollständigen Datenzugriff innerhalb
dieser Grenze. Diese Schwachstelle ist bewusst in Kauf genommen (siehe
W7), bleibt aber vor einem produktiven Einsatz mit echten
Prüfungsdaten eine offene Maßnahme, etwa durch feingranularere
Datenbankrechte je Worker-Instanz statt eines geteilten
Vollzugriffs-Credentials.
