#!/usr/bin/env bash
# Prüft Terraform und Ansible. Läuft lokal und in der CI identisch, damit ein
# grüner lokaler Lauf nicht in einer roten Pipeline endet.
#
# Fehlende Verzeichnisse sind kein Fehler, damit der Check schon in der CI
# hängen kann, bevor es Infrastruktur gibt.
set -uo pipefail

cd "$(dirname "$0")/.."
fail=0

# Das bin/ der Projektumgebung wird ausdrücklich vorangestellt. Der Aufruf über
# ./scripts/infra-check.sh erbt die PATH der Shell, in der er startet: ist dort
# keine Umgebung aktiv, würde eine systemweit installierte Version greifen und
# andere Regeln melden als die CI.
if [ -n "${VIRTUAL_ENV:-}" ] && [ -d "$VIRTUAL_ENV/bin" ]; then
  PATH="$VIRTUAL_ENV/bin:$PATH"
elif [ -d .venv/bin ]; then
  PATH="$PWD/.venv/bin:$PATH"
else
  echo "Hinweis: keine Projektumgebung aktiv." >&2
  echo "         python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
fi

# Die erwartete Python-Version wird aus den Workflows gelesen statt hier
# hinterlegt, damit sie nicht an zwei Stellen gepflegt werden muss. Nötig ist
# der Vergleich, weil venv keine Version erzwingt: es übernimmt das python3,
# das beim Anlegen der Umgebung in der PATH stand.
check_python() {
  local versions want actual
  versions=$(grep -rhoE 'python-version:[[:space:]]*"?[0-9]+\.[0-9]+' .github/workflows/ 2>/dev/null \
             | grep -oE '[0-9]+\.[0-9]+$' | sort -u)
  [ -n "$versions" ] || return 0
  if [ "$(printf '%s\n' "$versions" | wc -l)" -gt 1 ]; then
    echo "WARNUNG: die Workflows nennen mehr als eine Python-Version." >&2
    return 0
  fi
  want="$versions"
  actual=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
  [ -n "$actual" ] || return 0
  if [ "$actual" != "$want" ]; then
    echo "WARNUNG: Python $actual, die Workflows nutzen $want." >&2
    echo "         Abhilfe: rm -rf .venv && python$want -m venv .venv" >&2
    echo "                  && .venv/bin/pip install -r requirements.txt" >&2
  fi
}

# Weicht eine aufgelöste Version vom Pin ab, meldet die CI später Regeln, die
# in der Entwicklungsumgebung gar nicht auftauchen.
check_pin() {
  local tool="$1" pinned actual
  pinned=$(grep -oE "^$tool==[0-9.]+" requirements.txt 2>/dev/null | cut -d= -f3)
  [ -n "$pinned" ] || return 0
  command -v "$tool" >/dev/null || return 0
  actual=$("$tool" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [ -n "$actual" ] && [ "$actual" != "$pinned" ]; then
    echo "WARNUNG: $tool $actual, gepinnt ist $pinned." >&2
    echo "         Abhilfe: pip install -r requirements.txt" >&2
  fi
}

step() { printf '\n=== %s\n' "$1"; }
result() {
  if [ "$1" -eq 0 ]; then echo "    ok"; else echo "    FEHLGESCHLAGEN"; fail=1; fi
}

check_python

# ---------- Terraform ----------
tf_dirs=$(find . -name '*.tf' -not -path './.terraform/*' -not -path './.git/*' \
          -exec dirname {} \; | sort -u)

if [ -z "$tf_dirs" ]; then
  step "Terraform: keine .tf-Dateien, übersprungen"
elif ! command -v terraform >/dev/null; then
  echo "terraform nicht gefunden" >&2
  # Verwiesen wird auf die Seite statt auf ein Kommando: der Weg unterscheidet
  # sich je nach Betriebssystem, unter Windows gibt es nur den Binär-Download.
  # Die Version steht in den Workflows und nicht hier, damit sie an einer
  # Stelle gepflegt wird.
  echo "         Abhilfe: https://developer.hashicorp.com/terraform/install" >&2
  echo "         Version: siehe terraform_version in .github/workflows/infra.yml" >&2
  fail=1
else
  step "terraform fmt -check -recursive"
  terraform fmt -check -recursive
  result $?

  for dir in $tf_dirs; do
    step "terraform validate ($dir)"
    # -backend=false: kein State nötig, geprüft wird nur die Konfiguration.
    # Provider werden trotzdem geladen, sonst kennt validate die Ressourcentypen nicht.
    (cd "$dir" && terraform init -backend=false -input=false -no-color >/dev/null \
      && terraform validate -no-color)
    result $?
  done
fi

# ---------- Ansible ----------
if [ ! -d ansible ]; then
  step "Ansible: kein ansible/, übersprungen"
else
  # Lint und Syntax-Check lösen die Rolle aus deploy.yaml auf, ohne
  # installierte Rolle scheitern beide. In der CI fehlt sie immer, lokal wird
  # sie bei jedem Lauf neu geholt, siehe --force unten.
  if [ -f ansible/requirements.yml ] && command -v ansible-galaxy >/dev/null; then
    step "ansible-galaxy install (Rolle und Collections)"
    # --force, weil galaxy eine installierte Rolle sonst behält, auch wenn in
    # requirements.yml eine andere Version steht. Ohne das Flag prüfen lint und
    # syntax-check gegen den alten Stand und melden trotzdem Erfolg.
    #
    # Drei Versuche, weil Galaxy zeitweise mit "The read operation timed out"
    # abbricht, in 40 CI-Läufen einmal. Gemessen in der CI dauert der Schritt
    # 9 Sekunden, der Abbruch kam nach 67 Sekunden. Drei Versuche bleiben
    # damit weit unter dem Zeitlimit des Jobs von 15 Minuten. Eine Obergrenze
    # je Versuch gibt es nicht, ein hängender Abruf blockiert auch ohne
    # Wiederholung bis zum Zeitlimit.
    for versuch in 1 2 3; do
      ansible-galaxy install -r ansible/requirements.yml --force >/dev/null
      rc=$?
      [ "$rc" -eq 0 ] && break
      [ "$versuch" -lt 3 ] && echo "    Versuch $versuch fehlgeschlagen, erneut" >&2
    done
    result "$rc"
  fi

  if command -v ansible-lint >/dev/null; then
    check_pin ansible-lint
    step "ansible-lint (Profil basic)"
    # Profil festgelegt: die schärferen Profile bewerten vor allem
    # Namenskonventionen und erzeugen dadurch Änderungsdruck ohne fachlichen Gewinn.
    ansible-lint --profile=basic ansible/
    result $?
  else
    echo "ansible-lint nicht gefunden, übersprungen" >&2
  fi

  playbooks=$(find ansible -maxdepth 2 -name '*.y*ml' \
              -not -path '*/roles/*' -not -path '*/group_vars/*' \
              -not -path '*/host_vars/*' -not -path '*/tasks/*' 2>/dev/null)
  for pb in $playbooks; do
    # Nur echte Playbooks prüfen; Variablendateien haben weder hosts noch
    # import_playbook. tasks/ ist ausgenommen: das sind Includes, keine
    # Playbooks, und --syntax-check scheitert an ihnen mangels Play. Ein hosts:
    # in einer Ingress-TLS-Sektion würde die Heuristik unten sonst täuschen.
    # Geprüft werden sie ohnehin über das Playbook, das sie per import_tasks
    # einbindet (deploy.yaml).
    grep -qE '^\s*-?\s*(hosts|import_playbook):' "$pb" || continue
    step "ansible-playbook --syntax-check ($pb)"
    # Geprüft wird ohne Inventory: das schreibt Terraform, es liegt nicht im
    # Repo und fehlt in der CI immer. Die beiden Warnungen daraus melden nur,
    # dass hosts: all niemanden trifft. Playbook, Imports und Rolle löst der
    # Check trotzdem auf, ein Fehler darin fällt weiterhin auf.
    ANSIBLE_LOCALHOST_WARNING=False ANSIBLE_INVENTORY_UNPARSED_WARNING=False \
      ansible-playbook --syntax-check "$pb"
    result $?
  done
fi

printf '\n'
if [ "$fail" -ne 0 ]; then
  echo "Infra-Check fehlgeschlagen."
else
  echo "Infra-Check ok."
fi
exit "$fail"
