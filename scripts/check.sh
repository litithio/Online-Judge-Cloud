#!/usr/bin/env bash
# Bündelt die Prüfungen vor dem Push. Jeder Schritt läuft, auch wenn ein
# vorheriger fehlgeschlagen ist: sonst repariert man Terraform, startet neu und
# erfährt erst dann, dass ruff auch etwas hat.
#
# Dieses Skript prüft nichts selbst, es ruft die vorhandenen Prüfungen
# nacheinander auf und wertet die Exit-Codes aus. Die Einzelaufrufe bleiben
# gültig, die CI ruft weiterhin genau die auf.
set -uo pipefail

cd "$(dirname "$0")/.."

# Wie in infra-check.sh. Ohne aktive Projektumgebung greift sonst ein systemweit
# installiertes ruff und meldet andere Regeln als die CI. Exportiert, damit
# infra-check.sh als Kindprozess dieselbe PATH sieht.
if [ -n "${VIRTUAL_ENV:-}" ] && [ -d "$VIRTUAL_ENV/bin" ]; then
  PATH="$VIRTUAL_ENV/bin:$PATH"
elif [ -d .venv/bin ]; then
  PATH="$PWD/.venv/bin:$PATH"
fi
export PATH

# Vorab abfangen, sonst steht am Ende "FEHLGESCHLAGEN ruff check" samt einem
# Reparaturvorschlag, der das eigentliche Problem verdeckt.
if ! command -v ruff >/dev/null; then
  echo "ruff nicht gefunden: die Projektumgebung ist nicht eingerichtet oder nicht aktiv." >&2
  echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# Wie check_pin in infra-check.sh, nur für ruff: ohne Projektumgebung greift
# oben ein systemweit installiertes ruff, und eine abweichende Version meldet
# andere Regeln als die CI. Gewarnt statt abgebrochen, weil eine Abweichung
# nicht zwingend zu einem anderen Ergebnis führt.
warnung=''
ruff_pin=$(grep -oE '^ruff==[0-9.]+' requirements.txt 2>/dev/null | cut -d= -f3)
ruff_ist=$(ruff --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [ -n "$ruff_pin" ] && [ -n "$ruff_ist" ] && [ "$ruff_ist" != "$ruff_pin" ]; then
  warnung="ruff $ruff_ist, gepinnt ist $ruff_pin. Abhilfe: pip install -r requirements.txt"
fi

if [ -t 1 ]; then
  rot=$'\033[31m'; gruen=$'\033[32m'; gelb=$'\033[33m'; aus=$'\033[0m'
else
  rot=''; gruen=''; gelb=''; aus=''
fi

namen=(); status=(); abhilfe=()

# Erster Parameter ist der Name in der Zusammenfassung, zweiter der Hinweis bei
# einem Fehlschlag, danach folgt der eigentliche Aufruf. Die Überschrift nutzt
# eine andere Marke als das === von infra-check.sh, damit die verschachtelten
# Ausgaben unterscheidbar bleiben.
schritt() {
  local name="$1" hinweis="$2" code
  shift 2
  printf '\n######## %s\n' "$name"
  "$@"
  code=$?
  namen+=("$name")
  if [ "$code" -eq 0 ]; then
    status+=("ok"); abhilfe+=("")
  else
    status+=("FEHLGESCHLAGEN"); abhilfe+=("$hinweis")
  fi
}

# Nur der Abgleich, nicht das Rendern: ein Skript namens check soll den
# Arbeitsbaum nicht verändern. Verglichen werden die Zeitstempel, deshalb kann
# ein frischer Klon einmal fälschlich melden, die SVGs seien veraltet. Ein
# überflüssiger Lauf von diagramme.sh ist folgenlos, das Rendern ist byte-gleich.
diagramme_pruefen() {
  local quelle ziel code=0
  for quelle in docs/diagramme/*.mmd; do
    [ -e "$quelle" ] || continue
    ziel="${quelle%.mmd}.svg"
    if [ ! -e "$ziel" ] || [ "$quelle" -nt "$ziel" ]; then
      echo "    $quelle ist neuer als $ziel"
      code=1
    fi
  done
  [ "$code" -eq 0 ] && echo "    alle SVGs aktuell"
  return "$code"
}

schritt "Terraform und Ansible" "einzeln erneut: ./scripts/infra-check.sh" \
        ./scripts/infra-check.sh
schritt "ruff check" "teilweise behebbar mit: ruff check --fix ." \
        ruff check .
schritt "ruff format" "beheben mit: ruff format ." \
        ruff format --check .
schritt "Diagramme" "beheben mit: ./scripts/diagramme.sh" \
        diagramme_pruefen
schritt "Unit-Tests" "einzeln erneut: ./scripts/unit-tests.sh" \
        ./scripts/unit-tests.sh
schritt "Helm-Chart" "einzeln erneut: ./scripts/chart-check.sh" \
        ./scripts/chart-check.sh

# Ohne diese Zusammenfassung stehen bei einem Fehlschlag mehrere hundert Zeilen
# Werkzeugausgabe im Terminal und die Zeile, die zählt, ist weggescrollt.
printf '\n######## Zusammenfassung\n'
fehler=0
for i in "${!namen[@]}"; do
  if [ "${status[$i]}" = "ok" ]; then
    farbe="$gruen"
  else
    farbe="$rot"
    fehler=$((fehler + 1))
  fi
  printf '    %s%-14s%s %s\n' "$farbe" "${status[$i]}" "$aus" "${namen[$i]}"
  [ -n "${abhilfe[$i]}" ] && printf '    %-14s %s\n' "" "${abhilfe[$i]}"
done

# Gehört in die Zusammenfassung und nicht nur an den Anfang: oben gemeldet wäre
# sie nach dreißig Zeilen Werkzeugausgabe weggescrollt.
[ -n "$warnung" ] && printf '    %s%-14s%s %s\n' "$gelb" "WARNUNG" "$aus" "$warnung"

printf '\n'
if [ "$fehler" -eq 0 ]; then
  # Kein "Alles ok." neben einer Warnung: die Schlusszeile ist das, worauf man
  # sich verlässt, und sie soll nicht mehr behaupten als sie geprüft hat.
  if [ -n "$warnung" ]; then
    echo "Keine Fehler, aber die Warnung oben beachten."
  else
    echo "Alles ok."
  fi
  exit 0
fi
echo "$fehler von ${#namen[@]} Schritten fehlgeschlagen."
exit 1
