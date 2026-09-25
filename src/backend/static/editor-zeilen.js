// Zeilennummern für den Editor (#252). aufgabe.html trägt seit #56 ein
// echtes Eingabefeld statt des statischen Mockups aus dem Entwurf, die
// Zeilennummern daneben blieben bisher aus. Reines DOM-Skript statt htmx,
// denn nichts davon geht zum Server - reine Anzeige, die mit jedem
// Tastendruck neu zählt.
(function () {
  const eingabe = document.querySelector(".editor-eingabe");
  const zeilen = document.querySelector(".zeilen");
  if (!eingabe || !zeilen) return;

  function aktualisieren() {
    const anzahl = eingabe.value.split("\n").length;
    let html = "";
    for (let i = 1; i <= anzahl; i++) html += `<span>${i}</span>`;
    zeilen.innerHTML = html;
  }

  eingabe.addEventListener("input", aktualisieren);
  aktualisieren();
})();
