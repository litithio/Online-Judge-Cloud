#!/usr/bin/env bash
# Prüft das Helm-Chart unter app/chart: Lint, Rendern mit values.yaml sowie
# dem dev- und dem prod-Overlay, kubeconform über die Renders und Negativtests
# am values-Schema. Der chart-Job in lint.yml ruft genau dieses Skript auf,
# lokal läuft es über ./scripts/check.sh, damit prüfen beide dasselbe.
#
# helm und kubeconform laufen im Container statt als lokale Binaries: auf den
# Entwicklungsrechnern ist kein helm installiert, und die gepinnten Images
# halten CI und lokalen Lauf auf demselben Stand. Wer eine Version wechselt,
# ändert sie nur hier.
set -euo pipefail
cd "$(dirname "$0")/.."

helm_image=alpine/helm:3.19.0
kubeconform_image=ghcr.io/yannh/kubeconform:v0.8.0

# Unter /tmp statt $TMPDIR: Docker auf macOS teilt /tmp, die mktemp-Vorgabe
# /var/folders je nach Einstellung nicht.
render=$(mktemp -d /tmp/chart-check.XXXXXX)
trap 'rm -rf "$render"' EXIT

helm() {
    docker run --rm -v "$PWD/app/chart":/chart "$helm_image" "$@"
}

# Der Image-Tag hat im Schema bewusst keine Vorgabe, ohne --set bricht schon
# das Lint ab.
echo "== helm lint =="
helm lint /chart --set image.tag=0.0.0-test

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

# values.schema.json ist die einzige Stelle, die einen kaputten tmpSizeLimit
# abfängt. kubelet ignoriert ein Limit von 0 still und beliebiger Text fiele
# erst am API-Server auf. helm muss hier jedes Mal scheitern, und zwar an
# tmpSizeLimit, nicht an etwas anderem.
echo "== Negativtests am values-Schema =="
muss_scheitern() {
    if helm template /chart --set image.tag=0.0.0-test "$@" > /dev/null 2> "$render/fehler.txt"; then
        echo "Schema hat $* durchgelassen"
        exit 1
    fi
    if ! grep -q tmpSizeLimit "$render/fehler.txt"; then
        cat "$render/fehler.txt"
        echo "helm ist an etwas anderem gescheitert als an tmpSizeLimit ($*)"
        exit 1
    fi
    echo "abgelehnt wie erwartet: $*"
}
# null löscht den Schlüssel aus den values, das prüft den Pflichtfeld-Fall.
muss_scheitern --set judge.tmpSizeLimit=null
# --set-string erzwingt Strings, sonst käme die 0 als Zahl an und scheiterte
# am Typ statt am Muster.
muss_scheitern --set-string judge.tmpSizeLimit=
muss_scheitern --set-string judge.tmpSizeLimit=0
muss_scheitern --set-string judge.tmpSizeLimit=ganzviel
