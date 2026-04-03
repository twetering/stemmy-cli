#!/usr/bin/env bash
# Voer een commando uit met project-venv (PATH + site-packages), zonder handmatig te activeren.
#
# Gebruik:
#   ./scripts/with_venv.sh python echo/server.py --port 7843
#   ./scripts/with_venv.sh stemmy workflows --help
#
# Ondersteunt zowel .venv als venv in de repo-root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

VENV=""
for d in .venv venv; do
  if [[ -f "$ROOT/$d/bin/activate" ]]; then
    VENV="$ROOT/$d"
    break
  fi
done

if [[ -z "$VENV" ]]; then
  echo "Geen Python-venv in $ROOT (zoek naar .venv of venv)." >&2
  echo "Maak er een en installeer deps:" >&2
  echo "  cd $ROOT && python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$VENV/bin/activate"

if [[ $# -eq 0 ]]; then
  echo "Gebruik: $0 <commando> [args...]" >&2
  echo "Voorbeeld: $0 python echo/server.py" >&2
  exit 1
fi

exec "$@"
