#!/usr/bin/env bash
# ClarifyDeck Phase 1 investigation probe.
#
# READ-ONLY. Does not touch product code, user config, or running processes.
# Collects the real Game Mode environment so the overlay mechanism can be
# determined from evidence instead of assumptions.
#
# Usage (run as deck, in Game Mode with a game running):
#   bash scripts/phase1_probe.sh
#
# Output:
#   debug/gamescope_environment.txt
#   debug/gamescope_windows.txt
#   debug/mangoapp_properties.txt
#   debug/summary.txt

set +e
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEBUG="$ROOT/debug"
mkdir -p "$DEBUG"

ENV_FILE="$DEBUG/gamescope_environment.txt"
WIN_FILE="$DEBUG/gamescope_windows.txt"
MANGO_FILE="$DEBUG/mangoapp_properties.txt"
SUM_FILE="$DEBUG/summary.txt"

: > "$ENV_FILE"
: > "$WIN_FILE"
: > "$MANGO_FILE"
: > "$SUM_FILE"

note() { printf '%s\n' "$*" | tee -a "$SUM_FILE"; }
section() { printf '\n===== %s =====\n' "$*"; }

run_env() {
  {
    section "$1"
    shift
    "$@" 2>&1
  } >> "$ENV_FILE"
}

run_env "meta" bash -c 'echo "date: $(date)"; echo "user: $(id)"; echo "kernel: $(uname -a)"'

run_env "gamescope --version" bash -c 'gamescope --version 2>&1 || echo "gamescope binary not on PATH"'

run_env "pgrep gamescope" bash -c 'pgrep -af gamescope || echo "(none)"'
run_env "pgrep steam" bash -c "pgrep -af 'steam|steamwebhelper' || echo '(none)'"
run_env "pgrep mango" bash -c "pgrep -af 'mangoapp|mangohud' || echo '(none)'"

run_env "X11 sockets" bash -c 'ls -la /tmp/.X11-unix/ 2>&1 || echo "(none)"'
run_env "runtime sockets" bash -c "ls -la /run/user/1000/ 2>&1 | grep -E 'wayland|gamescope|pipewire|X11' || echo '(none)'"

run_env "tools" bash -c '
for t in xprop xwininfo xdotool wayland-info gamescopectl mangohudctl wlr-randr; do
  printf "%s: %s\n" "$t" "$(command -v "$t" 2>/dev/null || echo missing)"
done'

run_env "wayland gamescope protocols" bash -c '
if command -v wayland-info >/dev/null 2>&1; then
  for wd in gamescope-0 gamescope-1 wayland-0; do
    if [ -S "/run/user/1000/$wd" ]; then
      echo "--- WAYLAND_DISPLAY=$wd ---"
      XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY="$wd" wayland-info 2>&1 | grep -i -E "gamescope|interface:" | grep -i gamescope
    fi
  done
else
  echo "wayland-info missing"
fi'

# --- per-process environment ---
dump_environ() {
  local label="$1" pid="$2"
  {
    section "$label (pid $pid)"
    echo "cmdline: $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
    local raw
    raw="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null)"
    if [ -z "$raw" ]; then
      raw="$(sudo -n tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null)"
    fi
    printf '%s\n' "$raw" | grep -E 'DISPLAY|WAYLAND_DISPLAY|XDG_RUNTIME_DIR|GAMESCOPE|STEAM|MANGO|MANGOHUD' | sort
  } >> "$ENV_FILE"
}

GS_PID="$(pgrep -o gamescope 2>/dev/null || true)"
STEAM_PID="$(pgrep -o steam 2>/dev/null || true)"
MANGO_PID="$(pgrep -o mangoapp 2>/dev/null || true)"

[ -n "$GS_PID" ] && dump_environ "gamescope" "$GS_PID"
[ -n "$STEAM_PID" ] && dump_environ "steam" "$STEAM_PID"
[ -n "$MANGO_PID" ] && dump_environ "mangoapp" "$MANGO_PID"

note "gamescope pid: ${GS_PID:-none}"
note "steam pid: ${STEAM_PID:-none}"
note "mangoapp pid: ${MANGO_PID:-none}"

# --- enumerate every XWayland display, dump windows ---
declare -a DISPLAYS=()
for SOCKET in /tmp/.X11-unix/X*; do
  [ -e "$SOCKET" ] || continue
  NUM="${SOCKET##*X}"
  D=":$NUM"
  DISPLAYS+=("$D")
  {
    section "DISPLAY=$D"
    echo "--- xprop -root ---"
    DISPLAY="$D" xprop -root 2>&1 | head -150
    echo "--- xwininfo -root -tree ---"
    DISPLAY="$D" xwininfo -root -tree 2>&1 | head -250
  } >> "$WIN_FILE"
done

note "displays found: ${DISPLAYS[*]:-(none)}"

# --- find windows of interest and dump their full properties ---
find_windows() {
  local d="$1"
  DISPLAY="$d" xwininfo -root -tree 2>/dev/null
}

for D in "${DISPLAYS[@]}"; do
  TREE="$(find_windows "$D")"
  # Candidate window ids: lines with 0x... ids
  echo "$TREE" | grep -E '0x[0-9a-f]+' | while read -r line; do
    WID="$(printf '%s' "$line" | grep -oE '0x[0-9a-f]+' | head -1)"
    [ -n "$WID" ] || continue
    PROPS="$(DISPLAY="$D" xprop -id "$WID" 2>/dev/null)"
    if printf '%s' "$PROPS" | grep -qiE 'GAMESCOPE|STEAM_OVERLAY|STEAM_GAME|mango'; then
      {
        section "window $WID on $D"
        DISPLAY="$D" xprop -id "$WID" 2>&1
      } >> "$WIN_FILE"
    fi
  done
done

# --- mangoapp window properties ---
if [ -n "$MANGO_PID" ]; then
  {
    section "mangoapp cmdline"
    tr '\0' ' ' < "/proc/$MANGO_PID/cmdline" 2>/dev/null; echo
    section "mangoapp environ"
    tr '\0' '\n' < "/proc/$MANGO_PID/environ" 2>/dev/null | sort
  } >> "$MANGO_FILE"

  for D in "${DISPLAYS[@]}"; do
    {
      section "mangoapp window on $D"
      DISPLAY="$D" xwininfo -root -tree 2>&1 | grep -i -B2 -A2 mango
      DISPLAY="$D" xprop -root 2>&1 | grep -i -E 'GAMESCOPE|OVERLAY'
    } >> "$MANGO_FILE"
  done
fi

# --- screenshot interface ---
run_env "gamescope screenshot hints" bash -c '
echo "SIGUSR2 triggers TakeScreenshot(true) in current gamescope."
echo "gamescope-control protocol supports take_screenshot with screenshot_type:"
echo "  1 base_plane_only (game only, no overlays)"
echo "  2 all_real_layers"
echo "  3 full_composition"
echo "  4 screen_buffer"
'

echo
echo "Phase 1 probe complete."
echo "Files:"
echo "  $ENV_FILE"
echo "  $WIN_FILE"
echo "  $MANGO_FILE"
echo "  $SUM_FILE"
echo
echo "Please send the contents of the four files back."
