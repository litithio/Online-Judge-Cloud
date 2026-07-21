#!/usr/bin/env bash
# Prüft Terraform und Ansible. Läuft lokal und in der CI identisch, damit ein
# grüner lokaler Lauf nicht in einer roten Pipeline endet.
#
# Fehlende Verzeichnisse sind kein Fehler, damit der Check schon in der CI
# hängen kann, bevor es Infrastruktur gibt.
set -uo pipefail

cd "$(dirname "$0")/.."
fail=0

# Das bin/ der Projektumgebung wird ausdrücklich vorangestellt. `conda activate`
# setzt zwar CONDA_PREFIX, garantiert aber nicht, dass sein bin/ in der PATH vor
# den Systempfaden steht — eine systemweit installierte Version würde sonst die
# der Projektumgebung verdecken und andere Regeln melden als die CI.
if [ -n "${CONDA_PREFIX:-}" ] && [ -d "$CONDA_PREFIX/bin" ]; then
  PATH="$CONDA_PREFIX/bin:$PATH"
elif [ -d .venv/bin ]; then
  PATH="$PWD/.venv/bin:$PATH"
else
  echo "Hinweis: keine Projektumgebung aktiv." >&2
  echo "         conda env create -f environment.yml && conda activate online-judge" >&2
fi

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
    echo "         Abhilfe: conda env update -f environment.yml --prune" >&2
  fi
}

step() { printf '\n=== %s\n' "$1"; }
result() {
  if [ "$1" -eq 0 ]; then echo "    ok"; else echo "    FEHLGESCHLAGEN"; fail=1; fi
}

# ---------- Terraform ----------
tf_dirs=$(find . -name '*.tf' -not -path './.terraform/*' -not -path './.git/*' \
          -exec dirname {} \; | sort -u)

if [ -z "$tf_dirs" ]; then
  step "Terraform: keine .tf-Dateien, übersprungen"
elif ! command -v terraform >/dev/null; then
  echo "terraform nicht gefunden" >&2; fail=1
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
              -not -path '*/host_vars/*' 2>/dev/null)
  for pb in $playbooks; do
    # Nur echte Playbooks prüfen; Variablendateien haben weder hosts noch import_playbook.
    grep -qE '^\s*-?\s*(hosts|import_playbook):' "$pb" || continue
    step "ansible-playbook --syntax-check ($pb)"
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
