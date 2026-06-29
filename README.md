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

## OCR runtime notes

The backend searches for Tesseract in these locations, in order:

1. `/home/deck/Clarifydeck/bin/tesseract`
2. `/home/deck/Clarifydeck/.conda/bin/tesseract`
3. plugin-local `bin/tesseract`
4. plugin-local `.conda/bin/tesseract`
5. plugin-local `conda/bin/tesseract`
6. `PATH`

Useful environment variables:

- `CLARIFYDECK_OCR_LANG` (default: `eng`)
- `CLARIFYDECK_MSE_THRESHOLD` (default: `16.0`)
- `CLARIFYDECK_SCREENSHOT_CMD` for overriding screenshot capture. Use `{output}` if the command writes to a file.


For languages beyond the Tesseract data bundled with your conda `tesseract` package, place traineddata files in one of the searched `tessdata` directories, for example `/home/deck/Clarifydeck/.conda/share/tessdata`, and set `CLARIFYDECK_OCR_LANG` accordingly.

The default screenshot candidates are `grim -`, then `spectacle`, `gnome-screenshot`, `scrot`, and ImageMagick `import`.
