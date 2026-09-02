#!/usr/bin/env bash
# Bringt den ganzen Stack mit einem Aufruf hoch, von den VMs bis zur
# Anwendung. Der Lauf hält einmal an. Terraform braucht das VPN, SSH und
# Ansible brauchen es aus, und umschalten kann es nur der Mensch davor.
# Gewartet wird deshalb nicht auf einen VPN-Zustand, sondern auf die
# Gegenstelle, die der nächste Abschnitt braucht.
set -euo pipefail

cd "$(dirname "$0")/.."

# Das bin/ der Projektumgebung wird wie in infra-check.sh vorangestellt,
# sonst greift ein systemweit installiertes Ansible mit anderem Stand.
if [ -n "${VIRTUAL_ENV:-}" ] && [ -d "$VIRTUAL_ENV/bin" ]; then
  PATH="$VIRTUAL_ENV/bin:$PATH"
elif [ -d .venv/bin ]; then
  PATH="$PWD/.venv/bin:$PATH"
fi

# ---------- Voraussetzungen ----------
# Alles Fehlende wird gesammelt gemeldet. Ein Lauf, der erst nach dem
# Apply an einer fehlenden Datei scheitert, lässt halbe Infrastruktur
# und ein eingeschaltetes VPN zurück.
fehlt=0

brauche_datei() {
  if [ ! -f "$1" ]; then
    echo "FEHLT  $1" >&2
    echo "       Vorlage $1.example, siehe README Abschnitt Betrieb." >&2
    fehlt=1
  fi
}

brauche_werkzeug() {
  if ! command -v "$1" >/dev/null; then
    echo "FEHLT  $1 ist nicht in der PATH." >&2
    fehlt=1
  fi
}

brauche_datei terraform/terraform.tfvars
brauche_datei ansible/dns-credentials.yaml
brauche_datei ansible/auth-credentials.yaml
brauche_datei ansible/files/mongodb-password.yaml
brauche_datei ansible/files/valkey-password.yaml
brauche_werkzeug terraform
brauche_werkzeug ansible-galaxy
brauche_werkzeug ansible-playbook
brauche_werkzeug kubectl
brauche_werkzeug python3

[ "$fehlt" -eq 0 ] || exit 1

# ---------- Erreichbarkeit ----------
# Die Adresse der OpenStack-API steht genau einmal im Repo, in
# terraform/providers.tf. Sie wird von dort gelesen statt hier wiederholt.
os_url=$(grep -oE 'https://[^"]+' terraform/providers.tf | head -1)
os_host=${os_url#https://}
os_port=${os_host##*:}
os_host=${os_host%%:*}

erreichbar() {
  # TCP-Connect statt ping, die Nodes beantworten kein ICMP Echo. python3
  # statt nc, weil die netcat-Varianten sich bei IPv6-Literalen
  # unterscheiden und python3 ohnehin Voraussetzung ist.
  python3 - "$1" "$2" <<'PY' >/dev/null 2>&1
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
except OSError:
    sys.exit(1)
PY
}

# sleep 10 wie in ansible/tasks/wait.yaml. Dichtere Versuche hat ein IDS
# schon als Portscan gewertet und Port 22 zeitweise gesperrt.
warte_auf_openstack() {
  until erreichbar "$os_host" "$os_port"; do
    echo "Warte auf die OpenStack-API $os_host. Schalte das VPN ein."
    sleep 10
  done
}

# ---------- Terraform, VPN an ----------
warte_auf_openstack
terraform -chdir=terraform init -input=false
# apply bleibt interaktiv. Es kann bestehende Nodes zerstören, die
# Rückfrage mit dem Plan davor ist gewollt.
terraform -chdir=terraform apply

master=$(terraform -chdir=terraform output -raw master_ip)
if [ -z "$master" ]; then
  echo "terraform output master_ip ist leer, kein Server im State." >&2
  exit 1
fi

# ---------- Halt an der VPN-Grenze ----------
echo
echo "Die VMs stehen. Schalte das VPN jetzt aus."
letzte=""
sekunden=0
until erreichbar "$master" 22; do
  # Solange die OpenStack-API noch antwortet, ist der Tunnel die
  # wahrscheinliche Ursache. Danach bootet die Node vermutlich noch.
  if erreichbar "$os_host" "$os_port"; then
    meldung="Warte auf $master Port 22. Die OpenStack-API antwortet noch, das VPN ist wohl an."
  else
    meldung="Warte auf $master Port 22. Die Node bootet vermutlich noch."
  fi
  # Kein Timeout an dieser Stelle, gewartet wird auf den Handgriff eines
  # Menschen. Die Boot-Bereitschaft prüft danach ansible/tasks/wait.yaml
  # mit eigenem Timeout. Damit der Lauf sichtbar lebt, wiederholt sich die
  # Meldung jede Minute.
  if [ "$meldung" != "$letzte" ] || [ "$((sekunden % 60))" -eq 0 ]; then
    echo "$meldung"
    letzte=$meldung
  fi
  sleep 10
  sekunden=$((sekunden + 10))
done

# ---------- Ansible und Anwendung, VPN aus ----------
# --force, weil ansible-galaxy eine bereits installierte Rolle sonst stehen
# lässt, auch wenn requirements.yml eine andere Version pinnt.
(cd ansible \
  && ansible-galaxy install -r requirements.yml --force \
  && ansible-playbook -i inventory/generated-inventory.yml \
                      -i dns-credentials.yaml deploy.yaml)

# Der Abschlussbeleg kommt aus dem Cluster, nicht aus dem Playbook-Ende.
KUBECONFIG="$PWD/ansible/kubeconfig-generated.yaml" kubectl get nodes
