#!/usr/bin/env bash
# Prüft das Helm-Chart unter app/chart. Lint und Rendern je mit values.yaml
# allein sowie mit dem dev- und dem prod-Overlay, kubeconform über die Renders
# und Negativtests am values-Schema. Der chart-Job in lint.yml ruft genau
# dieses Skript auf, lokal läuft es über ./scripts/check.sh, damit prüfen beide
# dasselbe.
#
# helm und kubeconform laufen im Container statt als lokale Binaries. Auf den
# Entwicklungsrechnern ist kein helm installiert, und der Container hält CI und
# lokalen Lauf auf demselben Stand.
set -euo pipefail
cd "$(dirname "$0")/.."

# Die Helm-Version wird aus ansible/deploy.yaml gelesen statt hier hinterlegt.
# Dort pinnt sie den k3s-Server, der die Charts ausrollt, und geprüft werden
# soll mit derselben Version. Das Muster ist an beiden Enden verankert und
# nimmt den Wert mit beiden Anführungszeichen, ohne sie und mit einem
# nachgestellten Kommentar. Ein unvollständiger oder umschlossener Wert wird
# nicht gekürzt, sondern abgelehnt. Gekürzt zöge er einen Image-Tag, den es
# wirklich gibt, und die Prüfung liefe still gegen einen anderen Stand als das
# Ausrollen. Drei Ausdrücke statt einer Rückwärtsreferenz, weil das Ergebnis
# dann auf BSD und GNU sed dasselbe ist.
helm_version=$(
    sed -nE \
        -e 's/^[[:space:]]*judge_helm_version:[[:space:]]*"v([0-9]+\.[0-9]+\.[0-9]+)"[[:space:]]*(#.*)?$/\1/p' \
        -e "s/^[[:space:]]*judge_helm_version:[[:space:]]*'v([0-9]+\.[0-9]+\.[0-9]+)'[[:space:]]*(#.*)?\$/\1/p" \
        -e 's/^[[:space:]]*judge_helm_version:[[:space:]]*v([0-9]+\.[0-9]+\.[0-9]+)[[:space:]]*(#.*)?$/\1/p' \
        ansible/deploy.yaml
)
# Genau ein Treffer. Eine zweite judge_helm_version in einem anderen Play wäre
# der Weg zurück zu zwei Ständen, und das Skript nähme stumm den ersten.
treffer=$(printf '%s\n' "$helm_version" | grep -c .)
if [ "$treffer" -ne 1 ]; then
    echo "judge_helm_version in ansible/deploy.yaml nicht genau einmal als vX.Y.Z lesbar, Treffer $treffer" >&2
    exit 1
fi
helm_image=alpine/helm:$helm_version
kubeconform_image=ghcr.io/yannh/kubeconform:v0.8.0

# Unter /tmp statt $TMPDIR: Docker auf macOS teilt /tmp, die mktemp-Vorgabe
# /var/folders je nach Einstellung nicht.
render=$(mktemp -d /tmp/chart-check.XXXXXX)
trap 'rm -rf "$render"' EXIT

helm() {
    docker run --rm -v "$PWD/app/chart":/chart "$helm_image" "$@"
}

# Der Image-Tag hat im Schema bewusst keine Vorgabe, ohne --set bricht schon
# das Lint ab. --strict macht Warnungen zu Fehlern, damit eine schludrige
# Vorlage nicht durchrutscht. Gelintet wird gegen dieselben drei Stände wie
# gerendert, weil dev und prod sich in Replicas und resources unterscheiden und
# ein Fehler nur in einem auftreten kann.
echo "== helm lint =="
helm lint /chart --strict --set image.tag=0.0.0-test
helm lint /chart --strict -f /chart/values-dev.yaml --set image.tag=0.0.0-test
helm lint /chart --strict -f /chart/values-prod.yaml --set image.tag=0.0.0-test

# Drei Stände: values.yaml allein, dazu je das dev- und das prod-Overlay.
# kubeconform prüft die gerenderten Manifeste gegen die Kubernetes-Schemas.
# Das KEDA-ScaledObject ist eine CRD ohne Schema im Katalog, darum
# -ignore-missing-schemas, alles andere wird streng geprüft.
echo "== rendern und mit kubeconform validieren =="
helm template /chart --set image.tag=0.0.0-test > "$render/basis.yaml"
helm template /chart -f /chart/values-dev.yaml --set image.tag=0.0.0-test > "$render/dev.yaml"
helm template /chart -f /chart/values-prod.yaml --set image.tag=0.0.0-test > "$render/prod.yaml"
docker run --rm -v "$render":/render "$kubeconform_image" \
    -strict -ignore-missing-schemas -summary \
    /render/basis.yaml /render/dev.yaml /render/prod.yaml

# values.schema.json ist die einzige Stelle, die kaputte judge-Werte abfängt.
# kubelet ignoriert ein Limit von 0 still und beliebiger Text fiele erst am
# API-Server auf. helm muss hier jedes Mal scheitern, und zwar an dem
# Schlüssel im ersten Parameter, nicht an etwas anderem.
echo "== Negativtests am values-Schema =="
muss_scheitern() {
    local schluessel="$1"
    shift
    if helm template /chart --set image.tag=0.0.0-test "$@" > /dev/null 2> "$render/fehler.txt"; then
        echo "Schema hat $* durchgelassen"
        exit 1
    fi
    if ! grep -q "$schluessel" "$render/fehler.txt"; then
        cat "$render/fehler.txt"
        echo "helm ist an etwas anderem gescheitert als an $schluessel ($*)"
        exit 1
    fi
    echo "abgelehnt wie erwartet: $*"
}
# null löscht den Schlüssel aus den values, das prüft den Pflichtfeld-Fall.
muss_scheitern tmpSizeLimit --set judge.tmpSizeLimit=null
# --set-string erzwingt Strings, sonst käme die 0 als Zahl an und scheiterte
# am Typ statt am Muster.
muss_scheitern tmpSizeLimit --set-string judge.tmpSizeLimit=
muss_scheitern tmpSizeLimit --set-string judge.tmpSizeLimit=0
muss_scheitern tmpSizeLimit --set-string judge.tmpSizeLimit=ganzviel
# Frist für den geordneten Auslauf des Workers (#199). 67 liegt unter dem
# Minimum von 68 aus values.schema.json, darunter kann schon ein einzelner
# Testfall am maximalen Zeitlimit nicht zu Ende gerechnet werden.
muss_scheitern terminationGracePeriodSeconds --set judge.terminationGracePeriodSeconds=null
muss_scheitern terminationGracePeriodSeconds --set judge.terminationGracePeriodSeconds=67
# Argumente des Lastgenerators (#275). Ein Job aus der CronJob-Vorlage lässt
# sich nicht mehr umparametrieren, ein kaputter Wert fiele erst im Pod auf.
muss_scheitern rate --set lastgenerator.rate=0
muss_scheitern mix --set lastgenerator.mix=schnell=1

# Die Frist des Lastgenerator-Jobs muss den Fall decken, dass jede Einreichung
# in den Client-Timeout läuft. 1000 Einreichungen sind 32 angefangene Runden
# des Pools zu je 10 Sekunden, dazu 120 Reserve und die Dauer 1, also 441.
echo "== Frist des Lastgenerator-Jobs =="
frist=$(helm template /chart --set image.tag=0.0.0-test --set lastgenerator.rate=1000 --set lastgenerator.dauer=1 \
    --show-only templates/lastgenerator.yaml | sed -nE 's/^ *activeDeadlineSeconds: ([0-9]+)$/\1/p')
if [ "$frist" != "441" ]; then
    echo "activeDeadlineSeconds bei rate 1000 und dauer 1 ist $frist statt 441"
    exit 1
fi
echo "Frist bei rate 1000 und dauer 1 ist 441 wie erwartet"
