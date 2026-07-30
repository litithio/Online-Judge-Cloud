// Lädt die Aufgaben nach MongoDB. Der Titel ist der Schlüssel, damit ein zweiter
// Lauf die Aufgabe aktualisiert statt sie ein zweites Mal anzulegen.
//
// Die Aufgaben stehen als JSON im Repo und nicht in dieser Datei, weil sie so
// auch von anderen Werkzeugen gelesen werden, etwa vom Prüflauf gegen die
// Beispiellösungen in loesungen/. Im Cluster liest denselben Ordner ein Job nach
// dem Ausrollen.
const fs = require("fs");

// Nur die oberste Ebene, loesungen/ bleibt damit außen vor.
const ordner = "/aufgaben";
const dateien = fs
  .readdirSync(ordner)
  .filter((d) => d.endsWith(".json"))
  .sort();

for (const datei of dateien) {
  const aufgabe = JSON.parse(fs.readFileSync(`${ordner}/${datei}`, "utf8"));
  db.tasks.updateOne(
    { title: aufgabe.title },
    {
      $set: {
        description: aufgabe.description,
        test_cases: aufgabe.test_cases,
      },
    },
    { upsert: true },
  );
  print(`${datei}: ${aufgabe.test_cases.length} Testfälle`);
}

print(`${dateien.length} Aufgaben geladen, ${db.tasks.countDocuments()} in der Sammlung`);
