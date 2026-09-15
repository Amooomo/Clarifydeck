#!/usr/bin/env bash
# Run the ClarifyDeck Phase 1B external overlay PoC.
# Usage: ./run.sh [:0] [extra args]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
D="${1:-:0}"
shift || true

if [ -x "$DIR/overlay_poc" ]; then
  exec env DISPLAY="$D" "$DIR/overlay_poc" --display "$D" "$@"
else
  exec env DISPLAY="$D" python3 "$DIR/overlay_poc.py" --display "$D" "$@"
fi
