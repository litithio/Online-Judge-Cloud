// Beispielaufgabe für den lokalen Lauf. Der Titel ist der Schlüssel, damit ein
// zweiter Lauf die Aufgabe aktualisiert statt sie ein zweites Mal anzulegen.
db.tasks.updateOne(
  { title: "Summe zweier Zahlen" },
  {
    $set: {
      description:
        "Lies zwei durch Leerzeichen getrennte ganze Zahlen von der " +
        "Standardeingabe und gib ihre Summe aus.",
      test_cases: [
        { input: "3 4", expected_output: "7" },
        { input: "-5 5", expected_output: "0" },
      ],
    },
  },
  { upsert: true },
);
