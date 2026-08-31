#!/usr/bin/env bash
# Abnahme nach dem Deploy (#19). Wartet auf den Rollout der Chart-Workloads,
# lässt helm test laufen und prüft danach, was keine fertige Regel abdeckt,
# den Pod-Verkehr über Node-Grenzen, die eigene Metrik in Prometheus und den
# Wert des ScaledObject. Zustandsprüfungen wie CrashLooping, ungebundene PVCs
# oder fehlende Replicas kommen als Alert-Regeln aus dem kube-prometheus-stack
# und werden hier nicht nachgebaut.
#
# Jeder Schritt läuft, auch wenn ein vorheriger fehlgeschlagen ist, und die
# Zusammenfassung nennt je Fehlschlag das nächste Kommando, wie in check.sh.
#
# helm liegt nicht lokal und der Docker-Weg aus chart-check.sh scheidet aus,
# ein Container auf macOS erreicht keine IPv6-only-Ziele, die Cluster-API ist
# eines. helm test läuft deshalb per SSH auf dem Server-Node, dort pinnt
# ansible/deploy.yaml das Binary. Die Adresse kommt aus der KUBECONFIG, sie
# ist dieselbe wie die der Cluster-API.
set -uo pipefail

cd "$(dirname "$0")/.."

# Ein Aufruf je Name statt Kopie der Werte. Der Namespace steht in
# ansible/vars/app.yaml, der Release-Name in ansible/tasks/app.yaml, beide an
# genau einer Stelle. Wie in chart-check.sh wird genau ein Treffer verlangt,
# bei zwei Ständen nähme das Skript sonst stumm den ersten.
lies_einmal() {
  local wert
  wert=$(sed -nE "$2" "$3")
  if [ "$(printf '%s\n' "$wert" | grep -c .)" -ne 1 ]; then
    echo "$1 in $3 nicht genau einmal lesbar" >&2
    exit 1
  fi
  printf '%s' "$wert"
}
# Das || exit 1 gehört an die Zuweisung. Das exit in lies_einmal beendet nur
# die Subshell der Kommandosubstitution, ohne die Prüfung hier liefe das
# Skript mit leerem Wert weiter.
namespace=$(lies_einmal app_namespace \
  's/^app_namespace:[[:space:]]*"?([a-z0-9-]+)"?[[:space:]]*(#.*)?$/\1/p' \
  ansible/vars/app.yaml) || exit 1
release=$(lies_einmal name \
  's/^    name:[[:space:]]*([a-z0-9-]+)[[:space:]]*(#.*)?$/\1/p' \
  ansible/tasks/app.yaml) || exit 1

# Ohne erreichbare Cluster-API ist jeder Schritt unten derselbe Fehlschlag.
if ! kubectl get nodes >/dev/null 2>&1; then
  echo "kubectl erreicht den Cluster nicht." >&2
  echo "    VPN aus? KUBECONFIG kommt aus der .envrc, direnv allow oder source .envrc" >&2
  exit 1
fi

if [ -t 1 ]; then
  rot=$'\033[31m'; gruen=$'\033[32m'; aus=$'\033[0m'
else
  rot=''; gruen=''; aus=''
fi

namen=(); status=(); abhilfe=()

# Wie in check.sh, andere Marke als dort, damit verschachtelte Ausgaben
# unterscheidbar bleiben.
schritt() {
  local name="$1" hinweis="$2" code
  shift 2
  printf '\n==== %s\n' "$name"
  "$@"
  code=$?
  namen+=("$name")
  if [ "$code" -eq 0 ]; then
    status+=("ok"); abhilfe+=("")
  else
    status+=("FEHLGESCHLAGEN"); abhilfe+=("$hinweis")
  fi
}

# ---------- Rollout je Workload ----------
# Die Deployments des Release. Die Worker stehen ohne Einreichungen auf null
# Replicas, rollout status meldet sie dann sofort als fertig, das ist der
# gewollte Zustand. Der CronJob und das ScaledObject haben keinen Rollout.
rollout_pruefen() {
  local code=0 deployment deployments
  deployments=$(kubectl -n "$namespace" get deployments \
    -l "app.kubernetes.io/instance=$release" -o name)
  if [ -z "$deployments" ]; then
    echo "    Kein Deployment mit app.kubernetes.io/instance=$release gefunden."
    return 1
  fi
  for deployment in $deployments; do
    kubectl -n "$namespace" rollout status "$deployment" --timeout=180s || code=1
  done
  return "$code"
}

# ---------- helm test auf dem Server ----------
# --logs zeigt die Ausgabe der Testjobs direkt im Lauf. Der Timeout deckt den
# Prüflauf gegen die Beispiellösungen, seine Herleitung steht in
# app/chart/tests/loesungen_pruefen.py. Hostkey-Prüfung aus wie in
# ansible/ansible.cfg, die Nodes entstehen bei jedem Frisch-Deployment neu.
helm_test() {
  local server
  server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
  server=${server#https://}
  server=${server%:*}
  server=${server#[}
  server=${server%]}
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR "ubuntu@$server" \
      "sudo helm test $release --namespace $namespace --kubeconfig /etc/rancher/k3s/k3s.yaml --logs --timeout 15m"
}

# ---------- Pod auf Node A erreicht Pod auf Node B ----------
# Die beiden API-Replicas liegen per Anti-Affinity auf verschiedenen Nodes,
# der Aufruf von der einen zur Pod-IP der anderen prüft also genau den Verkehr
# über die Node-Grenze, den ein Service sonst verdeckt. python3 statt curl,
# das Backend-Image bringt kein curl mit.
pod_zu_pod() {
  local zeilen erste knoten zweite quelle ziel_ip
  zeilen=$(kubectl -n "$namespace" get pods -l app=backend \
    --field-selector status.phase=Running \
    -o jsonpath='{range .items[*]}{.metadata.name} {.spec.nodeName} {.status.podIP}{"\n"}{end}')
  erste=$(printf '%s\n' "$zeilen" | sed -n 1p)
  knoten=$(printf '%s' "$erste" | awk '{print $2}')
  zweite=$(printf '%s\n' "$zeilen" | awk -v knoten="$knoten" '$2 != knoten {print; exit}')
  if [ -z "$erste" ] || [ -z "$zweite" ]; then
    echo "    Keine zwei laufenden backend-Pods auf verschiedenen Nodes."
    printf '%s\n' "$zeilen"
    return 1
  fi
  quelle=$(printf '%s' "$erste" | awk '{print $1}')
  ziel_ip=$(printf '%s' "$zweite" | awk '{print $3}')
  case "$ziel_ip" in *:*) ziel_ip="[$ziel_ip]" ;; esac
  echo "    $quelle ($knoten) fragt $(printf '%s' "$zweite" | awk '{print $1, "auf", $2}')"
  kubectl -n "$namespace" exec "$quelle" -- python3 -c "
import urllib.request
antwort = urllib.request.urlopen('http://$ziel_ip:8000/healthz', timeout=5)
print('    HTTP', antwort.status)"
}

# ---------- Prometheus liefert die eigene Metrik ----------
# keda_scaler_metrics_value ist die Länge der Judge-Queue aus dem
# KEDA-Operator, eine der beiden Kurven des Dashboards. Sie kommt nur an,
# wenn der ServiceMonitor aus ansible/tasks/observability.yaml greift, und
# genau das prüft der Schritt. Abgefragt wird im Prometheus-Pod selbst, sein
# Port ist von außen nicht erreichbar. promtool statt wget, das
# Prometheus-Image ab v3 bringt kein wget mehr mit, gemessen am 31.08.
prometheus_pruefen() {
  local pod antwort
  pod=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -z "$pod" ]; then
    echo "    Kein Prometheus-Pod im Namespace monitoring."
    return 1
  fi
  antwort=$(kubectl -n monitoring exec "$pod" -c prometheus -- \
    promtool query instant http://localhost:9090 keda_scaler_metrics_value)
  if printf '%s' "$antwort" | grep -q '^keda_scaler_metrics_value{'; then
    printf '%s\n' "$antwort" | sed 's/^/    /'
    return 0
  fi
  echo "    keda_scaler_metrics_value ohne Zeitreihen, Antwort:"
  printf '    %s\n' "$antwort"
  return 1
}

# ---------- ScaledObject meldet einen Wert ----------
# Nicht nur die Ready-Bedingung, sondern der Weg, den auch der HPA nimmt, eine
# Abfrage der External-Metrics-API. Erst wenn dort ein Wert steht, kann KEDA
# aus der Queue-Länge Replicas machen.
scaledobject_pruefen() {
  local code=0 so metrik antwort
  local objekte
  objekte=$(kubectl -n "$namespace" get scaledobjects \
    -l "app.kubernetes.io/instance=$release" -o jsonpath='{.items[*].metadata.name}')
  if [ -z "$objekte" ]; then
    echo "    Kein ScaledObject mit app.kubernetes.io/instance=$release gefunden."
    return 1
  fi
  for so in $objekte; do
    metrik=$(kubectl -n "$namespace" get scaledobject "$so" \
      -o jsonpath='{.status.externalMetricNames[0]}')
    if [ -z "$metrik" ]; then
      echo "    $so trägt keinen externalMetricName."
      code=1
      continue
    fi
    # Der Doppelpunkt aus dem Listennamen (judge:python) muss in der URL
    # kodiert sein, ebenso Schrägstrich und Gleichheitszeichen im Selector.
    antwort=$(kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/$namespace/${metrik//:/%3A}?labelSelector=scaledobject.keda.sh%2Fname%3D$so")
    if printf '%s' "$antwort" | grep -q '"value"'; then
      echo "    $so meldet $metrik = $(printf '%s' "$antwort" | sed -nE 's/.*"value":"([^"]*)".*/\1/p')"
    else
      echo "    $so meldet keinen Wert für $metrik, Antwort:"
      printf '    %s\n' "$antwort"
      code=1
    fi
  done
  return "$code"
}

schritt "Rollout je Workload" \
        "kubectl -n $namespace describe deployments und kubectl -n $namespace get pods" \
        rollout_pruefen
schritt "helm test auf dem Server" \
        "kubectl -n $namespace logs job/test-api bzw. job/test-loesungen" \
        helm_test
schritt "Pod erreicht Pod auf anderem Node" \
        "kubectl -n $namespace get pods -o wide und kubectl describe node <node>" \
        pod_zu_pod
schritt "Prometheus liefert die eigene Metrik" \
        "kubectl -n monitoring get servicemonitor keda-operator und kubectl -n keda get pods" \
        prometheus_pruefen
schritt "ScaledObject meldet einen Wert" \
        "kubectl -n $namespace describe scaledobjects und kubectl -n keda logs deployment/keda-operator" \
        scaledobject_pruefen

printf '\n==== Zusammenfassung\n'
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

printf '\n'
if [ "$fehler" -eq 0 ]; then
  echo "Alle Prüfungen bestanden, das Deployment ist abgenommen."
  exit 0
fi
echo "$fehler von ${#namen[@]} Prüfungen fehlgeschlagen."
exit 1
