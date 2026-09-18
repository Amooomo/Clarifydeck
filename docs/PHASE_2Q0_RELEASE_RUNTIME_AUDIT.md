# Phase 2Q.0 — v0.1 Release Audit / Runtime Dependency & Size Review

Audit-only phase. **No runtime files were deleted, no packaging rules were
changed, no production package was generated.**

## Audit metadata

| Field | Value |
|---|---|
| Repository | `D:\Project\Clarifydeck` |
| Branch | `phase-2p1-qam-production-ui` |
| HEAD at audit time | `0e2e8d977e0aee854173d52db3f7f3992be22bb5` (`build: ignore production package output`) |
| Expected HEAD | `be0f721…` (`ui: add per-region panel opacity`) |
| Working tree | clean |
| Discrepancy | One expected follow-up commit exists on top of `be0f721`: `0e2e8d9 build: ignore production package output` (adds `Clarifydeck-prod-out/` to `.gitignore`). Read-only audit continued. |

Method: static source search (Python/TS), `git ls-files`, `readelf -d` dynamic
dependency inspection (mingw64 readelf, ELF-only), packaging-allowlist manifest
computation (`scripts/package_plugin.py::build_manifest`, read-only), dist-info
metadata, and directory size measurement. No device execution.

---

## A. Executive summary

- **Current allowlisted production artifact: 2247 files / 436.9 MiB**
  (measured with the packager's own `build_manifest`, no staging).
- **Current working tree (excl. `.git`): ≈ 743 MiB** (dominated by
  `runtime/` 395.7 MiB, `node_modules` 139.6 MiB, `.pnpm-store` 138.9 MiB).
- **Largest size contributors (allowlisted):**
  1. `runtime/ocr/site-packages/opencv_python.libs` — 115.3 MiB
  2. `runtime/ocr/site-packages/cv2` — 71.4 MiB
  3. `runtime/ocr/site-packages/onnxruntime` — 60.8 MiB
  4. `runtime/ocr/site-packages/numpy` + `numpy.libs` — 53.6 MiB
  5. `runtime/ocr/site-packages/rapidocr` — 30.9 MiB (29.72 MiB of which is a
     byte-identical duplicate of `models/ppocrv6/`)
  6. `models/ppocrv6` — 29.7 MiB (production det/rec)
  7. `share/tessdata` — 22.2 MiB (Tesseract)
  8. `lib/` — 11.9 MiB (Tesseract/Leptonica stack)
- **KEEP groups identified:** 14 (see §C/§E/§F).
- **LEGACY-CANDIDATE groups:** 2 major (Tesseract stack; `bin/grim`) plus
  several bundled-package test/tool subtrees.
- **DUPLICATE-CANDIDATE groups:** 2 (`rapidocr/models/PP-OCRv6_*.onnx` vs
  `models/ppocrv6/`; `lib/libtesseract.so.5` vs `lib/libtesseract.so.5.0.5`).
- **Estimated removable size:**
  - Tier 1 (high confidence, low risk, no device dependency): **≈ 8.8 MiB**
    (numpy `*/tests/`, `onnxruntime/transformers`, `pydevd_plugins`,
    `site-packages/bin/`, and the duplicate `libtesseract.so.5` copy *if* the
    Tesseract stack is retained).
  - Tier 2 (likely removable, requires Steam Deck device test): **≈ 63.9 MiB**
    (Tesseract stack 34.19 MiB + duplicate RapidOCR PP-OCRv6 models 29.72 MiB).
  - Tier 3 (uncertain / build-level, not file deletion): **≈ 69–115 MiB**
    potential from rebuilding OpenCV as `opencv-python-headless` (removes
    Qt/FFmpeg libraries that are currently *directly* `NEEDED` by `cv2.abi3.so`).

> A 2 MiB risky removal is not worth it. The Tesseract stack (≈ 34 MiB) and the
> duplicate PP-OCRv6 models (≈ 30 MiB) are the only materially valuable
> high-confidence targets.

---

## B. Runtime dependency map

```
Decky PluginLoader
  └─ main.py  (_main → ClarifyDeckEngine; leader lease via backend_leader.py)
       ├─ Capture
       │    capture/producer.py
       │      └─ capture/backend.py  (PipeWire default; screenshot explicit-only)
       │           └─ capture/pipewire_capture.py
       │                ├─ system GStreamer  (gst-launch-1.0 / Gst, NOT bundled)
       │                ├─ system PipeWire
       │                └─ numpy, cv2   (NV12→RGBA, from runtime/ocr/site-packages)
       ├─ OCR
       │    main.py.start_ocr_worker()
       │      └─ OCRWorkerManager → scripts/ocr_worker.py  (subprocess)
       │           └─ ocr/bootstrap.py  (prepends runtime/ocr/site-packages)
       │                └─ ocr/runtime.py
       │                     ├─ rapidocr (RapidOCR)          [site-packages]
       │                     ├─ onnxruntime (CPU EP)         [site-packages]
       │                     ├─ numpy / cv2 / PIL / omegaconf / pyclipper /
       │                     │  shapely / yaml / six          [site-packages]
       │                     └─ models/ppocrv6/PP-OCRv6_{det,rec}_small.onnx
       │                        + rapidocr/models/ch_ppocr_mobile_v2.0_cls_mobile.onnx
       ├─ Text pipeline
       │    ocr/stabilizer.py, ocr/multi_region.py, Fast Accept,
       │    ocr/transport.py ↔ backend/ocr_transport.py
       └─ Overlay
            overlay_manager.py  (AF_UNIX newline JSON)
              └─ overlay/renderer.py  (cairo / X11; persistent overlay + preview)
                   └─ overlay/presentation.py  (style/font/panel_opacity persistence)
Frontend: dist/index.js  (built rollup bundle; dist/index.js.map excluded)
```

Legacy path (NOT used by the current production UI, still reachable via RPCs):

```
main.py.start_plugin()/set_enabled()
  └─ ClarifyDeckEngine._capture_loop → _process_frame → _run_ocr
       └─ bin/tesseract + lib/libtesseract.so.5 + liblept + share/tessdata
```

---

## C. Size table

Allowlisted sizes are from `package_plugin.build_manifest` (excludes `.pyc`,
`__pycache__`, `.map`, `runtime/ocr/wheels`, `backend/src`).

| Component | Path | Size MiB | Classification | Why present | Removal risk |
|---|---|---|---|---|---|
| OpenCV native deps | `runtime/ocr/site-packages/opencv_python.libs` | 115.3 | KEEP-INDIRECT | Directly `NEEDED` by `cv2.abi3.so` (Qt, FFmpeg, OpenBLAS, png) | High — deleting breaks `import cv2` |
| OpenCV module | `runtime/ocr/site-packages/cv2` | 71.4 | KEEP-INDIRECT | `cv2.abi3.so` used by capture + RapidOCR | High |
| ONNX Runtime | `runtime/ocr/site-packages/onnxruntime` | 60.8 | KEEP | `ocr/runtime.py` imports `onnxruntime`; CPU EP | High |
| NumPy | `runtime/ocr/site-packages/numpy` + `numpy.libs` | 53.6 | KEEP | capture + RapidOCR numeric core | High |
| RapidOCR package | `runtime/ocr/site-packages/rapidocr` | 30.9 | KEEP (29.72 MiB duplicate) | Production OCR engine | Split: code KEEP; PP-OCRv6 copies Tier 2 |
| PP-OCRv6 models | `models/ppocrv6` | 29.7 | KEEP | Production det/rec via manifest | High |
| Tesseract tessdata | `share/tessdata` | 22.2 | LEGACY-CANDIDATE | Legacy Tesseract OCR only | Medium (legacy RPCs) |
| Pillow native deps | `runtime/ocr/site-packages/pillow.libs` | 13.4 | KEEP-INDIRECT | PIL imported by RapidOCR | High |
| Tesseract libs | `lib/` | 11.9 | LEGACY-CANDIDATE | Tesseract/Leptonica stack | Medium (legacy RPCs) |
| PIL | `runtime/ocr/site-packages/PIL` | 5.2 | KEEP-INDIRECT | `rapidocr/load_image.py` imports PIL | High |
| Shapely + libs | `runtime/ocr/site-packages/shapely{,.libs}` | 10.2 | KEEP-INDIRECT | RapidOCR dep (declared) | Medium-High |
| PyClipper | `runtime/ocr/site-packages/pyclipper` | 3.4 | KEEP-INDIRECT | RapidOCR dep (declared) | Medium-High |
| PyYAML | `runtime/ocr/site-packages/yaml` | 2.7 | KEEP-INDIRECT | RapidOCR dep (declared) | Medium |
| onnxruntime transformers | `…/onnxruntime/transformers` | 2.27 | LEGACY-CANDIDATE | HF model conversion tools, not inference | Low |
| requests stack | `requests/urllib3/charset_normalizer/idna/certifi` | 1.90 | KEEP-INDIRECT | `rapidocr/download_file.py` imports `requests` | Medium |
| protobuf/flatbuffers/packaging | `…/google`, `flatbuffers`, `packaging` | ~2.1 | KEEP-INDIRECT | onnxruntime declared deps | Medium |
| Tesseract binary | `bin/tesseract` | 0.06 | LEGACY-CANDIDATE | Legacy Tesseract OCR | Medium |
| grim | `bin/grim` | 0.04 | LEGACY-CANDIDATE | Dev spike screenshot only (`scripts/capture_spike.py`) | Low |
| Python modules | `backend/ capture/ ocr/ overlay/ scripts/{ocr_worker,ocr_test}.py` | ~0.6 | KEEP | Production pipeline | High |
| Frontend bundle | `dist/index.js` | 0.07 | KEEP | QAM UI (built; not tracked) | High |

Working-tree-only, excluded from artifact: `node_modules` 139.6, `.pnpm-store`
138.9, `.git` 51.4, `src/`, `scripts/test_*`, docs, `.map`, `runtime/ocr/wheels`
(absent at audit time), `assets/logo.png` (20.6 KiB, not allowlisted).

---

## D. Tesseract audit

| Item | Size MiB | Runtime reference | Indirect dependency | Packaging reason | Candidate status | Removal prerequisite |
|---|---|---|---|---|---|---|
| `bin/tesseract` | 0.06 | `main.py` legacy `_run_ocr`, `_list_langs`, `get_status` | NEEDED `liblept.so.5`, `libtesseract.so.5`, system `libarchive.so.13` | `bin/` copied recursively | **Appears unused by production; still referenced by legacy code** | Retire legacy RPCs + code; device test |
| `lib/libtesseract.so.5.0.5` | 3.66 | `bin/tesseract` NEEDED | `liblept`, `libgomp`, system `libarchive` | `lib/` recursive | Part of legacy stack | Same |
| `lib/libtesseract.so.5` | 3.66 | soname NEEDED | — | `lib/` recursive | **DUPLICATE** of `.so.5.0.5` (identical git blob `c54838ef`, both regular files) | Symlink/packager change, or remove stack |
| `lib/liblept.so.5` | 2.56 | `libtesseract` NEEDED | system `libgif`, `libopenjp2`, `libwebpmux`, `liblzma`, `libzstd`, `libz` | `lib/` recursive | Part of legacy stack | Same |
| `lib/libjpeg.so.8`, `libpng16.so.16`, `libtiff.so.5`, `libwebp.so.6`, `libjbig.so.0` | 1.72 | `liblept` NEEDED | — | `lib/` recursive | Part of legacy stack | Same |
| `lib/libgomp.so.1` | 0.27 | `libtesseract` NEEDED (OpenMP) | — | `lib/` recursive | Part of legacy stack | Same |
| `share/tessdata/*.traineddata` | 22.23 | `TESSDATA_PREFIX` in legacy OCR | — | `share/` recursive | **Appears unused by production** | Same |
| `pytesseract` | absent | — | — | — | Not present | n/a |
| `TESSDATA_PREFIX` handling | — | `main.py` lines 553/1548 | — | code | Legacy code path | Remove legacy code |
| Legacy OCR language config | — | `CLARIFYDECK_OCR_LANG` default `chi_sim+eng`; `set_ocr_lang`/`set_ocr_options` RPCs | — | code | Legacy code path | Remove legacy code |

**Final Tesseract verdict (section 3 question):**
The entire Tesseract stack **can be removed from the production package without
changing the current RapidOCR production behavior**, because:

1. Production OCR is `start_ocr_worker → OCRWorkerManager → scripts/ocr_worker.py
   → RapidOCR/ONNX`, resolving models from `models/ppocrv6/` and packages from
   `runtime/ocr/site-packages/` (`ocr/runtime.py:585-607`, `ocr/bootstrap.py`).
2. The only Tesseract references are in `main.py`'s legacy `ClarifyDeckEngine`
   methods (`_run_ocr`, `_list_langs`, `_find_tesseract`, `_find_tessdata_dir`,
   `get_status`) and the legacy RPCs `start_plugin`/`set_enabled`/
   `set_ocr_lang`/`set_ocr_options`/`run_ocr_now`.
3. The current QAM frontend does **not** call any of those RPCs (only
   `get_status` is called; it reads overlay/worker state, and `_list_langs()`
   returns `[]` gracefully when Tesseract is absent).
4. No `capture/`, `ocr/`, `overlay/`, `backend/` module references Tesseract,
   Leptonica, `lib/`, `share/`, or `bin/`.

**Confidence:** High that production is unaffected; **still requires a Steam
Deck device test** because the legacy RPC surface remains callable externally
and `scripts/test_packaging.py` currently asserts the Tesseract files are
included (must be updated).

---

## E. Runtime / native library audit

Evidence: `readelf -d` (NEEDED / RPATH / RUNPATH).

- **`cv2/cv2.abi3.so`** — directly `NEEDED`:
  `libavcodec`, `libavdevice`, `libavformat`, `libavutil`, `libswscale`,
  `libavif`, `libQt5Core`, `libQt5Gui`, `libQt5Widgets`, `libQt5Test`,
  `libopenblasp`, `libpng16`, plus system `libc/libm/libstdc++/libz/libdl/
  libgcc_s/libpthread`. RPATH `$ORIGIN/../opencv_python.libs`.
  → **KEEP-INDIRECT.** The Qt/FFmpeg libraries are *not* dead weight that can be
  deleted: the dynamic loader resolves them at `import cv2`. Removing them
  requires replacing the wheel with `opencv-python-headless` (build change).
- **`onnxruntime/capi/libonnxruntime.so.1.30.0`** — system libs only
  (`libc/libdl/libm/libpthread/librt/libstdc++/libgcc_s`), RUNPATH `$ORIGIN`.
  Self-contained. **KEEP.**
- **`bin/tesseract`** — `liblept.so.5`, `libtesseract.so.5`, system
  `libarchive.so.13`; RUNPATH `$ORIGIN/../lib`.
- **`lib/libtesseract.so.5.0.5`** — `liblept`, `libgomp`, system `libarchive`.
- **`lib/liblept.so.5`** — `libjpeg`, `libpng16`, `libtiff`, `libwebp`, plus
  **system** `libgif.so.7`, `libopenjp2.so.7`, `libwebpmux.so.3`, `liblzma.so.5`,
  `libzstd.so.1`, `libz.so.1`. The bundled stack is therefore only partially
  self-contained (depends on SteamOS system libs).
- **`numpy.libs`** — `libscipy_openblas64_` 23.2 MiB, `libgfortran` 2.73,
  `libquadmath` 0.26. **KEEP-INDIRECT.**
- **`pillow.libs`** — `libavif`, `libzstd`, `libfreetype`, `libharfbuzz`,
  `libjpeg`, `libtiff`, `libwebp`, `libopenjp2`. **KEEP-INDIRECT.**
- **`shapely.libs`** — GEOS. **KEEP-INDIRECT.**
- **GStreamer / PipeWire** — *not bundled*: production capture resolves
  `shutil.which("gst-launch-1.0")`/`/usr/bin/gst-launch-1.0` and system
  PipeWire (`main.py:1425-1430`, `capture/backend.py`, `capture/pipewire_capture.py`).
  → No bundled GStreamer/PipeWire assets to prune. **KEEP (system).**
- **Overlay renderer** — uses system cairo/X11 (loaded via `ctypes` in
  `overlay/renderer.py`); no bundled native assets beyond system libs.
  → **KEEP (system).**

No `.so` under `runtime/ocr/site-packages` has an RPATH into the plugin `lib/`
directory; each wheel is self-contained (`$ORIGIN`/`*.libs`). The plugin `lib/`
is referenced only by the Tesseract stack.

---

## F. Python dependency audit

Production import graph (from `main.py`, `capture/`, `ocr/`, `overlay/`,
`scripts/ocr_worker.py`):

- **Direct production imports (KEEP):** `numpy` (`capture/pipewire_capture.py`,
  `ocr/runtime.py`), `cv2` (same), `rapidocr` + `onnxruntime` (`ocr/runtime.py`
  lazy), plus stdlib.
- **Transitive via RapidOCR (KEEP-INDIRECT):** `PIL` (`rapidocr/load_image.py`),
  `omegaconf` (`rapidocr/base.py`, `main.py`), `pyclipper`, `shapely`, `yaml`,
  `six`, `tqdm`, `colorlog` (`rapidocr/log.py`), `requests`/`urllib3`/
  `charset_normalizer`/`idna`/`certifi` (`rapidocr/download_file.py`,
  `load_image.py`).
- **Transitive via onnxruntime (KEEP-INDIRECT):** `protobuf` (`google`),
  `flatbuffers`, `packaging`.
- **Test/tool-only shipped (candidates):** `numpy/**/tests/` (493 files,
  6.54 MiB), `onnxruntime/transformers/` (157 files, 2.27 MiB),
  `pydevd_plugins/` (3 files, PyCharm plugin), `site-packages/bin/` console
  scripts (8 files, 0.01 MiB).
- **Unused/legacy in site-packages:** none beyond the above. `runtime/ocr/wheels`
  (build-only) is already excluded and absent at audit time.

Conditional/lazy imports are all `noqa: PLC0415` lazy native imports in
`ocr/runtime.py`; none changes the bundle requirement.

---

## G. Packaging audit

`scripts/package_plugin.py` is **allowlist-driven** at the top level, then
**recursive** within each allowlisted directory.

- `ALLOWLIST_FILES`: `main.py`, `overlay_manager.py`, `backend_leader.py`,
  `plugin.json`, `package.json`, `LICENSE`, `README.md`.
- `ALLOWLIST_DIRS` (copied recursively): `backend`, `capture`, `ocr`, `overlay`,
  `scripts` (filtered to `ocr_worker.py`, `ocr_test.py`, `__init__.py`),
  `runtime`, `models`, `lib`, `share`, `bin`, `defaults`, `py_modules`, `dist`.
- Excluded everywhere: `__pycache__`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`, `.ipynb_checkpoints`, `*.pyc`/`*.pyo`/`*.map`,
  `backend/src`, `runtime/ocr/wheels`, `backend/Dockerfile|Makefile|entrypoint.sh`.

**Why each major group ships:**

| Group | Rule responsible | Notes |
|---|---|---|
| `runtime/` (372.4 MiB) | `ALLOWLIST_DIRS` recursive | Whole site-packages; `.pyc`/tests-included |
| `models/` (29.7) | recursive | Only PP-OCRv6 det/rec + manifest |
| `share/` (22.2) | recursive | Only `tessdata/` |
| `lib/` (11.9) | recursive | Only Tesseract/Leptonica stack |
| `bin/` (0.1) | recursive | `tesseract`, `grim` |
| `dist/` (0.07) | recursive, `.map` excluded | Built bundle; not tracked in Git |

`share/`, `lib/`, `models/`, `runtime/` are **copied wholesale recursively** —
there is no per-file allowlist inside them. This is exactly why the Tesseract
stack and the RapidOCR duplicate models are included: the packager cannot
currently distinguish them from required assets.

---

## H. Git repository hygiene

- **Total tracked bytes: 65.6 MiB**; `.git` object store 51.4 MiB.
- **Not tracked (good):** `runtime/ocr/site-packages/` (`.gitignore` line 68),
  `runtime/ocr/wheels/`, `node_modules`, `.pnpm-store`, `dist/` (line 36),
  `Clarifydeck-prod-out/` (line 83), `__pycache__`/`*.pyc`.
- **Tracked under `runtime/`:** only `runtime/ocr/bundle_manifest.json`
  (10.7 KiB, intentional).
- **Largest tracked files:** `models/ppocrv6/PP-OCRv6_rec_small.onnx` 20.25 MiB,
  `share/tessdata/chi_sim.traineddata` 12.47, `models/…det…` 9.47,
  `share/tessdata/eng.traineddata` 3.92, `lib/libtesseract.so.5` 3.66,
  `lib/libtesseract.so.5.0.5` 3.66, `lib/liblept.so.5` 2.56, remaining tessdata
  ~5.84, remaining libs ~2.0. Total tracked binaries ≈ 64 MiB.
- **Generated output accidentally tracked:** none found (`dist/`, production
  package output, caches all ignored).
- **Duplicate binaries in the current tree:** `lib/libtesseract.so.5` ==
  `lib/libtesseract.so.5.0.5` (identical blob, 3.66 MiB each; normally a
  symlink). `rapidocr/models/PP-OCRv6_{det,rec}_small.onnx` ==
  `models/ppocrv6/…` (identical SHA-256; the former is inside the untracked
  `runtime/` bundle, so it is not a Git duplicate but is an artifact duplicate).
- **Release-asset recommendation:** keep tracked `models/ppocrv6` (needed to
  rebuild `runtime/`), but consider moving the Tesseract assets out of Git once
  removal is approved; the installable artifact is the Release asset, not the
  repository. Git history rewrite / LFS is out of scope for this phase.

---

## I. License / security flags

- **Secrets:** none found. Only a test literal
  `secret = "SECRET-DIALOG-TEXT"` (`scripts/test_stabilizer_reliability_audit.py:131`),
  not a credential. No private keys, tokens, or API keys in tracked text files.
- **Local paths:** only legitimate Steam Deck paths (`/home/deck/...`,
  `~/.Xauthority`, `/run/user/1000`) — no private developer paths.
- **Debug logs / captures / temp files tracked:** none.
- **Bundled Python licenses (all permissive):** rapidocr Apache-2.0,
  onnxruntime MIT, opencv Apache-2.0, numpy BSD-3-Clause + permissive bundle,
  Pillow MIT-CMU, shapely BSD-3, pyclipper MIT, PyYAML MIT, omegaconf BSD,
  antlr4 BSD, six MIT, packaging Apache-2.0/BSD, protobuf BSD-3, flatbuffers
  Apache-2.0, requests Apache-2.0, urllib3 MIT, charset_normalizer MIT, idna
  BSD-3, certifi MPL-2.0, tqdm MPL-2.0/MIT, colorlog MIT.
- **Redistribution review before public v0.1 (no legal certainty claimed):**
  1. **RapidOCR** — `rapidocr-3.9.2.dist-info` ships **no LICENSE file**
     (METADATA declares Apache-2.0). Recommend bundling the upstream
     Apache-2.0 text in the artifact.
  2. **PP-OCRv6 models** (`models/ppocrv6/*.onnx`) — distributed via PaddleOCR;
     confirm the model license (upstream PaddleOCR is Apache-2.0) accompanies
     redistribution.
  3. **Tesseract / tessdata** — Apache-2.0; **Leptonica** BSD-2. If the stack is
     retained, ensure their license texts ship; if removed, this review item
     disappears.
  4. **OpenCV** `cv2/LICENSE-3RD-PARTY.txt` and **onnxruntime**
     `ThirdPartyNotices.txt` are present in the bundle (good).
  5. Root `LICENSE` is the unmodified Decky template ("Hypothetical Plugin
     Developer") — should be replaced with the real project license before
     release.
  6. `bin/grim` is MIT; `lib/libgomp.so.1` is GPL with the GCC Runtime Library
     Exception (redistributable with GCC-compiled binaries; review if shipped).

---

## J. Slimming candidates

### Tier 1 — high-confidence removal candidates (no device dependency, ≈ 8.8 MiB)

1. `runtime/ocr/site-packages/numpy/**/tests/` — **6.54 MiB**, 493 files.
   Test-only; never imported at runtime. (Keep `numpy/testing`.)
2. `runtime/ocr/site-packages/onnxruntime/transformers/` — **2.27 MiB**.
   Model-conversion tooling (HF/Stable Diffusion/Whisper/Llama), not used by
   inference or RapidOCR.
3. `runtime/ocr/site-packages/pydevd_plugins/` — ~0 MiB. PyCharm/pydev leftover.
4. `runtime/ocr/site-packages/bin/` — 0.01 MiB. Console entry-point scripts.
5. `lib/libtesseract.so.5` duplicate copy — **3.66 MiB** *only if the Tesseract
   stack is retained* (make it a real symlink / teach the packager
   `symlinks=True`; the soname `libtesseract.so.5` must still resolve).

> Tier 1 items 1–4 are inside `runtime/`, which the packager copies recursively.
> Applying them requires either pruning the build (`build_ocr_runtime_bundle.py`)
> or adding explicit exclude rules to `package_plugin.py`. Deleting them from the
> working tree alone would be undone on the next bundle rebuild.

### Tier 2 — likely removable but requires Steam Deck device test (≈ 63.9 MiB)

1. **Tesseract stack** — `bin/tesseract`, `bin/grim`, `lib/*`, `share/tessdata/*`
   = **34.19 MiB** (of which tessdata 22.23, libs 11.86, binaries 0.10).
   Production RapidOCR is unaffected (evidence §D). Risk: legacy RPCs
   (`start_plugin`, `set_enabled`, `set_ocr_lang`, `set_ocr_options`,
   `run_ocr_now`) and `get_status`'s `tesseract`/`tessdata` fields change
   behavior; `scripts/test_packaging.py` asserts these files.
2. **RapidOCR duplicate PP-OCRv6 models** —
   `runtime/ocr/site-packages/rapidocr/models/PP-OCRv6_{det,rec}_small.onnx`
   = **29.72 MiB** (byte-identical to `models/ppocrv6/`; production pins
   explicit absolute paths and `_verify_model_paths` fails closed). Keep the
   classifier `ch_ppocr_mobile_v2.0_cls_mobile.onnx` (0.56 MiB). Risk: RapidOCR
   package internals may expect default model files to exist even when
   overridden — must be device-tested.

### Tier 3 — uncertain / build-level (do not delete files)

1. **OpenCV Qt/FFmpeg libs** (Qt ≈ 27.6 MiB, FFmpeg ≈ 28.7 MiB, codecs ≈ 12.4
   MiB, plus in-`.so` code). Directly `NEEDED` by `cv2.abi3.so` → cannot be
   deleted. Only recoverable by switching to `opencv-python-headless` (wheel
   rebuild + device test). **KEEP as-is for now.**
2. **requests/urllib3/charset_normalizer/idna/certifi** — 1.90 MiB. Declared and
   imported by RapidOCR; do not remove without device test.
3. **GStreamer/PipeWire, cairo/X11** — system-provided; nothing bundled to prune.
4. **`capture/gamescope_capture.py`** — legacy/diagnostic screenshot backend,
   retained by explicit policy. Not a size concern; keep.

---

## K. Proposed future deletion order

One group per step. Each step: files, packaging change, tests, device test,
rollback checkpoint, expected saving. **Do not combine steps.**

### Step 1 — Bundled test/tool pruning (Tier 1, no device dependency)

- **Files:** `runtime/ocr/site-packages/numpy/**/tests/`,
  `runtime/ocr/site-packages/onnxruntime/transformers/`,
  `runtime/ocr/site-packages/pydevd_plugins/`,
  `runtime/ocr/site-packages/bin/`.
- **Packaging change:** add explicit excludes in `scripts/package_plugin.py`
  (e.g. `EXCLUDE_RELATIVE_DIRS`/glob excludes) *and* prune in
  `scripts/build_ocr_runtime_bundle.py` so a rebuild stays clean.
- **Tests:** `python scripts/test_packaging.py`, `python scripts/test_ocr_bundle.py`,
  full `scripts/test_*.py` suite.
- **Device test:** Start OCR → confirm text renders; run one capture/OCR cycle.
- **Rollback:** current commit (audit checkpoint).
- **Expected saving:** ≈ 8.8 MiB (6.54 + 2.27 + ~0.01).

### Step 2 — Remove duplicate RapidOCR PP-OCRv6 models (Tier 2)

- **Files:** `runtime/ocr/site-packages/rapidocr/models/PP-OCRv6_det_small.onnx`,
  `…/PP-OCRv6_rec_small.onnx` (keep the cls model).
- **Packaging change:** prune in `build_ocr_runtime_bundle.py`; add a packaging
  test asserting the classifier remains and `models/ppocrv6/` is intact.
- **Tests:** `test_ocr_runtime.py`, `test_ocr_bundle.py`, `test_multi_region_ocr.py`,
  `test_packaging.py`; verify `_verify_model_paths` still passes in a probe.
- **Device test:** Start/Stop OCR; confirm det/rec run from `models/ppocrv6/`
  (check `[ocr] det_model=`/`rec_model=` probe output and on-device text).
- **Rollback:** commit from Step 1.
- **Expected saving:** ≈ 29.72 MiB.

### Step 3 — Retire legacy Tesseract code paths, then remove the stack (Tier 2)

- **Files (code first):** `main.py` legacy `_run_ocr`, `_list_langs`,
  `_find_tesseract`, `_find_tessdata_dir`, `_text_score`, `_clean_ocr_text`,
  `set_ocr_lang`/`set_ocr_options`/`run_ocr_now`, legacy `start_plugin`/
  `set_enabled` RPCs, and the tesseract fields in `get_status`; then
  `bin/tesseract`, `bin/grim`, `lib/*`, `share/tessdata/*`.
- **Packaging change:** remove `bin`, `lib`, `share` from `ALLOWLIST_DIRS`
  (or leave empty dirs absent); update `scripts/test_packaging.py` fixture.
- **Tests:** full Python suite; `test_packaging.py`; frontend harness
  (`npm test`) to confirm no UI RPC references legacy names.
- **Device test:** Start/Stop OCR, Persistent Overlay, Panel Opacity, Region
  Preview, profile/region switching; confirm Steam screenshot notifications
  remain 0.
- **Rollback:** commit from Step 2.
- **Expected saving:** ≈ 34.19 MiB (incl. the `libtesseract` duplicate).

### Step 4 (build-level, optional) — OpenCV headless wheel (Tier 3)

- **Files:** rebuild `runtime/ocr` with `opencv-python-headless`; regenerate
  `runtime/ocr/bundle_manifest.json`.
- **Packaging change:** none beyond the rebuilt bundle.
- **Tests:** `test_ocr_runtime.py`, `test_pipewire_capture.py`,
  `test_capture_producer.py`, `test_ocr_bundle.py`, full suite.
- **Device test:** full OCR + capture cycle (NV12 path uses cv2 in
  `capture/pipewire_capture.py`), overlay rendering.
- **Rollback:** commit from Step 3.
- **Expected saving:** ≈ 69–115 MiB (Qt + FFmpeg libs and associated `cv2` code),
  at higher risk and with a real wheel-compatibility dependency on the Deck.

---

## Appendix — evidence commands (read-only)

```
git status / git rev-parse HEAD / git branch --show-current
git log --oneline -6
git ls-files -s bin lib share models
git ls-files | <top-dir grouping> / <largest tracked files>
Get-FileHash models/ppocrv6/*.onnx runtime/ocr/site-packages/rapidocr/models/*.onnx
readelf -d cv2/cv2.abi3.so | onnxruntime/capi/libonnxruntime.so.1.30.0 |
          bin/tesseract | lib/libtesseract.so.5.0.5 | lib/liblept.so.5
Select-String (tesseract|tessdata|lept|grim) across production modules
python -c "import scripts.package_plugin as p; p.build_manifest(Path('.'))"
dist-info RECORD / METADATA inspection
```

No production package was built. No files were deleted or modified by this
audit.
