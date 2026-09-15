# ClarifyDeck

ClarifyDeck is a Decky Loader plugin for Steam Deck. It lets the user define OCR regions for small in-game text, runs backend OCR against changed regions, and renders readable high-contrast overlay captions on top of the game.

## Current architecture

- `main.py` is the backend SSOT for region state: `dict[box_id, BoxState]`.
- `src/index.tsx` is the React/TypeScript Decky frontend.
- The frontend never persists region coordinates locally; it calls backend RPC methods and listens for backend events.
- The overlay is rendered with `pointer-events: none` and a very high `z-index`.

## Backend RPC/events

RPC methods exposed by `main.py`:

- `list_boxes()`
- `add_box()`
- `update_box(box_id, x, y, w, h)`
- `remove_box(box_id)`
- `get_status()`

Backend events:

- `boxes_changed`: emitted after box add/update/remove.
- `ocr_broadcast`: emitted after OCR text is recognized for a box.

## Project-local conda environment

All toolchain/dependency installation should stay under this project directory. The provided `environment.yml` uses USTC Anaconda mirror channels and disables default channels with `nodefaults`. Frontend packages are installed by the conda-provided `pnpm`, with `.npmrc` forcing the USTC npm registry and the project-local `./.pnpm-store`.

Windows PowerShell:

```powershell
.\scripts\setup-conda.ps1
conda activate .\.conda
pnpm build
```

Linux/Steam Deck:

```bash
bash scripts/setup-conda.sh
conda activate ./.conda
pnpm build
```

The scripts create/update `./.conda`, force conda package downloads/cache into `./.conda-pkgs`, and install frontend dependencies into the project-local pnpm store `./.pnpm-store`. Do not use a global Node.js/pnpm toolchain for project dependency installation.


## Local verification

The backend can be smoke-tested without a running Decky loader:

```bash
python scripts/smoke_backend.py
```

After dependencies are installed through the project-local conda environment, verify the frontend bundle with:

```bash
pnpm build
```

## Screenshot spike (step 1)

Before wiring capture into the plugin, validate that the Steam Deck can actually
grab the gamescope frame. `scripts/capture_spike.py` probes every plausible
backend (`grim`, `gst-launch-1.0 pipewiresrc`, the `xdg-desktop-portal`
Screenshot method, and X11 fallbacks), writes each artifact to a known
directory, and reports what worked.

**Run it in game mode** (a `gamescope-*` socket must be present). A capture in
desktop mode only proves the desktop compositor works, not the game frame. The
script auto-`chmod +x`es the bundled `bin/grim` / `bin/tesseract`, only treats
real compositor sockets as displays, and prints the detected session.

On the Steam Deck:

```bash
python3 scripts/capture_spike.py
python3 scripts/capture_spike.py --list
python3 scripts/capture_spike.py --display gamescope-0
```

Output defaults to `~/Clarifydeck-spike` (`/home/deck/Clarifydeck-spike`). The
script writes:

- `report.txt` - full PASS/FAIL report with the exact commands and stderr.
- `env.txt` - detected user/session/tools and enumerated PipeWire nodes.
- `grim_*.png|ppm`, `gst_*.png|ppm`, `portal_screenshot.png`, `latest_capture.*`
  - captured frames.

Open `latest_capture.png` first. A backend only "passes" when the output has a
valid PNG/PPM/PAM header and non-zero dimensions; PPM captures also report a
`blank` flag so an all-black frame is obvious. Use `--out <dir>` to change the
destination and `--backend grim|gst|portal|fallback` to isolate one path. Run it
as the plugin does (root) to get a valid result: when root it automatically
re-runs the capture commands via `sudo -u deck`.

If no PipeWire `Video/Source` node is listed, raw `pipewiresrc` cannot work:
SteamOS only exposes the gamescope screen-cast node after an
`xdg-desktop-portal` ScreenCast session exists, which is why the portal backend
is included.

## Screenshot runtime notes

The backend captures with the pipeline validated by the spike:

```
pipewiresrc num-buffers=1 ! videoconvert ! video/x-raw,format=RGB ! pnmenc ! filesink
```

`video/x-raw,format=RGB` is required so `pnmenc` emits a binary PPM (P6) instead
of a PAM (P7) with alpha. Capture only works in game mode, where gamescope
exposes a PipeWire `Video/Source` node; desktop mode fails with `target not
found`. The capture runs as the `deck` user (`sudo -u deck` when the backend is
root) with `XDG_RUNTIME_DIR=/run/user/1000`, uses a unique temp file per frame,
and allows up to `CLARIFYDECK_CAPTURE_TIMEOUT` seconds (default `10`).

## OCR runtime notes

The backend searches for Tesseract in these locations, in order, and will
`chmod +x` a bundled binary whose executable bit was lost during install:

1. plugin-local `bin/tesseract`
2. plugin-local `.conda/bin/tesseract`
3. plugin-local `conda/bin/tesseract`
4. `PATH`

Useful environment variables:

- `CLARIFYDECK_OCR_LANG` (default: `eng`)
- `CLARIFYDECK_MSE_THRESHOLD` (default: `16.0`)
- `CLARIFYDECK_SCREENSHOT_CMD` for overriding screenshot capture. Use `{output}` if the command writes to a file.
- `CLARIFYDECK_XDG_RUNTIME_DIR` (default: `/run/user/1000`)


For languages beyond the Tesseract data bundled with your conda `tesseract` package, place traineddata files in one of the searched `tessdata` directories, for example `/home/deck/Clarifydeck/.conda/share/tessdata`, and set `CLARIFYDECK_OCR_LANG` accordingly.
