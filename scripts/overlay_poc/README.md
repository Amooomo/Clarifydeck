# ClarifyDeck Phase 1B — Gamescope External Overlay PoC

Goal: prove that an overlay independent of the Decky/Steam QAM React tree can stay
on screen over a running game **after the QAM is closed**.

It draws a single `OCR TEST` box at the bottom center on a fullscreen transparent
1280x800 ARGB window tagged as a Gamescope external overlay.

This is a PoC only. It does **not** touch OCR, screenshots, the QAM UI, or any
user config.

## Mechanism

From current gamescope source (`src/main.cpp`, `UpdateCompatEnvVars()`):

```
// We no longer need to set GAMESCOPE_EXTERNAL_OVERLAY from steam, mangoapp now does it itself
setenv("STEAM_DISABLE_MANGOAPP_ATOM_WORKAROUND", "1", 0);
```

So the supported classification is the X11 window property
`GAMESCOPE_EXTERNAL_OVERLAY` (CARDINAL, 32-bit). It must be set **before** the
window is mapped. `GAMESCOPE_NO_FOCUS=1` is set as an extra hint.

- `GAMESCOPE_EXTERNAL_OVERLAY = 1` (not 0)
- `GAMESCOPE_NO_FOCUS = 1`
- `WM_HINTS.input = False`
- optional empty `XFixes` `ShapeInput` region (click-through)

Do **not** set `STEAM_GAME` or `STEAM_OVERLAY`. The desired stacking is:

```
Game
  -> ClarifyDeck External Overlay
     -> Steam Overlay / QAM
```

## Files

- `overlay_poc.py` — Python + ctypes + libX11 fallback (no compiler needed).
- `overlay_poc.c` — minimal C/X11 implementation (preferred if a compiler exists).
- `build.sh` — builds the C binary; prints the Python fallback if no compiler.
- `run.sh` — runs the C binary if built, else the Python fallback.

## Run

Game Mode, a game running, QAM closed:

```bash
cd ~/Downloads/Clarifydeck
bash scripts/overlay_poc/build.sh      # optional, only if a compiler exists
bash scripts/overlay_poc/run.sh :0
# or directly:
python3 scripts/overlay_poc/overlay_poc.py --display :0
```

Auto-exit after 20s for a quick check:

```bash
python3 scripts/overlay_poc/overlay_poc.py --display :0 --duration 20
```

## Verify

The program prints its window id. In another shell:

```bash
DISPLAY=:0 xprop -id <WINDOW_ID>
```

Must show at least:

```
GAMESCOPE_EXTERNAL_OVERLAY(CARDINAL) = 1
GAMESCOPE_NO_FOCUS(CARDINAL) = 1
```

If `GAMESCOPE_EXTERNAL_OVERLAY` is not `1`, the PoC is a failure.

Also capture focus state before/after:

```bash
DISPLAY=:0 xprop -root | grep -E 'GAMESCOPE_FOCUSED|FOCUSABLE' > /tmp/root_before.txt
# ... run PoC ...
DISPLAY=:0 xprop -root | grep -E 'GAMESCOPE_FOCUSED|FOCUSABLE' > /tmp/root_after.txt
diff /tmp/root_before.txt /tmp/root_after.txt
```

## Test matrix

| Test | Action | Expected |
|------|--------|----------|
| A | Game running, QAM closed, start PoC | `OCR TEST` visible at bottom |
| B | Open QAM | QAM appears **above** `OCR TEST` |
| C | Close QAM | `OCR TEST` still visible |
| D | Touch / sticks / buttons | Game receives input, no focus steal |
| E | Touch the `OCR TEST` area | Input passes through to game/UI |
| F | `kill` the PoC | `OCR TEST` disappears, game unaffected |
| G | start/kill/start/kill | No leftover window, no zombie, no focus corruption |

Test C is the key result of Phase 1B.

## Troubleshooting

- `cannot open display`: try `--display :1` (game XWayland) — but the overlay
  should live on the Steam/mangoapp XWayland (currently `:0`). Do not hardcode
  `:0` in the final product; discover it (see `phase1_probe.sh`).
- Nothing visible but `xprop` shows the atom: try `--override-redirect`.
- No transparency: the screen lacks a 32-bit TrueColor visual (unexpected on
  SteamOS/Xwayland).
- The Deck has no compiler: use the Python fallback; it needs only Python 3 and
  libX11 (both present on SteamOS).
