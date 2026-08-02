# Entwurf der Oberfläche

Neun statische Seiten im DHBW-Design, ohne JavaScript. `index.html` listet
sie auf und verlinkt sie untereinander; im Browser öffnen genügt.

Der Entwurf ist keine Wegwerfarbeit: Aus den sieben Anwendungsseiten werden
die Jinja2-Templates des API-Service, aus `dhbw.css` wird `static/dhbw.css`.
Im Markup steht je Seite, welches Template daraus wird.

## Corporate Design

Grundlage ist das DHBW Corporate Design Manual, Kapitel 1.6 "Corporate Design
Masterstudiengänge" (Karrieremarke). Die Werte stehen als CSS-Variablen im
`:root` von `dhbw.css`, jeweils mit dem Kapitel, aus dem sie stammen:

| | Wert | Herkunft |
|---|---|---|
| Rot HKS 14 | `#e2001a` | 1.3.1, nur als Akzent, keine Aufrasterung |
| Grau HKS 92 | `#5c6971` | 1.3.1, trägt die Fläche, Stufen 25/50/75 % |
| Schriften | Arial, Times New Roman | 1.2.2 Ersatzschriften |
| Eckenrundung | Höhe mal 0,04 | 1.6.2 |

Zwei bewusste Abweichungen. 1.6.4 verlangt Serif auch im Fließtext; in einer
Oberfläche mit Quelltext bleibt der Fließtext Arial, Serif tragen nur die
Überschriften. Und das CD kennt kein Grün: Ein bestandener Testfall ist grau
mit Häkchen, Rot bleibt der Fehlerfarbe vorbehalten. Das Signal hängt damit
am Symbol und nicht an der Farbe.

Das Logo ist die CAS-Sonderform als JPG. Eine Vektorfassung ohne Zusatzzeile
ist bei `cd@dhbw.de` angefragt und ersetzt `logo.jpg`, sobald sie vorliegt.

## Was beim Umsetzen zu beachten ist

`login.html` gehört **nicht** zum API-Service. Traefik leitet unangemeldete
Anfragen per ForwardAuth zu Keycloak um, das seine eigene Seite ausliefert.
Aus dem Entwurf wird ein Keycloak-Theme aus `dhbw.css` und Logo; die Felder
kommen weiter aus Keycloaks `login.ftl`. Alle Anmelderegeln hängen deshalb an
`.anmeldung`, damit dieselbe CSS-Datei in beiden Diensten laufen kann.

`fehler.html` wird ein gewöhnliches Template und deckt die Fehler ab, die die
Anwendung selbst bemerkt: MongoDB oder Valkey nicht erreichbar, Aufgabe nicht
vorhanden. Fällt ein API-Pod aus, übernimmt die zweite Replica. Erst wenn keine
Replica mehr antwortet, greift die Seite nicht mehr und Traefik liefert seine
Standardmeldung. Eine `errors`-Middleware auf einen eigenen Dienst wäre die
Erweiterung dafür; sie ist zurückgestellt, bis die Standardseite tatsächlich
stört (#15).

`ergebnis-laeuft.html` nennt eine Wiederaufnahme nach einem Worker-Ausfall.
Das setzt voraus, dass der Delivery-Counter aus dem Valkey-Stream im Document
landet; ohne ihn entfällt der Absatz.

Der Editor ist im Entwurf ein `pre` mit Zeilennummern. Dort steht später
Monaco.
