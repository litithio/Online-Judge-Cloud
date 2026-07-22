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
Code ist fremd — Endlosschleifen, Speicherfresser und Zugriffsversuche
auf das Netz sind der Normalfall, nicht die Ausnahme.

Über Container-Images können für jede Sprache (Python, C++, Java, Rust) maßgeschneiderte, schlanke Laufzeitumgebungen bereitgestellt werden, ohne den Host-Worker mit Abhängigkeiten zu überladen.

## Architektur
Diagramm und Datenfluss, von der VM bis zum Pod
## Betrieb
Wie man den Stack hochbringt, exakte Kommandos
## Entscheidungen
Je Pflicht- und Wahlthema ein Absatz: Wahl, Alternative, Begründung
## Grenzen
Was die Lösung nicht kann und was Sie unter echter Last erwarten
## Bonus
Nur falls Sie Bonuspunkte beanspruchen, sonst weglassen
