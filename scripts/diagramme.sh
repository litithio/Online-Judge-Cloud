#!/usr/bin/env bash
# Rendert die Mermaid-Quellen aus docs/diagramme/ zu SVGs daneben. Nach
# jeder Änderung an einer .mmd laufen lassen und beide Dateien committen,
# das README bindet die SVGs ein.
#
# -b white: Als statisches SVG passt sich das Diagramm nicht mehr an das
# GitHub-Farbschema an; auf festem weißem Grund bleibt es auch im Dark
# Mode lesbar.
set -euo pipefail
cd "$(dirname "$0")/.."

# Schrift und weitere Render-Einstellungen stehen als Frontmatter in den
# .mmd-Dateien selbst, hier kommt nur der Hintergrund dazu.
#
# Version gepinnt, damit Entwicklungsrechner und CI byte-gleich rendern;
# der Diagramm-Job in lint.yml vergleicht das Ergebnis mit dem Commit.
# Wer die Version wechselt, erzeugt die SVGs im selben Commit neu, sonst
# schlägt genau dieser Abgleich fehl.
for quelle in docs/diagramme/*.mmd; do
    npx -y @mermaid-js/mermaid-cli@11.16.0 -i "$quelle" -o "${quelle%.mmd}.svg" -b white
done
