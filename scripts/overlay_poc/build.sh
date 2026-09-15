#!/usr/bin/env bash
# Build the ClarifyDeck Phase 1B Gamescope external overlay PoC.
# If no C compiler is present, prints the Python fallback instead of failing.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CC_BIN=""
for c in cc gcc clang; do
  if command -v "$c" >/dev/null 2>&1; then
    CC_BIN="$c"
    break
  fi
done

if [ -z "$CC_BIN" ]; then
  echo "No C compiler found (cc/gcc/clang)."
  echo "Use the Python ctypes fallback instead:"
  echo "  python3 \"$DIR/overlay_poc.py\" --display :0"
  exit 0
fi

CFLAGS=""
LIBS="-lX11"
if command -v pkg-config >/dev/null 2>&1; then
  CFLAGS="$(pkg-config --cflags x11 2>/dev/null || true)"
  LIBS="$(pkg-config --libs x11 2>/dev/null || echo -lX11)"
fi

DEFS=""
if [ -f /usr/include/X11/extensions/Xfixes.h ] \
   || { command -v pkg-config >/dev/null 2>&1 && pkg-config --exists xfixes 2>/dev/null; }; then
  DEFS="-DHAVE_XFIXES"
  if command -v pkg-config >/dev/null 2>&1; then
    DEFS="$DEFS $(pkg-config --cflags xfixes 2>/dev/null || true)"
    LIBS="$LIBS $(pkg-config --libs xfixes 2>/dev/null || echo -lXfixes)"
  else
    LIBS="$LIBS -lXfixes"
  fi
fi

# shellcheck disable=SC2086
"$CC_BIN" -O2 -Wall -o "$DIR/overlay_poc" "$DIR/overlay_poc.c" $CFLAGS $DEFS $LIBS
echo "built $DIR/overlay_poc"
