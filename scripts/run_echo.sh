#!/usr/bin/env bash
# Grafstemming / echo-server met automatisch geactiveerde project-venv.
#
# Gebruik:
#   ./scripts/run_echo.sh
#   ./scripts/run_echo.sh --port 7843 --browser

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/with_venv.sh" python echo/server.py "$@"
