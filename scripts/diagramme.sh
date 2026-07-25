#!/usr/bin/env bash
# Rendert die Mermaid-Quellen aus docs/diagramme/ zu SVGs daneben. Nach
# jeder Änderung an einer .mmd laufen lassen und beide Dateien committen,
# das README bindet die SVGs ein.
#
# Gerendert wird im Container statt mit npx: Schriften und Chromium
# kommen aus dem Image, damit erzeugen Entwicklungsrechner und CI
# byte-gleiche SVGs. Mit Systemschriften scheiterte das an den
# Emoji-Metriken, macOS und Linux messen sie unterschiedlich breit.
# Der Diagramm-Job in lint.yml vergleicht das Ergebnis mit dem Commit;
# wer die Image-Version wechselt, erzeugt die SVGs im selben Commit neu.
#
# -b white: Als statisches SVG passt sich das Diagramm nicht mehr an das
# GitHub-Farbschema an; auf festem weißem Grund bleibt es auch im Dark
# Mode lesbar.
set -euo pipefail
cd "$(dirname "$0")/.."

for quelle in docs/diagramme/*.mmd; do
    docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/data" \
        minlag/mermaid-cli:11.16.0 \
        -i "/data/$quelle" -o "/data/${quelle%.mmd}.svg" -b white
done
