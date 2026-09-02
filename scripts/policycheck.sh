#!/usr/bin/env bash
# Prüft die NetworkPolicies im Namespace judge (#62). Je Pod eine Verbindung,
# die gehen muss, und eine, die nicht gehen darf.
#
# Kurzlebige Pods (Seed, Durchlauf, Lastgenerator, Helm-Tests) werden über
# Wegwerf-Pods mit demselben Label geprüft. Das sleep davor überbrückt das
# Fenster nach dem Pod-Start, in dem kube-router die Adresse noch nicht in
# den Regeln stehen hat.
#
# Grenze der Methode: das Skript misst den Rückgabewert von kubectl exec, und
# ein fehlendes Werkzeug im Image sieht damit aus wie eine Sperre. Deshalb
# geht jede Prüfung über ein Werkzeug, das im jeweiligen Image nachweislich
# liegt -- python3 in den Judge- und MongoDB-Images, bash im Keycloak-Image,
# wget im Traefik-Image, nc in busybox. Ein neues Image braucht hier einen
# eigenen Aufruf.
#
# Der MongoDB-Operator fehlt: sein Image bringt weder Shell noch Python mit.
# Seine Regeln zeigen sich stattdessen an einem Neustart des Pods, danach
# muss der Reconcile im Log ohne Timeout durchlaufen.

set -u
NS=${1:-judge}
DNS=$(kubectl get svc kube-dns -n kube-system -o jsonpath='{.spec.clusterIP}')
# example.com, feste Adresse statt Name: den Namen aufzulösen scheiterte ohne
# DNS-Regel, und das Ergebnis wäre nicht mehr der Internetsperre zuzuordnen.
# Ändert sich die Adresse, misst der Test ins Leere und meldet weiter "zu".
NETZ=2606:2800:21f:cb07:6820:80da:af6b:8b2c

fehler=0

werte() {
  local text=$1 soll=$2 rc=$3
  local ist=offen
  [ "$rc" != 0 ] && ist=zu
  local urteil=FEHLER
  [ "$ist" = "$soll" ] && urteil=ok
  [ "$urteil" = FEHLER ] && fehler=$((fehler + 1))
  printf '%-6s %-42s soll=%-5s ist=%s\n' "$urteil" "$text" "$soll" "$ist"
}

# Verbindung aus einem laufenden Pod über python3.
aus_pod() {
  local text=$1 pod=$2 container=$3 ziel=$4 port=$5 soll=$6
  kubectl exec -n "$NS" "$pod" ${container:+-c "$container"} -- python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.settimeout(5)
sys.exit(s.connect_ex(('$ziel', $port)))
" >/dev/null 2>&1
  werte "$text" "$soll" $?
}

# Verbindung aus dem Keycloak-Pod. Kein python3 im Image, deshalb /dev/tcp.
aus_keycloak() {
  local text=$1 ziel=$2 port=$3 soll=$4
  kubectl exec -n "$NS" keycloak-keycloakx-0 -- \
    timeout 5 bash -c "</dev/tcp/$ziel/$port" >/dev/null 2>&1
  werte "$text" "$soll" $?
}

# Verbindung aus dem Traefik-Pod in kube-system. Kein python3, aber wget.
aus_traefik() {
  local text=$1 url=$2 soll=$3
  kubectl exec -n kube-system deploy/traefik -- \
    wget -q -O /dev/null --timeout=5 "$url" >/dev/null 2>&1
  werte "$text" "$soll" $?
}

# Verbindung aus einem Wegwerf-Pod mit gesetzten Labels.
aus_wegwerf() {
  local text=$1 namespace=$2 labels=$3 ziel=$4 port=$5 soll=$6
  local name="policycheck-$RANDOM"
  kubectl run "$name" -n "$namespace" \
    --labels "${labels:+$labels,}run=$name" \
    --image=busybox --restart=Never --rm --attach --quiet \
    -- sh -c "sleep 8; nc -z -w 5 $ziel $port" >/dev/null 2>&1
  werte "$text" "$soll" $?
}

echo "== Backend =="
aus_pod "backend -> valkey 6379"        deploy/backend "" valkey."$NS".svc.cluster.local 6379 offen
aus_pod "backend -> mongodb 27017"      deploy/backend "" mongodb-svc."$NS".svc.cluster.local 27017 offen
aus_pod "backend -> mongodb 27018"      deploy/backend "" mongodb-svc."$NS".svc.cluster.local 27018 zu
aus_pod "backend -> keycloak 80"        deploy/backend "" keycloak-keycloakx-http."$NS".svc.cluster.local 80 zu
aus_pod "backend -> Internet 80"        deploy/backend "" "$NETZ" 80 zu
aus_pod "backend -> DNS 53"             deploy/backend "" "$DNS" 53 offen

echo
echo "== MongoDB =="
aus_pod "mongodb-0 -> mongodb-1 27017"  mongodb-0 mongod mongodb-1.mongodb-svc."$NS".svc.cluster.local 27017 offen
aus_pod "mongodb-0 -> backend 8000"     mongodb-0 mongod backend."$NS".svc.cluster.local 8000 zu
aus_pod "mongodb-0 -> valkey 6379"      mongodb-0 mongod valkey."$NS".svc.cluster.local 6379 zu
aus_pod "mongodb-0 -> Internet 80"      mongodb-0 mongod "$NETZ" 80 zu

echo
echo "== Worker =="
WORKER=$(kubectl get pods -n "$NS" -l komponente=judge-worker -o name 2>/dev/null | head -1 | cut -d/ -f2)
if [ -n "$WORKER" ]; then
  aus_pod "worker -> valkey 6379"       "$WORKER" worker valkey."$NS".svc.cluster.local 6379 offen
  aus_pod "worker -> mongodb 27017"     "$WORKER" worker mongodb-svc."$NS".svc.cluster.local 27017 offen
  aus_pod "worker -> backend 8000"      "$WORKER" worker backend."$NS".svc.cluster.local 8000 zu
  aus_pod "worker -> Internet 80"       "$WORKER" worker "$NETZ" 80 zu
else
  echo "       kein Worker vorhanden, uebersprungen"
  echo "       Lauf erzeugen: kubectl create job --from=cronjob/lastgenerator lg -n $NS"
fi

echo
echo "== Keycloak =="
aus_keycloak "keycloak -> DNS 53"        "$DNS" 53 offen
aus_keycloak "keycloak -> backend 8000"  backend."$NS".svc.cluster.local 8000 zu
aus_keycloak "keycloak -> mongodb 27017" mongodb-svc."$NS".svc.cluster.local 27017 zu

echo
echo "== Traefik aus kube-system =="
aus_traefik "traefik -> backend 8000"  "http://backend.$NS.svc.cluster.local:8000/healthz" offen
aus_traefik "traefik -> keycloak 80"   "http://keycloak-keycloakx-http.$NS.svc.cluster.local:80/" offen
aus_traefik "traefik -> valkey 6379"   "http://valkey.$NS.svc.cluster.local:6379/" zu

echo
echo "== Kurzlebige Jobs, ueber Wegwerf-Pods =="
aus_wegwerf "seed -> mongodb 27017"     "$NS" "app=aufgaben-seed" mongodb-svc 27017 offen
aus_wegwerf "seed -> valkey 6379"       "$NS" "app=aufgaben-seed" valkey 6379 zu
aus_wegwerf "durchlauf -> valkey 6379"  "$NS" "app=durchlauf" valkey 6379 offen
aus_wegwerf "durchlauf -> backend 8000" "$NS" "app=durchlauf" backend 8000 zu
aus_wegwerf "lastgen -> backend 8000"   "$NS" "app=lastgenerator" backend 8000 offen
aus_wegwerf "lastgen -> mongodb 27017"  "$NS" "app=lastgenerator" mongodb-svc 27017 zu
aus_wegwerf "helm-test -> backend 8000" "$NS" "batch.kubernetes.io/job-name=test-api" backend 8000 offen
aus_wegwerf "helm-test -> valkey 6379"  "$NS" "batch.kubernetes.io/job-name=test-api" valkey 6379 zu

echo
echo "== Pod ohne Label, default-deny =="
aus_wegwerf "ohne Label -> DNS 53"      "$NS" "" "$DNS" 53 zu
aus_wegwerf "ohne Label -> valkey 6379" "$NS" "" valkey 6379 zu

echo
echo "== Aus dem Namespace default =="
aus_wegwerf "default -> backend 8000"  default "" backend."$NS".svc.cluster.local 8000 zu
aus_wegwerf "default -> mongodb 27017" default "" mongodb-svc."$NS".svc.cluster.local 27017 zu
aus_wegwerf "default -> valkey 6379"   default "" valkey."$NS".svc.cluster.local 6379 zu

echo
if [ "$fehler" -eq 0 ]; then
  echo "Alle Pruefungen wie erwartet."
else
  echo "$fehler Pruefung(en) abweichend."
fi
exit "$fehler"