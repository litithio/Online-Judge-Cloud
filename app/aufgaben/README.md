# Aufgaben

Eine JSON je Aufgabe, dazu in `../chart/loesungen/` je eine Lösung, die
bestehen muss, und eine, die an einem Limit scheitern muss. Die Lösungen
liegen im Chart, weil der Prüflauf aus #19 sie als helm-test-Job von dort in
den Cluster rollt. `laden.py` schreibt die
Aufgaben nach MongoDB. Der Titel ist dabei der Schlüssel, ein zweiter Lauf
aktualisiert die Aufgabe also, statt sie erneut anzulegen. Ein geänderter Titel
legt dagegen eine zweite Aufgabe an und lässt die alte stehen, denn `laden.py`
entfernt nichts aus der Datenbank. Das Ausführen im Cluster ist Teil der
Infrastruktur und nicht des App-Deployments, der Seed lädt die Dateien hier in
die vorhandene MongoDB.

## Format

```json
{
  "title": "Primzahlen zählen",
  "description": "Zähle die Primzahlen bis zu einer Grenze.",
  "input_format": "Eine Zeile mit der Zahl n.",
  "output_format": "Die Anzahl der Primzahlen kleiner oder gleich n.",
  "difficulty": "mittel",
  "time_limit_seconds": 2,
  "memory_limit_mb": 96,
  "test_cases": [
    { "name": "Bis 100", "sample": true, "input": "100\n", "expected_output": "25" }
  ]
}
```

`title`, `description`, `input_format`, `output_format`, `difficulty` und
`test_cases` sind Pflicht. `input` und `expected_output` müssen Zeichenketten
sein, auch bei Zahlen.

Die Aufgabenseite zeigt `description` als Einleitung und `input_format` und
`output_format` unter den Überschriften Eingabe und Ausgabe. Alles ist
einfacher Text, das Markup liegt im Template.

`difficulty` ist `leicht`, `mittel` oder `schwer` und steht als Marke in der
Aufgabenliste. `name` je Testfall ist Pflicht und beschriftet die Zeile des
Testfalls auf der Ergebnisseite. Er soll sagen, was der Fall prüft, die Eingabe
selbst bleibt verborgen.

`sample` markiert einen Testfall als Beispiel. Die Aufgabenseite zeigt seine
Eingabe und Sollausgabe unter der Überschrift Beispiel, alle anderen Testfälle
bleiben verborgen. Mindestens ein Testfall je Aufgabe braucht die Markierung,
sonst lehnt `laden.py` die Datei ab. Ein eigenes Beispiel-Textfeld gibt es
bewusst nicht, ein von Hand gepflegtes Beispiel könnte von den echten
Testfällen abweichen.

`time_limit_seconds` und `memory_limit_mb` sind freiwillig. Fehlen sie, gelten
die Vorgaben aus `../worker/worker.py`. Erlaubt sind höchstens 60 Sekunden und
256 MB, darüber lehnt `laden.py` die Datei ab: Eine Aufgabe soll das Zeitlimit
nicht abschalten und nicht mehr Speicher erlauben, als der Worker-Container
insgesamt hat.

## Eine Aufgabe dazulegen

Die erwartete Ausgabe nie von Hand rechnen, sondern die eigene Lösung gegen die
Eingabe laufen lassen und ihre Ausgabe übernehmen. Ein früherer Satz Aufgaben
hatte zwei von Hand gerechnete Werte, beide falsch.

Als Limit taugt etwa das Drei- bis Vierfache dessen, was die eigene Lösung
braucht. Dann kommt auch eine langsamere richtige Lösung durch, und eine, die
den falschen Algorithmus nimmt, scheitert trotzdem.

Unter `../chart/loesungen/<name>/` gehören `akzeptiert.py` und, je nachdem was die
Aufgabe zeigen soll, `zeitlimit.py` oder `speicherlimit.py`. Der Dateiname sagt,
welches Urteil der Judge fällen muss. Beide Lösungen rechnen richtig, sie
unterscheiden sich nur im Verbrauch, sonst prüft der Lauf das Falsche.
