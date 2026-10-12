#!/bin/bash
# Stemmy Woordenboek generator
# Gebruik: ./scripts/run_dictionary.sh [LETTER] [LIMIT] [CLIPS]
#
# Voorbeelden:
#   ./scripts/run_dictionary.sh P          # letter P, 60 woorden, 3 clips
#   ./scripts/run_dictionary.sh G 80 4     # letter G, 80 woorden, 4 clips
#   ./scripts/run_dictionary.sh A 40 2     # letter A, 40 woorden, 2 clips

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."
LETTER="${1:-P}"
LIMIT="${2:-60}"
CLIPS="${3:-3}"

export PATH="/opt/homebrew/bin:$PATH"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

"$ROOT/venv/bin/python3" "$SCRIPT_DIR/dictionary.py" \
    --letter "$LETTER" \
    --limit  "$LIMIT" \
    --clips  "$CLIPS" \
    --db     "$ROOT/data/stemmy-replica.db" \
    --out    "$ROOT/output/dictionary"

echo ""
echo "✓ Klaar. Start nu de server:"
echo "   python3 dict/server.py --letter ${LETTER^^}"
echo "   → http://localhost:7842/woordenboek_${LETTER^^}.html"
