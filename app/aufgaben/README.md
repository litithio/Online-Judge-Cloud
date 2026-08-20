# Aufgaben

Eine JSON je Aufgabe, darunter in `loesungen/` je eine Lösung, die bestehen
muss, und eine, die an einem Limit scheitern muss. `laden.py` schreibt die
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
  "description": "Lies eine Zahl n von der Standardeingabe und gib aus, ...",
  "time_limit_seconds": 2,
  "memory_limit_mb": 96,
  "test_cases": [
    { "input": "100\n", "expected_output": "25" }
  ]
}
```

`title`, `description` und `test_cases` sind Pflicht. `input` und
`expected_output` müssen Zeichenketten sein, auch bei Zahlen.

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

Unter `loesungen/<name>/` gehören `akzeptiert.py` und, je nachdem was die
Aufgabe zeigen soll, `zeitlimit.py` oder `speicherlimit.py`. Der Dateiname sagt,
welches Urteil der Judge fällen muss. Beide Lösungen rechnen richtig, sie
unterscheiden sich nur im Verbrauch, sonst prüft der Lauf das Falsche.
