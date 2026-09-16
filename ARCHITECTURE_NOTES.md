# ClarifyDeck Architecture Notes

Running notes gathered during the Phase 1 overlay investigation. These are
evidence-based findings, not assumptions.

## Overlay persistence root cause

The Steam UI / QAM compositor layer is **not** kept composited over a running
game once the QAM is closed. Consequences observed on device:

- A plain DOM node appended to `document.body` is invisible even while the QAM
  is open (`CD raw` probe never appeared).
- A component mounted via `routerHook.addGlobalComponent` (Steam UI app tree)
  is visible only while the Steam UI layer is active (QAM open, standby, Steam
  background) and disappears when the QAM is closed.
- A **visible** Steam notification keeps the layer composited and therefore
  keeps the app-tree overlay visible; a silent notification (`showToast:false`)
  does not.

Conclusion: this is a compositor-layer visibility problem, not a React unmount
problem. `z-index` / CSS cannot fix it. A persistent overlay must live outside
the Steam UI layer.

## Gamescope external overlay mechanism (confirmed in source)

From `ValveSoftware/gamescope` `src/main.cpp` `UpdateCompatEnvVars()`:

```
// We no longer need to set GAMESCOPE_EXTERNAL_OVERLAY from steam, mangoapp now does it itself
setenv("STEAM_DISABLE_MANGOAPP_ATOM_WORKAROUND", "1", 0);
```

- The supported classification is the X11 window property
  `GAMESCOPE_EXTERNAL_OVERLAY` (CARDINAL, 32-bit), set **before** mapping.
- `isExternalOverlay = get_prop(GAMESCOPE_EXTERNAL_OVERLAY, 0)`, so `0` is
  false and non-zero is true.
- Modern gamescope sets the atom from mangoapp itself; a third-party X11 window
  may set it directly (to be confirmed by the PoC).
- `GAMESCOPE_NO_FOCUS=1` is an extra hint; focus exclusion for external
  overlays is handled by gamescope itself.
- Do not set `STEAM_GAME` / `STEAM_OVERLAY` for this overlay.

Reference implementation: `src/mangoapp.cpp` (gamescope feeds mangoapp over a
System V message queue; mangoapp is the known-working persistent overlay).

## Screenshot interface (for a later phase, not implemented)

`protocol/gamescope-control.xml` exposes `take_screenshot(path, type, flags)`
with `screenshot_type`:

- `base_plane_only` (1) — game only, no overlays
- `all_real_layers` (2) — game + overlays
- `full_composition` (3)
- `screen_buffer` (4)

Using `base_plane_only` would exclude ClarifyDeck's own overlay from OCR input
and remove the current feedback loop (overlay -> screenshot -> OCR -> overlay).
Device investigation reported `gamescope_control` version 6 is exposed on
`WAYLAND_DISPLAY=gamescope-0`.

Also: `SIGUSR2` triggers `CScreenshotManager::Get().TakeScreenshot(true)`.

## Device XWayland topology (from Phase 1 probe)

- `DISPLAY=:0` — Steam Big Picture + mangoapp (mangoapp overlay window
  1280x800, `DISPLAY=:0`, `GAMESCOPE_WAYLAND_DISPLAY=gamescope-0`).
- `DISPLAY=:1` — the running game (Lies of P), window 1280x800.

The external overlay is created on the Steam/mangoapp XWayland (`:0`), not the
game's (`:1`). The final product must discover this dynamically instead of
hardcoding `:0`.

Note: `pgrep -o gamescope` can match `/bin/sh /usr/bin/start-gamescope-session`
instead of the real gamescope process; use `pgrep -x gamescope` or the
`GAMESCOPE_PID` root property. `pgrep -o steam` can match `steamos-manager`.

## Phase status

- Phase 0 (audit) — done.
- Phase 1 investigation — done.
- Phase 1B (external overlay PoC) — PASS on device (`scripts/overlay_poc/`).
- Phase 1C (persistent renderer + IPC) — implemented (`overlay/`, `overlay_manager.py`).
- Phase 1C.2 (recovery-safe overlay) — implemented; see `PHASE_1C2_AUDIT.md`
  and `RECOVERY.md`.
- Phase 2A (capture isolation) — PASS on device (`capture/gamescope_capture.py`,
  `scripts/gamescope_capture_test.py`).
- Phase 2B (in-memory CaptureFrame + latest-frame queue) — PASS on device
  (`capture/frame.py`, `capture/latest_frame_queue.py`,
  `scripts/capture_queue_test.py`).
- Phase 2C (bounded low-rate capture producer) — implemented
  (`capture/producer.py`, `scripts/capture_producer_test.py`).
- Phase 2C.2 (live-backend Wayland env fix) — implemented; live backend resolves
  the Gamescope socket explicitly instead of relying on shell env.
- Phase 2D (frame freshness + change detection) — implemented
  (`capture/change_detector.py`, `scripts/change_detection_test.py`).
- OCR migration (PP-OCRv6), game profiles, temporal stabilization — not started.

## Phase 2D change detection

- `FrameChangeDetector` (`capture/change_detector.py`) decodes a CaptureFrame's
  in-memory PNG with a small stdlib decoder (no OpenCV/Pillow), computes a
  compact 32x20 luminance signature, and classifies changed/unchanged against a
  single retained baseline (baseline updates only when changed).
- The stdlib PNG decoder expands RGB rows to RGBA with C-level extended-slice
  assignment (a per-pixel Python loop was the CPU hot spot: ~234 ms -> ~7 ms per
  1280x800 frame locally). Stats expose decode_ms/signature_ms/compare_ms/total_ms.
- Phase 2D.3 adds a native libpng fast path via the libpng simplified API
  (`png_image_begin_read_from_memory`/`png_image_finish_read`/`png_image_free`)
  through ctypes, gated to 8-bit RGB/RGBA non-interlaced PNGs (the Gamescope
  screenshot format); the stdlib decoder stays as reference/fallback with an
  explicit `decoder_fallback_reason`. The library is resolved and bound once per
  process. A structural parser reports width/height/bit_depth/color_type/
  interlace/idat_bytes/bpp/row_bytes; the filter histogram is collected only for
  diagnostics (`--debug`/`collect_filters`) so production frames never re-run the
  Python inflate/unfilter. Native substages are reported as `None` (not
  observable from Python).
- The diagnostic prints process CPU attribution (`time.process_time`) and loop
  counters (iterations / frames_received / empty_polls / wait_wakeups) so a
  busy-loop is distinguishable from unavoidable decode cost.
- Decisions: `first_frame`, `changed`, `below_threshold`, `stale_sequence`,
  `too_old` (opt-in `max_frame_age_ms`), `decode_error`. `age_ms` uses
  `time.monotonic()` against `frame.captured_monotonic`.
- Default threshold calibrated from true-device data to `0.005` (static noise
  ≤ ~0.0009, deliberate scene change ≥ ~0.0112); configurable via constructor
  and `--threshold`.
- Bounded state: one baseline signature + scalars only; no frame/score history.
- Pipeline: producer → LatestFrameQueue(1) → detector consumer (diagnostic);
  shutdown clears the queue and resets the detector (queue.pending=0).
- Known limitation: full-frame 32x20 downsampling misses small subtitle-like
  text changes; ROI (Phase 2E) is the intended fix.
- No detector task starts at boot; only the diagnostic script runs it.

## Phase 2E ROI foundation

- `capture/roi.py` adds a bounded subtitle ROI layer that runs *after* native
  RGBA decode: `NormalizedROI` -> `resolve_roi` -> `PixelROI` -> `crop_rgba` ->
  `preprocess_roi` -> `luminance_signature` -> ROI score. No OCR, no overlay
  wiring, no worker started at boot.
- `resolve_roi` uses a documented deterministic half-up rounding policy and
  clamps partially out-of-frame ROIs; fully outside, zero-size, NaN and inf ROIs
  are rejected. Default ROI `(0.08, 0.62, 0.84, 0.32)` resolves to
  `(102, 496, 1075, 256)` at 1280x800.
- `crop_rgba` copies row slices only (no per-pixel Python objects) and rejects
  buffers whose length does not match the frame.
- `preprocess_roi` is the future-OCR scaling interface: `scale=1` is the
  identity, 2x/3x Lanczos is deferred (`scale_unavailable`) rather than pulling
  in OpenCV/Pillow.
- `ROIProfileStore` / `GameROIProfile` provide a static default profile plus
  optional exact `app_id` overrides; no Steam metadata lookup, no UI.
- `ROIChangeDetector` reuses the same signature/score method but with its own
  `ROI_GRID = (48, 12)` and `ROI_CHANGE_THRESHOLD = 0.006`; the frozen
  full-frame `DEFAULT_GRID = (32, 20)` / `DEFAULT_THRESHOLD = 0.005` are
  untouched. Bounded state: one baseline signature + one current ROI buffer.
- Calibrated bands (320x200 synthetic): static ROI noise `0.0`, `25x10`
  subtitle-like block `roi=0.0106` vs `full=0.0019` (full-frame misses), large
  ROI change `roi=0.408`. The diagnostic (`scripts/roi_test.py`) reproduces
  this: `--mock-subtitle --full` shows `full max_score=0.00235` while the ROI
  detector flags 5 changes.
- Debug export (`--debug-copy PATH`) writes one PNG built from the in-memory ROI
  RGBA (`encode_rgba_png`), never from the transient Gamescope screenshot path.

## Phase 2E.1 subtitle band

- True-device Gate 5 failed with the broad ROI alone: mild camera/background
  motion pushed the ROI score to ~0.016–0.022 (changed 31/31). Raising the
  threshold would reintroduce subtitle misses, so Phase 2E.1 refines geometry
  instead of retuning the threshold.
- `SubtitleBandROI` is a `NormalizedROI` subclass interpreted *relative to the
  broad ROI*. `resolve_subtitle_band(band, broad_w, broad_h)` shares the exact
  half-up rounding/clamp rules with `resolve_roi`. Default band
  `(0.05, 0.35, 0.90, 0.45)` resolves to `(13, 22, 242, 29)` inside the 1280x800
  broad ROI `(102, 496, 1075, 256)`.
- Pipeline per frame: one libpng decode -> broad crop -> band crop (from the
  broad RGBA, never re-decoded) -> `ROIChangeDetector` on the band.
  `crop_rgba` has a zero-copy fast path when the ROI covers the whole buffer.
- `GameROIProfile` gained a defaulted `subtitle_band` field (existing callers
  stay compatible); `ROIProfileStore`/`resolve_profile` now carry broad ROI +
  band + scale + label.
- Decode-once support: `ROIChangeDetector.classify_rgba(...)` and
  `classify_subtitle_band(SubtitleBandFrame)` classify already-decoded pixels;
  `classify(frame)` is unchanged and still decodes internally.
- Frozen thresholds untouched: `ROI_GRID=(48,12)`, `ROI_CHANGE_THRESHOLD=0.006`,
  full-frame `(32,20)`/`0.005`. Geometry is isolated from threshold changes.
- Synthetic isolation (320x200): a 40x12 block moving inside the broad ROI but
  above the band -> broad `changed` (score 0.0105) while band score is exactly
  `0.0` (unchanged). A 25x10 text block inside the band -> band score 0.0211
  (changed) while the full-frame score stays below 0.005.
- Diagnostic `scripts/roi_test.py` reports `[broad]` and `[band]` scores and
  writes `--debug-copy` (band) / `--debug-copy-broad` from in-memory buffers.

## Phase 2E.2 user-configurable recognition ROI

- The authoritative working region is now a **full-frame** `NormalizedROI`, not a
  nested band. `capture/recognition_roi.py` adds `RecognitionROI(roi, source)`,
  `ActiveROIResolver`, `ROIFrame`, `resolve_active_recognition_roi()` and
  `extract_recognition_roi()` (resolve -> pixel map -> crop once, no re-decode).
- Resolution priority: user per-game override -> game profile preset -> user
  global default -> built-in default. Sources: `user` / `game_profile` /
  `default`. The broad ROI and subtitle band stay as internal presets/diagnostics.
- Persistence: `ROIConfigStore` writes `recognition_roi.json` (version 1,
  `default_roi`, per-app `games`) with strict validation, a `MAX_CONFIG_BYTES`
  (64 KiB) cap, UTF-8, temp-file + `fsync` + `os.replace` atomic save, and
  fail-safe loading (corrupt/oversized/unknown-version files fall back to
  defaults and report `last_error`). Writes happen only on explicit user changes.
  Path: `decky.DECKY_PLUGIN_SETTINGS_DIR` (or `CLARIFYDECK_ROI_CONFIG`).
- Bounds (reject, never clamp user input): `0 <= x,y < 1`, `0 < width,height <= 1`,
  `x+width <= 1`, `y+height <= 1`, `width,height >= MIN_ROI_SIZE (0.02)`.
- Backend RPCs (`main.py`): `roi_config_get`, `roi_config_set`, `roi_config_reset`,
  `roi_config_preview` — all validate server-side and return `{ok, source, roi,
  pixel, frame, config_path, config_error}`. No capture/renderer is started.
- Frontend `src/index.tsx` gains a "Recognition Area" panel: X/Y/W/H percent
  sliders, Apply/Reset, and presets (Bottom 20%/30%, Center, Full frame).
  Client-side validation blocks invalid input; the backend stays authoritative.
- Change detection is now an optional optimization layer: the recognition ROI is
  usable with the detector disabled, and future OCR must call
  `resolve_active_recognition_roi()` rather than re-deriving profiles.
- Phase 2E.2a adds a config-only device harness to `scripts/roi_test.py`:
  `--set-active-roi x,y,w,h` and `--reset-active-roi` (optionally with
  `--app-id` / `--roi-config`). It reuses `ROIConfigStore` + the same validation
  (no looser CLI rules), writes atomically, and never starts capture/renderer.
  `scripts/test_roi_config_harness.py` covers set/get/reset per-game + fallback.

## Phase 2F OCR diagnostic integration

- OCR lives in a separate `ocr/` package and is **never imported by `main.py`**:
  `OCRLine`/`OCRFrameResult` (`ocr/result.py`) and `OCRRuntime`/`OCRConfig`/
  `ModelManifest`/`probe_runtime`/`rgba_to_array` (`ocr/runtime.py`). RapidOCR,
  ONNX Runtime and NumPy are imported lazily inside functions, so the Decky
  plugin loader never loads a native extension at boot.
- Runtime pipeline: `CaptureFrame -> decode_png_ex (once) -> ActiveROIResolver ->
  crop_rgba (once) -> OCR`. Change detection is not required; OCR runs on every
  consumed frame at the diagnostic cadence (default 1 FPS, 0.2–2 FPS allowed).
- `OCRRuntime` initializes once per process (`engine_init_count == 1`) and is
  injectable (`engine_factory`) for tests. Model assets are explicit local files
  validated against `models/<family>/manifest.json` (missing asset / hash
  mismatch / bad version fail closed; no implicit download). RGBA is converted
  with one explicit, tested color order (`bgr` default, `rgb` optional).
- `scripts/ocr_runtime_probe.py` reports interpreter/native/provider/model status
  and exits 0 only when numpy + rapidocr + onnxruntime + `CPUExecutionProvider`
  are all present. `scripts/ocr_test.py` adds `--probe`, `--image`, `--live`
  (plus `--mock`), bounded metrics, a 3-consecutive-error failure threshold, and
  clean shutdown (producer stop, queue clear, runtime close).
- `scripts/test_ocr_runtime.py` (36 tests) exercises the adapter with a fake
  engine (init-once, manifest failures, color order, UTF-8/confidence/box
  preservation, close idempotency, decode-once, crop-once, queue newest-wins,
  consecutive-error failure, no auto-start).

## Phase 2F.1 OCR runtime packaging

- The Steam Deck Gate 0 failure was a packaging blocker (`numpy`/`rapidocr`/
  `onnxruntime` absent on `/usr/bin/python3`), not an algorithm failure. OCR
  dependencies are now delivered as an **offline, plugin-local bundle**:
  `runtime/ocr/site-packages/` + `runtime/ocr/bundle_manifest.json`.
- `ocr/bootstrap.py` (`activate_plugin_ocr_runtime`) prepends that directory to
  `sys.path` once; it never runs pip, never downloads, never mutates global
  site-packages/PATH, and is imported only by OCR diagnostic scripts (never
  `main.py`).
- `ocr/bundle.py` models the bundle (`BundleTarget`, `BundleManifest`),
  validates target python/abi/arch against the running interpreter, and rejects
  foreign-OS/arch/CPython wheel filenames.
- `scripts/build_ocr_runtime_bundle.py` acquires binary-only cp313 x86_64 Linux
  wheels (`rapidocr==3.9.2`, `onnxruntime==1.30.0`, `numpy<3` + dependency tree),
  validates tags/hashes, installs via pip cross-target mode, and writes the
  bundle manifest. Build is a Linux-host/Docker/WSL operation; nothing native
  runs on Windows.
- Model manifest upgraded to **v2** (v1 still accepted):
  `models/ppocrv6/manifest.json` declares `PP-OCRv6_det_small.onnx` /
  `PP-OCRv6_rec_small.onnx` with SHA256s, `dictionary.mode=embedded` (PP-OCRv6
  ONNX carries character metadata), and rejects `.traineddata` assets.
  `Tesseract` `share/tessdata/` is left untouched.
- `_rapidocr_params` passes explicit absolute model paths (no network model
  download) using `Det./Rec.engine_type|ocr_version|model_type|model_path`.
- `scripts/test_ocr_bundle.py` (23 tests) covers bootstrap idempotency, missing/
  invalid/target-mismatched bundles, wheel tag validation, manifest v2 hashes,
  embedded/external dictionary modes, `.traineddata` rejection, params keys, and
  no-OCR-in-`main.py` / no-capture-in-probe.

## Phase 2F.1a wheel resolver compatibility

- The bundle build failed at pip resolution (`Could not find a version that
  satisfies pyclipper>=1.2.0`) because a single `--platform
  manylinux_2_28_x86_64` / `--abi cp313` tag set is too restrictive.
- Wheel policy is now centralized in `ocr.bundle.is_target_compatible_wheel`
  (one predicate shared by resolver, validator and manifest), using
  `packaging.utils.parse_wheel_filename` when available with a tested manual
  fallback.
- Resolver tags: platforms `manylinux_2_28..2_17_x86_64` + `manylinux2014_x86_64`
  (`resolver_platforms`), ABIs `cp313` + `abi3` + `none` (`resolver_abis`).
  Both the download and install steps pass the full platform/ABI set.
- `is_target_compatible_wheel` accepts cp313/abi3 (stable ABI, `cp3X` with
  X ≤ 13) / `py3-none-any` manylinux wheels within the 2.17–2.28 floor, and
  rejects win/macOS/aarch64/i686/musllinux, cp311/cp314-only, PyPy, and sdists.
- `build_ocr_runtime_bundle.py` prints resolver diagnostics
  (`--print-target-tags`), records `resolver` + per-wheel python/abi/platform
  tags in `bundle_manifest.json`, and reports `dependency_bundle` and
  `model_assets` as separate statuses.
- Real run (Windows host cross-resolver, cp313 manylinux): 22 wheels resolved
  (RapidOCR 3.9.2, ONNX Runtime 1.30.0, NumPy 2.5.3, OpenCV 5.0.0.93,
  pyclipper 1.4.0, …), `dependency_bundle=PASS`, `install=PASS`
  (site-packages ≈ 415 MB), `model_assets=FAIL` (PP-OCRv6 det/rec absent).

## Phase 2F.1c OCR test bootstrap fix

- Gate 0.2/0.3 passed but Gate 0.4 failed: `scripts/ocr_test.py` never activated
  the plugin-local runtime, so `rapidocr`/`onnxruntime` were unresolvable.
- `ocr_test.py` now calls `activate_plugin_ocr_runtime(...)` (the canonical
  `ocr.bootstrap` helper) before building the runtime / probing / importing any
  native dependency; it accepts `--plugin-root` and `--require-bundle`. No
  `sys.path` duplication, no hard-coded deck paths, no user-site fallback.
- Probe output is shared: `ocr.runtime.probe_report_lines` + `PROBE_DEPENDENCIES`
  are used by both `ocr_runtime_probe.py` and `ocr_test.py --probe`, so the two
  cannot diverge (runtime activation, dependency source paths, providers, model
  status, compatibility).
- Fail-closed: a missing bundle with `--require-bundle` → `bundle_error=runtime_bundle_missing`
  exit 1; a target-mismatched bundle → `bundle_error=bundle_target_mismatch`
  exit 1 (e.g. the cp313 deck bundle on the cp314 Windows dev host).
- `runtime/ocr/site-packages/` and `runtime/ocr/wheels/` are gitignored (large
  build artifacts); `runtime/ocr/bundle_manifest.json` remains the build evidence.

## Phase 2F.1d OmegaConf runtime compatibility

- Gate 0.4 reached RapidOCR init and failed with
  `omegaconf.errors.UnsupportedValueType: Value 'PosixPath' is not a supported
  primitive type (Global.model_root_dir)`. RapidOCR 3.9.2 assigns a
  `pathlib.Path` into its OmegaConf config; the bundle had resolved OmegaConf
  **2.0.0** (newer OmegaConf pulls `antlr4-python3-runtime==4.9.*`, whose PyPI
  4.9.3 release is sdist-only, so the binary-only resolver fell back).
- Fix is a version pin, not a vendor patch:
  `BUNDLE_COMPAT_PINS = {omegaconf: 2.3.1, antlr4-python3-runtime: 4.9.3}`,
  `BUNDLE_REJECTED_VERSIONS = {omegaconf: (2.0.0, 2.2.1)}`, enforced by
  `validate_bundle_compat` in `activate_plugin_ocr_runtime` and after each build.
  `rapidocr/main.py` is untouched (no `Path -> str` fork).
- `antlr4-python3-runtime==4.9.3` is the single sanctioned build-host-only
  pure-Python wheel conversion (`pip wheel --no-deps`, `py3-none-any`, no
  compiler); `build_ocr_runtime_bundle.py --prepare-antlr` performs it and
  records provenance (`source=PyPI sdist`, `source_version`, `built_on`) in the
  manifest alongside `wheel_sha256`.
- Rebuilt bundle: 23 wheels, `dependency_bundle=PASS`, `install=PASS`,
  `model_assets=PASS` (PP-OCRv6 det/rec present and hash-valid),
  `manifest_validated compat_pins_ok`. Local verification:
  `omegaconf 2.3.1` + `antlr4` resolve under `runtime/ocr/site-packages/`, and
  `cfg.Global.model_root_dir = Path(...)` now succeeds.

## Phase 2F.1e local wheelhouse resolver bridge

- The 2F.1d rebuild failed at the main cross-platform `pip download` with
  `Could not find a version that satisfies the requirement antlr4-python3-runtime==4.9.*`:
  the locally built ANTLR wheel was not visible to the resolver.
- Build ordering is now explicit and enforced in `build_ocr_runtime_bundle.py`:
  prepare the ANTLR wheel into the wheelhouse → print
  `prebuilt_local_wheel=` / `resolver_find_links=` → run the main resolver with
  `--find-links <wheelhouse>` → validate → offline install. The wheelhouse is
  never cleared after the prebuild (`--clean-site-packages` only removes
  `site-packages`).
- `--prepare-antlr` is now default-on (`--no-prepare-antlr` to disable);
  `_prepare_antlr` reuses a valid existing wheel and rebuilds an invalid one.
- Command builders are pure/testable: `_download_command` (has `--find-links`,
  no `--no-index`) and `_install_command` (has `--no-index` + `--find-links`).
- Proof of the exact failure/fix: `pip download omegaconf==2.3.1` without
  `--find-links` → `ERROR ... versions: 4.11.0 … 4.13.2`; with
  `--find-links <wheelhouse>` → resolves `antlr4-python3-runtime 4.9.3` locally
  plus `omegaconf 2.3.1`, `PyYAML`.
- Rebuilt deployment artifact `runtime/ocr/`: 23 wheels incl. `omegaconf 2.3.1`
  + `antlr4-python3-runtime 4.9.3`; `dependency_bundle=PASS`, `install=PASS`,
  `model_assets=PASS`, `manifest_validated compat_pins_ok`.

## Phase 2F.1f explicit model path wiring

- Gate 0.4 initialized RapidOCR but used RapidOCR's own bundled models
  (`site-packages/rapidocr/models/PP-OCRv6_*.onnx`) instead of ClarifyDeck's.
- Root cause: RapidOCR 3.9.2's `ParseParams.update_batch` requires **Enum**
  instances for `engine_type`/`ocr_version`/`model_type`; the adapter passed
  strings, which raised `TypeError`, and the old `except TypeError: RapidOCR()`
  fallback silently reverted to defaults.
- `_rapidocr_params` now passes `Det.model_path`/`Rec.model_path` as absolute
  ClarifyDeck paths, enum params as real `EngineType`/`OCRVersion`/`ModelType`
  instances, and `Global.use_cls=False`; the silent fallback is removed.
- `_verify_model_paths` reads RapidOCR's resolved `engine.cfg` and fails closed
  (`model_path_not_applied`) unless it matches our explicit det/rec paths.
  `OrtInferSession` uses `model_path` when set, so no `model_root_dir`/download
  path is taken (`Global.model_root_dir` is never passed).
- Classifier: RapidOCR 3.9.2 always constructs `TextClassifier` in
  `_initialize` even with `use_cls=False`, so it cannot be disabled cleanly.
  Phase 2F.1f disables its *use* and pins `Cls.model_path` explicitly to the
  RapidOCR-bundled `ch_ppocr_mobile_v2.0_cls_mobile.onnx` to avoid implicit
  resolution/download; this reliance is reported for a follow-up decision.
- `OCRRuntime.model_paths` + `ocr_test.py --probe` now print
  `[ocr] det_model=`, `[ocr] rec_model=`, `[ocr] classifier=`.

## Phase 2F.2 RapidOCR result normalization

- Static-image OCR reached real inference but failed post-inference with
  `The truth value of an array with more than one element is ambiguous`.
- Exact cause: `_normalize_rapidocr_result` did
  `boxes = getattr(result, "boxes") or []` — `bool(numpy.ndarray)` is ambiguous.
- Rewritten with explicit `is None` presence checks, separate structured vs
  legacy `(payload, elapse)` paths, length validation (`invalid_engine_result`
  instead of silent `zip` truncation), and `_elapse_ms` for `elapse_list`.
  Empty/no-text results normalize to `lines=()` (not an error); `0.0` confidence
  is preserved (no `score or None`).
- `describe_result()` provides a bounded `--debug` structural summary
  (type/shape/len only — never arrays/images); `OCRConfig.debug` enables it.
- `_RapidOCREngine` stores `last_result`; `OCRConfig` gained `debug`.

## Phase 2F.3 live OCR performance attribution

- Live OCR was functionally correct but slow (`avg_ocr_ms≈2806`, CPU≈397%).
  Phase 2F.3 adds instrumentation + controlled thread tuning (no feature work).
- Stage timings: `decode_ms`, `roi_crop_ms`, `rgba_to_array_ms`, `ocr_call_ms`,
  `result_normalize_ms`, `ocr_wall_ms`, `capture_wait_ms`,
  `total_frame_pipeline_ms`, plus `det_ms`/`rec_ms` when RapidOCR exposes them
  (else `None`). `OCRRuntime.last_timings` carries per-call values; `_RapidOCREngine`
  records `last_raw_ms`/`last_normalize_ms`.
- Per-call CPU: `ocr_process_cpu_ms` + `ocr_effective_cpu_pct` (process CPU / wall).
- ORT threading via RapidOCR's own params (`EngineConfig.onnxruntime.intra_op_num_threads`
  / `inter_op_num_threads`) — no monkey-patching; `--ort-intra-threads`,
  `--ort-inter-threads`. `cv2.setNumThreads` via `--opencv-threads`.
  `validate_thread_count` accepts `-1` or `>=1` and rejects the rest
  (`invalid_thread_count`).
- Benchmark modes in `ocr_test.py`: `--image --repeat N` (static repeated,
  decode-once/init-once), `--live --no-ocr` (capture/decode/crop only, engine
  never initialized), and the existing live+OCR mode. `TimingStats` reports
  first/steady-avg/p50/p95/max, effective CPU, RSS start/peak/end, queue counters,
  and `[ocr-threads]` oversubscription info.

## Phase 2F.4 content-vs-contention attribution

- 2F.3 left an unexplained gap (static det ≈990 ms vs live det ≈1747 ms). 2F.4
  separates *content cost* from *live contention* using the identical ROI bytes.
- `ocr_test.py --debug-roi-copy PATH` saves exactly one live active-ROI PNG from
  the same RGBA passed to OCR (written after the timing-critical OCR sample, so
  encoding is excluded from OCR timing; no extra decode/capture). It prints
  `[ocr-debug] saved_roi seq=<N> path=<...> size=WxH`; invalid paths fail safely.
- `--replay-roi PATH` is an alias for `--image` so the saved ROI can be replayed
  with `--repeat` under the same thread config.
- `[ocr-correlation]` logs a bounded table per OCR frame (`sequence`,
  `ocr_wall_ms`, `det_ms`, `rec_ms`, `capture_wait_ms`, `capture_age_ms`) to check
  whether OCR spikes track expensive captures.

## Phase 2F.5 detector input geometry

- 2F.4 showed content dominates (offline/live det ratio ≈0.93) and the slowdown
  tracks detector input area: 2F.5 exposes RapidOCR's detector resize controls.
- `OCRConfig.det_limit_side_len` (default 736) and `det_limit_type` (default
  `min`) are explicit ClarifyDeck defaults, wired as `Det.limit_side_len` /
  `Det.limit_type` in `_rapidocr_params` (before `RapidOCR(params=...)`, no
  post-init mutation). `validate_det_limit_side_len` rejects `<= 0`;
  `validate_det_limit_type` accepts only `min`/`max`.
- `predict_det_geometry(width, height, limit_side_len, limit_type)` mirrors
  RapidOCR 3.9.2 `DetPreProcess.resize` (min/max ratio, multiples of 32) and
  powers the `[ocr-det-input]` diagnostic.
- CLI: `--det-limit-side-len`, `--det-limit-type`. Predicted geometry for
  1024x160 at `min`: 256→1632x256, 320→2048x320, 384→2464x384, 512→3264x512,
  736→4704x736. `limit_type=max` ignores `limit_side_len` in RapidOCR 3.9.2
  (uses 960/1500/2000 by long edge) and is not enabled by default.

## Phase 2F.6 Ctrl-C graceful shutdown

- `run_live` previously awaited `asyncio.sleep(duration)` with cleanup only after
  it, so SIGINT/CancelledError skipped producer/consumer/queue cleanup and printed
  a traceback.
- The live lifecycle is now `try/except CancelledError/finally`: on cancellation
  `_interrupted` is set, `CancelledError` is re-raised (semantics preserved), and
  the `finally` always runs `_shutdown_live(reason)`.
- `_shutdown_live` is the single **idempotent** path (`_shutdown_done` guard):
  STOPPING -> `producer.stop()` -> set stop event -> cancel the exact
  `_consumer_task` (suppressing only its own `CancelledError` via
  `task.cancelled()`) -> `queue.clear()` -> `runtime.close()` -> STOPPED ->
  `_print_summary()`. Summary is exception-guarded so very-early interruption
  cannot raise.
- CLI boundary: `main()` catches `KeyboardInterrupt`/`asyncio.CancelledError`,
  prints `[ocr] interrupted`, returns **130**. Normal completion returns the
  PASS/FAIL code; a manually interrupted run does not print PASS.

## Phase 2G OCR output stabilization

- `ocr/stabilizer.py` (dependency-free: no numpy/cv2/onnxruntime/rapidocr/capture)
  turns raw OCR lines into a stable text stream: confidence filter → conservative
  normalization → temporal consensus → duplicate suppression → stale timeout.
- `OCRStabilizer(min_line_confidence=0.70, consensus_required=2, history_size=3,
  stale_timeout_sec=2.0)`. A candidate is the ordered join (`"\n"`) of normalized
  non-empty eligible lines; missing confidence is **rejected** (never treated as
  1.0); `confidence >= threshold` is eligible. Normalization is NFC + outer strip
  + inner space/tab collapse + CRLF→LF; no translation/punctuation edits.
- Consensus = same normalized candidate in `consensus_required` of the most
  recent `history_size` eligible frames (exact match; bounded `deque`). Emits a
  `text` event only when the stable text differs from `last_emitted_text`;
  otherwise counts `duplicate_suppressed`. Empty frames do not clear
  immediately; a monotonic `stale_timeout_sec` with no eligible candidate emits
  exactly one `clear`, then no spam. `reset()` clears all state.
- `scripts/ocr_test.py` integration is **opt-in** (`--stable-output`, default
  off): `--min-line-confidence`, `--consensus-required`, `--history-size`,
  `--stale-timeout-sec`; invalid config → `config_error` exit 2. Prints
  `[ocr-stable] candidate=`, `consensus=N/M`, `emit=text|clear`, and
  `[ocr-stable] stats={...}` at shutdown. Raw `[ocr]` diagnostics are unchanged
  and the raw path is untouched when the flag is absent.

## Phase 2H change-gated OCR scheduling

- `capture/scheduler.py` adds `OCRChangeGate`, a thin adapter over the existing
  `ROIChangeDetector` (grid 48x12, threshold 0.006). Decision per consumed frame:
  first frame -> OCR; detector changed -> OCR; `force_interval_sec` elapsed ->
  OCR; otherwise skip. Detector exceptions fail **open** to OCR and increment
  `change_detector_errors`.
- Scheduling position: decode -> crop active ROI -> change check -> optional OCR
  -> stabilizer. Only the change check runs on skipped frames (no image re-encode).
- `ocr.stabilizer.tick(timestamp)` advances time for skipped frames without
  adding history, counting as empty, resetting consensus, or incrementing
  `raw_frames`. If the last real observation contained text the stable text is
  preserved (keep-alive); if the last observation was empty the stale timeout can
  complete and emit one clear. An unchanged subtitle therefore never stale-clears.
- `OCRChangeGate.reset()` (session start, explicit reset) and geometry-change
  detection both reset the detector baseline so the next frame always OCRs.
- CLI (opt-in): `--change-gate` (default OFF), `--force-ocr-interval-sec`
  (default 3.0, must be > 0). Prints `[ocr-scheduler] seq=… changed=… action=…
  reason=…` under `--debug` and `[ocr-scheduler] stats={…}` (incl.
  `ocr_skip_ratio`, `avg_change_check_ms`) at shutdown.

## Phase 2H.1 capture shutdown accounting

- H2 showed `capture_errors=1` at shutdown with no visible error. The only
  increment site is `CaptureProducer._run`'s `except Exception` →
  `queue.note_capture_error()`; the producer had a no-op logger in the
  diagnostic, so the reason was hidden.
- Attribution + fix in `capture/producer.py`:
  - `asyncio.CancelledError` → `capture_cancellations++` (never a capture error).
  - `Exception` while `_stop_requested` is set (capture in flight when stop was
    requested) → `capture_cancellations++`, `last_error_category="stop_during_capture"`,
    logged, **not** counted as `capture_errors`/`consecutive_failures`, no FAILED.
  - `Exception` while not stopping → unchanged genuine failure: `frames_failed++`,
    `consecutive_failures++`, `queue.note_capture_error()`,
    `last_error_category="capture_operation_error"`, failure threshold still 5.
  - Successful capture after stop is discarded (not published) and counted as
    `shutdown_discarded_frames`.
- New bounded counters in `status()`: `last_error_category`, `capture_cancellations`,
  `shutdown_discarded_frames` (plus `main.py`'s default status dict).
- `scripts/ocr_test.py` now passes a printing logger to `CaptureProducer` and
  prints `[capture-producer] state=… frames_failed=… capture_cancellations=…
  shutdown_discarded_frames=… last_error_category=… last_error=…` in the summary,
  so genuine failures are always visible. `capture_errors` now means genuine
  capture failures only.

## Phase 2H.2 post-change confirmation OCR

- H3 gap: a changed frame produced a new candidate at `consensus=1/2`, but the
  next frames were visually unchanged and got skipped, so the second consensus
  vote waited for the 3 s forced refresh — adding ~3 s of text latency.
- `OCRChangeGate` gains bounded confirmation scheduling:
  - `note_ocr_result(needs_confirmation)` is the narrow downstream feedback
    (no OCR objects cross the boundary; unexpected values fail open to arming).
  - Priority: `first_frame` > `change` > `confirmation` > `forced_refresh` >
    `unchanged`. A new change supersedes pending confirmation
    (`confirmation_superseded++`).
  - Bounded to `max_confirmation_attempts=1` per change event (no OCR-until-stable
    loop); `confirmation` clears on execute, supersede, reset, and ROI geometry
    change. Forced refresh remains the detector false-negative safety net.
- `OCRStabilizer.needs_confirmation` (public property) is True only when the last
  real observation has a candidate that is not yet stable **and** differs from the
  emitted text. Stable emits, duplicate already-stable text, and empty
  observations report False, so harmless visual noise does not trigger
  confirmation loops.
- Confirmation OCR is a real observation: it increments stabilizer `raw_frames`
  and can satisfy consensus normally. Skipped frames still do not.
- New counters: `ocr_trigger_confirmation`, `confirmation_armed`,
  `confirmation_superseded`. Gate OFF is unchanged (no scheduler output, no
  confirmation).

## Phase 2H.3 bounded confirmation retry

- C2 showed a short-lived auto-advance subtitle where two real observations
  differed slightly (an extra punctuation/ellipsis line), so exact-string
  consensus stayed at 1/2 and the budget (`max_confirmation_attempts=1`) was
  exhausted before a matching vote arrived.
- Budget raised to `DEFAULT_MAX_CONFIRMATION_ATTEMPTS = 2`, giving at most
  `change OCR + confirmation #1 + confirmation #2` (3 real observations) per
  detected change — still bounded, no OCR-until-stable loop.
- `OCRChangeGate` tracks `_confirmation_remaining` / `_confirmation_attempts_done`
  / `_confirmation_exhausted_counted`. `note_ocr_result(needs)` arms on the first
  vote (`confirmation_armed`), schedules a retry after a prior confirmation still
  needs consensus (`confirmation_retried`), and marks `confirmation_exhausted`
  once when the budget ends while consensus is still needed. A new change
  re-arms a fresh budget and supersedes the old one (`confirmation_superseded`).
- `SchedulerDecision.confirmation_remaining` is exposed and printed for
  confirmation decisions: `[ocr-scheduler] seq=N changed=False action=ocr
  reason=confirmation remaining=1`.
- Priority unchanged: `first_frame > change > confirmation > forced_refresh >
  unchanged`; forced refresh remains the long-tail safety net.

## Phase 2H H4 forced-refresh recovery hook

- H4 verifies the false-negative safety net: a real subtitle change that the
  detector misses must still be recovered by the forced refresh.
- Diagnostic-only `--debug-force-unchanged` (default OFF) makes `OCRChangeGate`
  report the scheduler-facing `changed` as False while the detector still runs
  (threshold/resolution untouched). `first_frame` still OCRs, `reason=change` is
  suppressed, and `forced_refresh` fires normally; a forced-refresh OCR may still
  arm bounded confirmation when the stabilizer needs another vote.
- Detector exceptions still fail open to OCR even under the override.
- `OCRChangeGate(force_unchanged=...)`; `SchedulerDecision.changed` reflects the
  effective (forced) value so `[ocr-scheduler] changed=False` is accurate.

## Phase 2H H5 change-detector fail-open

- H5 verifies the fail-open contract: a change-detector exception must never
  cause a skipped OCR. `OCRChangeGate.decide` catches detector exceptions at the
  call boundary, increments `change_detector_errors`, and emits a distinct
  `reason=detector_error` (priority `first_frame > detector_error > change >
  confirmation > forced_refresh > unchanged`). The OCR loop, queue, and
  `CaptureProducer` are unaffected; detector failures are not counted as
  `capture_errors` / `frames_failed` / `ocr_errors`.
- Diagnostic-only `--debug-force-change-detector-error` (default OFF) raises at
  the exact detector boundary so the fail-open path can be exercised on device
  without touching the frozen 48x12 / 0.006 detector. `first_frame` still OCRs.
- New counter `ocr_trigger_detector_error`; `change_detector_errors` unchanged.

## Phase 2H H6 ROI geometry reset boundary

- H6 makes the scheduler's ROI boundary explicit. `OCRChangeGate.decide` now
  accepts `roi_key` (the full authoritative pixel rect `(x, y, w, h)`); a change
  in that key — not just the size — resets the detector baseline, clears pending
  confirmation, clears the confirmation budget, and treats the next frame as
  `reason=first_frame` (immediate OCR, no comparison against the old ROI). Same
  geometry does not spuriously reset.
- `SchedulerDecision.roi_geometry_changed` / `.roi_geometry` expose the boundary;
  `--debug` prints `[ocr-scheduler] roi_geometry_changed new=(x,y,w,h) reset=1`.
- Diagnostic-only `--debug-switch-roi-after-sec N` + `--debug-switch-roi x,y,w,h`
  (default OFF) switch the diagnostic process's ROI via a `_StaticResolver`,
  routing through the same production reset path and never mutating persisted
  user config. It prints `[ocr-scheduler] debug_roi_switch roi=(...)`.
- Detector threshold/grid, force interval, confirmation budget, OCR settings, and
  the stabilizer are untouched.

## Phase 2H H7 explicit session reset boundary

- H7 verifies the explicit reset contract using the existing production
  `OCRChangeGate.reset()` (no second reset implementation). `reset()` clears the
  detector baseline, pending confirmation, confirmation budget, ROI geometry,
  per-session first-frame state, and forced-refresh timing, so the next valid
  frame OCRs as a fresh `reason=first_frame`. Cumulative counters **persist**
  across reset so the boundary is observable (`ocr_trigger_first` increases).
- Diagnostic-only `--debug-reset-scheduler-after-sec N` (default OFF) calls the
  production `reset()` once after N seconds, printing
  `[ocr-scheduler] debug_scheduler_reset reset=1 ocr_trigger_first_before=N`; it
  does not restart the OCR engine, CaptureProducer, or mutate persisted config.
- Reset does not alter detector 48x12 / 0.006, force interval, or confirmation
  budget defaults. Stabilizer consensus/history/stale rules are untouched.
- `run_live` continues to call `reset()` at session start.

## Phase 2H closure — change-gated OCR scheduling

Purpose: change detection is an **optimization only**. The production pipeline is
`CaptureFrame -> authoritative Recognition ROI crop -> optional ROI change gate ->
OCR when required -> OCR stabilizer`; OCR correctness remains fully testable with
the gate OFF.

- **Scheduler priority (implemented):** `first_frame > detector_error > change >
  confirmation > forced_refresh > unchanged`.
- **Forced refresh** (`--force-ocr-interval-sec 3.0`) is the false-negative safety
  net: a persistent real change that the detector misses is still OCR'd and can
  stabilize/emit. Correctness never depends on `changed=True`.
- **Detector failure fails open:** exceptions increment `change_detector_errors`,
  emit `reason=detector_error`, and OCR; they are never counted as
  `capture_errors` / `frames_failed` / `ocr_errors`.
- **Bounded confirmation:** `max_confirmation_attempts=2` → at most `change OCR +
  confirmation #1 + confirmation #2` per detected state (no unbounded loop); a
  newer change supersedes the old budget with a fresh one.
- **ROI geometry boundary:** the gate keys on the full pixel rect `(x, y, w, h)`;
  a change clears the detector baseline + confirmation and forces the next frame
  to `first_frame`.
- **Explicit reset:** `OCRChangeGate.reset()` clears transient state (baseline,
  confirmation, geometry, first-frame, force timing) while preserving counters.
- **Stabilizer separation:** skipped frames call `tick()` only — no fake empty
  OCR, no history/`raw_frames` change; an unchanged subtitle never stale-clears.
- **Capture/queue safety:** `LatestFrameQueue` cap 1 newest-wins, no OCR overlap,
  bounded producer stop, genuine `capture_errors` only (shutdown discard /
  cancellation accounted separately, Phase 2H.1).
- **Engine lifetime:** `engine_init_count == 1` per session.
- **Diagnostic-only hooks (default OFF, no effect when absent):**
  `--debug-force-unchanged`, `--debug-force-change-detector-error`,
  `--debug-switch-roi-after-sec` / `--debug-switch-roi`,
  `--debug-reset-scheduler-after-sec`. They never mutate persisted Recognition ROI.

Frozen validated defaults (code constants): full-frame `(32,20)`/`0.005`; ROI
detector `(48,12)`/`0.006`; `consensus_required=2`, `history_size=3`,
`stale_timeout_sec=2.0`, `min_line_confidence=0.70`, `max_confirmation_attempts=2`,
`force_ocr_interval_sec=3.0`; diagnostic FPS default 1; gate + stabilizer opt-in
(OFF). The validated device invocation additionally passes `--ort-intra-threads 2
--ort-inter-threads 1 --opencv-threads 1 --det-limit-type min
--det-limit-side-len 256` (these remain explicit CLI values, not code defaults).

True-device gates: H1, H2, 2H.1, H3, 2H.2→2H.3, H4, H5, H6, H7 all PASS/CLOSED.
Local suite: 540 tests, 0 failures, 2 platform skips; `pnpm build` PASS.

## Phase 2I.1 stable text event transport

- Transport foundation for the OCR worker's stabilizer output; no rendering,
  translation, or OCR UI.
- **Protocol v1** (`ocr/transport.py`, pure stdlib): newline-delimited JSON
  envelope
  `{"v":1,"type":"stable_text","event_seq":N,"kind":"text|clear","text":...,"confidence":...,"source_seq":...,"timestamp_monotonic":...}`.
  `event_seq` is a per-worker-session counter starting at 1 and incrementing by 1
  per emitted stable event; `source_seq` remains the capture/OCR frame sequence.
  Strict validation rejects invalid JSON/non-object, unknown `v`/`type`/`kind`,
  `event_seq<=0`, non-finite/negative timestamps, empty/invalid text, invalid
  confidence, invalid `source_seq`, malformed clear events, and lines > 64 KiB.
- **Backend receiver** (`backend/ocr_transport.py`, pure stdlib):
  `OCRTransportReceiver` parses one line, enforces per-session `event_seq`
  monotonicity (duplicates/out-of-order rejected without overwriting last good
  state), and keeps a bounded `StableOCRState` (worker_session_id, last_event_seq,
  kind, text, confidence, source_seq, timestamp_monotonic). `begin_session()`
  resets the boundary to 0 with a fresh opaque uuid4 session id. Counters:
  `transport_messages_received/rejected/out_of_order/text_events/clear_events`,
  `last_transport_error`. Malformed input never becomes `capture_errors`/
  `ocr_errors`.
- **Backend RPCs** (`main.py`): read-only `get_ocr_transport_status()` and
  `get_latest_stable_text()`. They never start OCR.
- **Diagnostic emission** (`scripts/ocr_test.py`): opt-in `--emit-stable-jsonl`
  (default OFF). When on, machine JSONL goes to **stdout** and all diagnostics are
  redirected to **stderr** (`contextlib.redirect_stdout(sys.stderr)`); the
  stabilizer emits exactly one flushed line per stable event via
  `envelope_from_event`/`encode_envelope`. Emission failures never break the loop.
- **Native dependency isolation:** `main.py` and the transport modules import no
  numpy/cv2/onnxruntime/rapidocr/omegaconf/antlr4 (asserted by a subprocess
  import-safety test). No OCR auto-start, no daemon, no second singleton.

## Phase 2I.2 backend OCR worker lifecycle

- `scripts/ocr_worker.py` is the production child entrypoint: runs the validated
  pipeline via `ocr_test.OCRDiagnostic` (capture -> ROI -> optional gate ->
  PP-OCRv6 -> stabilizer), emits protocol v1 JSONL on **stdout**, diagnostics on
  **stderr**. Frozen baseline defaults: FPS 1, ORT intra 2 / inter 1, OpenCV 1,
  `Det.limit_type=min`, `Det.limit_side_len=256`, stable output ON, change gate
  **opt-in (OFF)**. A `--parent-pid` watchdog exits the worker (SIGINT) if the
  owning backend disappears.
- `backend/ocr_worker.py` (`OCRWorkerManager`, pure stdlib) owns exactly one
  child: state machine `STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`
  (or `FAILED`). Explicit start only (idempotent while STARTING/RUNNING);
  unexpected exit → `FAILED` with the exit code, **no auto-restart**. Status query
  never spawns.
- Spawn policy: system python from `overlay_manager.resolve_python3` (never a
  PluginLoader/decky interpreter), `cwd=plugin root`, `PYTHONNOUSERSITE=1`,
  `PYTHONPATH=plugin root`, `stdin=DEVNULL`, `stdout/stderr=PIPE`, `shell=False`,
  no `setsid`/`start_new_session`. `--parent-pid` is passed to the child.
- Each start calls `receiver.begin_session()` (fresh uuid4 session id, resets
  `last_event_seq`/counters); the child's JSONL starts at `event_seq=1`. The
  stdout reader feeds `OCRTransportReceiver` with the 64 KiB line bound; malformed
  / oversized / out-of-order lines are counted and never overwrite the last good
  state or crash the backend. stderr is drained into a bounded 200-line tail.
- Stop: bounded escalation on the exact owned PID (`wait` → SIGINT → SIGTERM →
  SIGKILL), then joins reader/monitor threads; idempotent when already STOPPED.
  `RLock` guards state so `start()`/`stop()` may call `status()` reentrantly.
- RPCs (`main.py`): `start_ocr_worker(change_gate=False)`, `stop_ocr_worker()`,
  `get_ocr_worker_status()`; read-only `get_ocr_transport_status()` /
  `get_latest_stable_text()` remain. Start requires the backend **leader** and is
  rejected with `capture_conflict` while a `CaptureProducer` is RUNNING (single
  capture owner). `_unload`/`_uninstall` stop the worker.
- Native dependency isolation preserved: `main.py`, `backend/*` import no
  numpy/cv2/onnxruntime/rapidocr/omegaconf/antlr4 (subprocess import-safety gate).

### Phase 2I.2.1 explicit-stop latency fix

- Device D1 measured `stop_elapsed_sec≈3.666`: `_terminate` did an unconditional
  `wait(timeout)` **before** signalling, adding the full cooperative timeout to
  every normal stop of a live long-running worker.
- New order: `poll()` (skip if already exited) → `SIGINT` exact PID immediately →
  bounded wait → `SIGTERM` → bounded wait → `SIGKILL` last resort. No pre-signal
  wait; an already-exited child is never signalled.
- `stop()` now joins the reader/monitor threads **outside** the `RLock` so the
  monitor can complete its own locked section (previously the join-within-lock
  could leave the monitor alive until the join timed out).
- Intentional SIGINT exit code 130 remains `STOPPED`, not `FAILED`; unexpected
  exit still `FAILED` with no auto-restart. Local suite time for the manager
  tests dropped 10.2s → 2.2s, confirming the removed wait.

### Phase 2I.2.2 parent-death / orphan-worker safety

- Device D4 failed: after the backend did an abrupt `os._exit(0)`, the worker
  stayed alive (`worker_still_alive`, `orphan OCR worker still alive`).
- Root cause: the userspace watchdog used `/proc/<pid>` existence + `kill(pid, 0)`.
  An exited-but-not-reaped parent is a **zombie** that still has `/proc/<pid>` and
  accepts signal 0, so liveness never reported death (PID reuse would be a similar
  false-positive).
- Fix: `backend/parent_death.py` (pure stdlib) arms the kernel
  `prctl(PR_SET_PDEATHSIG, SIGINT)` via libc, so the kernel signals the worker when
  the parent thread dies — independent of zombie state or reaping. After arming it
  verifies `os.getppid() == expected` and fails closed on the
  parent-died-before-arm race. `parent_changed()` compares the parent
  **relationship**, never bare PID existence.
- `scripts/ocr_worker.py` arms protection **early** — right after arg parsing,
  before `activate_plugin_ocr_runtime`/`OCRRuntime` — with `--parent-pid` defaulting
  to `None` (standalone runs skip; an explicit pid `<= 1` or a changed parent fails
  closed, exit 2). The watchdog is retained as a secondary defense but now checks
  `getppid()`; the old `_pid_alive` PID-existence loop is removed.
- `SIGINT` keeps the validated clean-shutdown path (exit 130). No daemon, no
  `setsid`, no process-group signals; the worker remains a direct child.
- Tests: `scripts/test_parent_death.py` (helper units + a **real Linux subprocess
  test** that spawns a child with PDEATHSIG armed, has the parent `os._exit(0)`, and
  asserts the child disappears on its own; Linux-only, skipped on Windows).
  True-device D4 retest still pending.

### Phase 2I.2.3 production launch default path resolution

- Device D6 preflight showed the production RPC path could emit
  `--model-dir None` (and no `--roi-config`): `ClarifyDeckEngine.start_ocr_worker`
  calls `manager.start(fps, change_gate)` without `model_dir`/`roi_config`, and
  `build_command` serialized a `None` path.
- `OCRWorkerManager` now resolves canonical defaults itself:
  - **model dir:** `model_dir or <plugin_root>/models/ppocrv6`, validated before
    spawn — the directory must exist and contain the required files (read from
    `manifest.json` `files`, falling back to `PP-OCRv6_det_small.onnx` /
    `PP-OCRv6_rec_small.onnx`). Missing dir/files → `OCRWorkerError("model_missing")`
    with **no child spawned**; no runtime download.
  - **ROI config:** `roi_config or <settings_root>/recognition_roi.json`, where
    `settings_root` is supplied by `main.py` as `roi_config_path().parent` (the
    same Decky settings dir the Recognition ROI system owns). No second precedence
    system; a missing file falls back to the existing `ROIConfigStore` defaults.
- `build_command` never serializes `None` into a path argument; `start()` passes
  `model_dir`/`roi_config` through unchanged (validation happens before `Popen`).
- Tests (`ProductionLaunchPathTest`) use temp plugin/settings roots (no `/home/deck`
  dependency) and assert the exact production RPC argv contains a real model path
  and canonical ROI config path with no `"None"`.
- Device D6 retest pending.

## Phase 2I.2 closure — backend OCR worker lifecycle

Ownership: only the backend **leader** starts exactly one OCR child via an explicit
`start_ocr_worker()`; the worker runs the frozen pipeline
(capture → authoritative Recognition ROI → optional change gate → PP-OCRv6 →
stabilizer) and emits `stable_text` protocol v1 JSONL on stdout / diagnostics on
stderr; the backend `OCRTransportReceiver` holds the latest stable state. No
rendering, no translation, no frontend subtitle presentation.

- **State machine:** `STOPPED → STARTING → RUNNING → STOPPING → STOPPED`; an
  unexpected exit goes `RUNNING → FAILED` with `pid=None` + `exit_code` +
  `last_error`, and there is **no auto-restart** (`FAILED → STARTING` only via an
  explicit new `start()`). `start()` is idempotent while STARTING/RUNNING (same
  PID/session, no second spawn).
- **Fresh session:** every real start calls `receiver.begin_session()` → new
  `worker_session_id`, `last_event_seq=0`, counters reset; the child's first event
  is `event_seq=1`. Session identity is never inferred from PID alone.
- **Safe spawn:** `overlay_manager.resolve_python3` (PluginLoader/decky rejected),
  `cwd=plugin root`, `PYTHONNOUSERSITE=1`, `PYTHONPATH=plugin root`,
  `stdin=DEVNULL`, `stdout/stderr=PIPE`, `shell=False`, no setsid/`start_new_session`.
- **Production default paths:** model dir resolves to
  `<plugin_root>/models/ppocrv6` (validated against the manifest's files before
  spawn; `model_missing` + no child on failure, no runtime download); ROI config
  resolves to `<settings_root>/recognition_roi.json` (the same settings root the
  Recognition ROI system owns; missing file → existing `ROIConfigStore` defaults).
  No argv path is ever the literal `None`.
- **Transport:** stdout reader continuously feeds `OCRTransportReceiver` with the
  64 KiB line bound; stderr drains into a bounded 200-line tail; malformed /
  oversized / out-of-order lines are counted and preserve last good state and never
  become `capture_errors`/`ocr_errors`.
- **Stop (2I.2.1):** `stop_requested → SIGINT exact PID immediately → bounded wait
  → SIGTERM → bounded wait → SIGKILL last resort → reader cleanup → STOPPED`. No
  unconditional pre-signal wait; already-exited children are never signalled;
  SIGINT exit 130 is `STOPPED`, not `FAILED`. Device: stop ≈3.666 s → ≈0.615 s.
- **Parent death (2I.2.2):** worker arms `prctl(PR_SET_PDEATHSIG, SIGINT)` early
  (before OCR init), then verifies `os.getppid()` still matches the expected
  parent; fail closed on invalid pid / changed parent / setup failure. Secondary
  watchdog uses the parent relationship, never bare PID existence. Device D4:
  abrupt parent `os._exit(0)` → worker terminates on its own.
- **Leader/capture guards:** `start_ocr_worker` rejects `not_leader` and
  `capture_conflict` (backend `CaptureProducer` RUNNING) before `manager.start()`;
  no second capture loop runs silently.
- **No auto-start:** `get_ocr_transport_status` / `get_latest_stable_text` never
  create the manager; `get_ocr_worker_status` may lazy-create the pure-stdlib
  manager but never calls `start()`; `stop_ocr_worker` never starts. `_unload` /
  `_uninstall` stop the worker explicitly.
- **Native isolation:** `main.py` + `backend/*` import no
  rapidocr/onnxruntime/numpy/cv2/omegaconf/antlr4; native OCR loads only in the
  child.
- **Frozen worker defaults:** FPS 1, ORT intra 2 / inter 1, OpenCV 1,
  `Det.limit_type=min`, `Det.limit_side_len=256`, stable output ON, change gate
  **OFF**; force interval 3.0, min line confidence 0.70, consensus 2, history 3,
  stale 2.0, `max_confirmation_attempts=2`. Recognition ROI remains frozen from
  Phase 2E.2.
- **Device gates:** D1, 2I.2.1, D2, D3, D4, 2I.2.2, D5, D6, 2I.2.3 all PASS/CLOSED
  (D6-B: production-default start → RUNNING → live stable event → rejected=0 →
  out_of_order=0 → explicit stop → STOPPED, child gone, stop≈0.615 s, exit=0).
- **Local suite:** 634 tests, 0 failures, 3 platform skips (1 Linux-only
  parent-death subprocess test on Windows + 2 flock); `pnpm build` PASS.

## Phase 2I.3 QAM diagnostic OCR control

- The Decky QAM gains an explicit **OCR Diagnostic** panel
  (`src/components/OCRDiagnostic.tsx`, pure logic in `src/ocrDiagnostic.ts`,
  mounted from `src/index.tsx`). The backend remains the single source of truth.
- Controls: worker state (+PID), Start OCR, Stop OCR, a "Use change-gated OCR"
  toggle (default **OFF**, disabled while RUNNING and applied only on the next
  explicit start), transport session/event status, latest stable text, last
  confidence/source sequence, and last error. No translation, no persistent
  overlay, no frontend process spawn.
- RPCs reused only (no new lifecycle APIs): `start_ocr_worker(change_gate)`,
  `stop_ocr_worker()`, `get_ocr_worker_status()`, `get_latest_stable_text()`.
  Polling is read-only at ~1 Hz (`POLL_INTERVAL_MS = 1000`), and the timer is
  cleared on unmount; mount/QAM-open/polling never call `start_ocr_worker`.
- Start/Stop are explicit and de-duplicated: the handler guards plus a `busy`
  flag disable repeat calls; structured backend errors (`not_leader`,
  `capture_conflict`, `ocr_worker_unavailable`, `model_missing`,
  `forbidden_interpreter`) map to concise messages with **no auto-retry**.
- Stable text is rendered exactly as returned (`white-space: pre-wrap`); `clear`
  shows `(no stable text)`, no event shows `(waiting for stable text)`.
  Frontend event identity is `(worker_session_id, last_event_seq)` so repeated
  polls are not new events and a new session resets identity.
- QAM close/reopen does not start or stop the worker; on reopen the RUNNING state
  and latest text are restored from the backend.
- Frontend harness: `scripts/test_frontend_ocr_diagnostic.mjs` (55 checks after
  Phase 2I.3.2) run via `pnpm test` — pure-logic behavior + static guards
  (mount/effect must not call start/stop; polling read-only + timer cleanup; no
  translation/overlay/spawn).
- No production backend changes (RPCs already existed).

### Phase 2I.3.1 shared transport receiver wiring fix

- **Trigger:** Steam Deck D2 showed `Event #1 / Received 1 / Rejected 0` in the QAM
  worker status while `latest_stable_text` stayed `(waiting for stable text)`.
- **Root cause:** the engine and the manager owned two independent
  `OCRTransportReceiver` instances. `ClarifyDeckEngine._ocr_worker_manager()` built
  `OCRWorkerManager` without a receiver, so `OCRWorkerManager.start()` allocated its
  own via `transport_factory`; the stdout reader fed the manager's receiver while
  `get_latest_stable_text()` / `get_ocr_transport_status()` read the untouched
  engine receiver (`event_seq=0`, no text).
- **Fix:** the engine remains the single receiver owner. `_ocr_worker_manager()`
  obtains `self._transport_receiver()` and injects it as
  `OCRWorkerManager(transport_receiver=receiver)`. The manager reuses that exact
  object and never allocates a competing production receiver; if transport is
  unavailable it returns the existing structured `ocr_worker_unavailable` path.
- **Manager standalone behavior:** with no injected receiver the manager creates
  exactly one receiver at `__init__` and reuses it across real starts.
- **Fresh logical session preserved:** each real start still calls
  `begin_session()` (new uuid4 `worker_session_id`, `last_event_seq=0`), and now
  also explicitly resets the transport counters — required because the receiver
  object is reused instead of reallocated per start. Repeated start while
  STARTING/RUNNING stays idempotent (no `begin_session()`, same PID/session).
- **Tests:** `scripts/test_backend_ocr_worker.py` gains `SharedReceiverWiringTest`
  and `DirectIntegrationRegressionTest` (engine-owned receiver identity, shared
  session/event state, `latest_stable_text` after a manager-path event, single
  receiver construction, begin_session call counts, restart/reuse, counter reset).
- **Device D2 retest PASS** (shared-receiver fix closed): explicit Start OCR →
  RUNNING → one worker PID → Event #1 → stable text + confidence/source_seq visible
  in QAM → rejected=0 / out_of_order=0; QAM close/reopen preserves PID/session/text
  (D3); explicit Stop → STOPPED with no worker process (D4).

### Phase 2I.3.2 temporary capture diagnostic controls

- Temporary QAM **Capture Diagnostic** controls were added to
  `src/components/OCRDiagnostic.tsx` solely to make the Phase 2I.3 D5 device test
  possible without GamepadUI DevTools: prove that `Start OCR` returns the backend
  `capture_conflict` guard while a `CaptureProducer` is RUNNING.
- Reuses existing RPCs only — `capture_producer_start(1.0)`,
  `capture_producer_stop()`, `capture_producer_status()`. No new backend API and
  **no backend lifecycle changes**; `main.py`, `backend/*`, the capture producer,
  OCR worker/scheduler, ROI, transport, overlay, and translation are untouched.
- **Explicit only:** lifecycle changes come from button presses. The controls are
  never started on component mount / QAM open / polling, are never stopped on QAM
  close, and have no auto-retry. Cadence is the fixed diagnostic **1 FPS**.
- Capture status is polled read-only on the existing 1 Hz timer (one timer
  services both OCR and capture status); it shows state, `target_fps`,
  `frames_succeeded`, `frames_failed`, and `last_error`.
- **OCR Start is not frontend-blocked** by a RUNNING capture producer: the existing
  handler still issues the explicit request so the backend `capture_conflict` path
  stays testable; the mapped error is surfaced from the RPC result.
- Marked in code/docs as a **temporary Phase 2I.3 diagnostic control**; it was not
  a product feature and was removed in Post-2I.3 Cleanup C1 (kept through D5 and
  the closure review).
- Frontend harness extended (`scripts/test_frontend_ocr_diagnostic.mjs`, 55 checks).
- **Device D5 PASS:** `CaptureProducer RUNNING → explicit Start OCR →` backend
  `capture_conflict` surfaced in QAM → OCR worker remains STOPPED with no
  `scripts/ocr_worker.py` process → CaptureProducer remains RUNNING → explicit
  Stop Capture Diagnostic → CaptureProducer STOPPED.

### Phase 2I.3 closure

- **Status: Phase 2I.3 — QAM Diagnostic OCR Control + Live Stable Text — PASS /
  CLOSED.**
- Device gates D1–D5 all PASS: no OCR/capture auto-start on QAM open or polling;
  explicit Start → RUNNING → live stable text; QAM close/reopen preserves backend
  worker PID/session/text; explicit Stop → exact-PID exit; backend
  `capture_conflict` guard proven from the QAM surface.
- Frozen contracts verified unchanged: explicit QAM Start/Stop only; Change Gate
  default **OFF** (next-start only, disabled while RUNNING/STARTING); single 1 Hz
  read-only poll timer cleared on unmount; backend source of truth; no translation,
  no persistent overlay, no frontend OCR/process spawn; structured error mapping
  with no auto-retry; `capture_conflict` originates from the backend Start OCR
  result, never synthesized in the frontend.
- Shared receiver (Phase 2I.3.1) and backend worker lifecycle (Phase 2I.2) remain
  intact: one engine-owned `OCRTransportReceiver`; fresh `begin_session()` per real
  start (new session id, `event_seq`/counters reset); idempotent repeated start;
  no auto-restart; parent-death safety; production default model/ROI paths; native
  import isolation.
- Local regression at closure: Python 646 tests / 0 failures / 3 platform skips;
  frontend harness 55 checks PASS; `pnpm build` PASS; import-safety OK.
- The temporary Capture Diagnostic subsection (Phase 2I.3.2) was kept through the
  closure review and removed in Post-2I.3 Cleanup C1. Cleanup candidates are
  recorded in the separately reviewed cleanup phase; long-term safety regression
  tests are not disposable.

### Post-2I.3 Cleanup C1 — temporary capture diagnostic UI removal

- Status: **PASS / CLOSED.**
- Removed the temporary Phase 2I.3.2 QAM Capture Diagnostic UI after D5 device
  PASS: the `Capture Diagnostic (Temporary)` panel, its Start/Stop buttons, and
  capture status display.
- Removed its frontend-only state/helpers/RPC bindings/tests
  (`src/components/OCRDiagnostic.tsx`, `src/ocrDiagnostic.ts`,
  `scripts/test_frontend_ocr_diagnostic.mjs`). The OCR Diagnostic poll loop again
  reads only `get_ocr_worker_status()` and `get_latest_stable_text()`.
- Backend CaptureProducer RPCs (`capture_producer_start/stop/status/reset`) and the
  backend `capture_conflict` guard were intentionally retained; `main.py` and
  `backend/*` are unchanged. OCR error mapping for `capture_conflict` remains
  supported, with generic wording "Stop the backend capture producer before
  starting OCR." (no longer referencing the removed diagnostic UI).
- OCR Diagnostic remains explicit Start/Stop, Change Gate default **OFF**, one
  1 Hz read-only poll cleared on unmount, backend source of truth.
- No translation, persistent overlay, renderer, OCR scheduling, transport, ROI, or
  backend lifecycle behavior changed.
- Regression after C1: Python 646 tests / 0 failures / 3 platform skips; frontend
  harness 47 checks PASS; `pnpm build` PASS; import-safety OK.

### Post-2I.3 Cleanup C2 — legacy frontend overlay/debug path removal

- Status: **PASS / CLOSED.**
- Removed the unreachable hard-false legacy React subtitle rendering branch
  (`ENABLE_LEGACY_SUBTITLE_OVERLAY`), including its subtitle JSX and
  `subtitleBoxStyle`.
- Removed the legacy **Subtitle color** / **Subtitle size** QAM controls and their
  dead plumbing (`SubtitleColor`, `globalTextColor`, `globalFontSize`,
  `settingsEvents`, `setGlobalTextColor`, `setGlobalFontSize`, `useTextColor`,
  `useFontSize`), which only mutated the disabled React caption path.
- Removed the disabled notification keepalive workaround
  (`ENABLE_NOTIFICATION_KEEPALIVE`, `globalToastEnabled`, `setGlobalToastEnabled`,
  `useToastEnabled`, `mountOverlayKeepAlive`, `disposeToast`) and its obsolete
  **In-game overlay** / **Keep overlay visible** QAM control.
- Removed obsolete frontend debug probes (`ENABLE_DEBUG_PROBES`, `overlayProbeStyle`,
  `overlayBadgeStyle`, `CD probe` / `CD overlay`) and the historical raw DOM
  `CD raw` probe injected by `mountOverlay()`; the real region-preview container's
  re-append keepalive was preserved.
- QAM-visible Recognition ROI/region preview retained (still `qamVisible`-gated,
  geometry/scaling unchanged). Active backend **Persistent Game Overlay
  (Experimental)** control/status (`set_overlay_enabled`) retained unchanged.
- No backend `OverlayManager`/renderer, OCR, capture, ROI, worker, transport, or
  lifecycle behavior changed. No translation or final-subtitle styling introduced.
- The static guard in `scripts/test_overlay_safety.py` and the frontend harness
  (`scripts/test_frontend_ocr_diagnostic.mjs`, now 56 checks) were updated to
  assert the removed paths stay gone while the active paths remain.
- Regression after C2: Python 646 tests / 0 failures / 3 platform skips; frontend
  harness 56 checks PASS; `pnpm build` PASS; import-safety OK.

### Post-2I.3 cleanup closure

- **Status: PASS / CLOSED.**
- Completed: C1 removed the temporary Phase 2I.3.2 Capture Diagnostic frontend
  controls; C2 removed the unreachable legacy React subtitle / notification
  keepalive / frontend debug-probe paths. The accepted C1+C2 work was checkpointed
  locally at commit `055c27f` ("cleanup: close post-2i3 frontend diagnostics").
- Retained intentionally (evidence-based probe audit):
  - `scripts/roi_test.py` — imported by `test_roi_config_harness.py` and
    `test_subtitle_band.py`; live/mock ROI + band diagnostics.
  - `scripts/capture_producer_rpc_test.py` — loaded by `test_capture_producer_rpc.py`;
    live-backend producer RPC harness.
  - `scripts/gamescope_capture_test.py` — loaded by `test_capture.py`; bounded
    one-shot Gamescope base-plane capture / probe isolation tool.
  - `scripts/change_detection_test.py` — imported by `test_change_detector.py`;
    `--live` real-Gamescope change-detection diagnostic.
  - `scripts/capture_producer_test.py` — bounded live/mock producer diagnostic
    (slow-consumer, `--no-consume`, device timing) not reproducible by unit tests.
  - `scripts/capture_queue_test.py` — bounded live/mock latest-frame-queue
    diagnostic (consumer delay, debug copy).
  - `scripts/overlay_ipc_test.py` — manual renderer/IPC isolation (show/update/
    UTF-8/multiline/hide/shutdown); retained for renderer troubleshooting and the
    future persistent-subtitle work.
- Deferred cleanup candidates (audited, not removed): `WORKER_STATES` in
  `src/ocrDiagnostic.ts` is dead-proven (zero references); `eventIdentity` in
  `src/ocrDiagnostic.ts` is test-only. Intentionally exposed diagnostic RPCs
  (`capture_producer_reset`, `capture_test_base_plane`, `capture_frame_test`,
  `capture_probe`) are retained as public interfaces, not dead.
- Long-term regression contracts retained: no auto-start; exact-PID stop;
  parent-death; no auto-restart; shared receiver/session reset; ROI precedence;
  queue cap1 newest-wins; `capture_conflict`; native import safety.
- No translation or final persistent subtitle integration was started.

## Phase 2C.2 Wayland environment

- The live Decky backend (frozen loader) may not inherit `XDG_RUNTIME_DIR`, so
  `wl_display_connect("gamescope-0")` fails there while an interactive shell
  works.
- `capture/gamescope_capture.py` now resolves the runtime dir explicitly
  (`CLARIFYDECK_WAYLAND_RUNTIME_DIR` → valid `XDG_RUNTIME_DIR` →
  `/run/user/<uid>`, never hard-coded) and the Gamescope display
  (`GAMESCOPE_WAYLAND_DISPLAY` → constructor override → `WAYLAND_DISPLAY` if it
  names gamescope → `gamescope-0`).
- Connection uses `wl_display_connect_to_fd` on an explicitly connected AF_UNIX
  socket (`<runtime>/<display>`), avoiding dependence on libwayland's global
  environment lookup. No global `os.environ` mutation.
- Structured errors: `wayland_runtime_dir_unavailable`,
  `gamescope_wayland_socket_missing`, `gamescope_wayland_connect_failed`; the
  connect diagnostic logs display/runtime/socket/exists.

## Phase 2C capture producer

- `CaptureProducer` (`capture/producer.py`): explicit START/STOP state machine
  (`STOPPED/STARTING/RUNNING/STOPPING/FAILED`), default 1.0 FPS (clamped
  0.2–2.0), deadline-based cadence via `time.monotonic()`, strictly sequential
  captures (no overlap), `LatestFrameQueue.put_latest` integration.
- One failed capture increments counters and continues; `max_consecutive_failures`
  (default 5) moves the producer to `FAILED` with no automatic restart.
- Backend leader only; started only via explicit RPC
  (`capture_producer_start/stop/status/reset`). Never starts at boot/reload.
- `stop()` is bounded (3 s); backend `_unload`/`_uninstall` call
  `shutdown_capture()` which stops the producer and clears the queue.
- Capture runs via `asyncio.to_thread` so the event loop stays responsive; each
  capture uses its own Wayland client, and only one capture is ever in flight.
- Diagnostic: `scripts/capture_producer_test.py` (bounded, `--mock` supported).

## Phase 2B in-memory frames

- `CaptureFrame` (`capture/frame.py`) owns its PNG bytes; dimensions are derived
  from the bytes, so downstream consumers never depend on the transient Gamescope
  screenshot path (Steam may consume/remove it shortly after capture).
- `GamescopeCapture.capture_frame()` writes to a unique internal source path
  (`$XDG_RUNTIME_DIR/clarifydeck/capture/capture-<pid>-<seq>-<rand>.png`), reads
  the bytes once, validates them, builds the frame, and unlinks the source
  tolerantly (`_safe_unlink`). `--debug-copy` writes a durable copy from the
  in-memory bytes.
- `LatestFrameQueue` (`capture/latest_frame_queue.py`) is an asyncio mailbox with
  capacity exactly 1: newest wins, stale pending frames are replaced, the
  producer never blocks, and a consumer-owned frame is never mutated. Counters:
  produced/replaced/consumed/pending/cleared/capture_errors/max_pending.
- No capture producer runs at boot; one-shot diagnostics are explicit
  (`--frame`, `capture_frame_test` RPC, `scripts/capture_queue_test.py`).

## Phase 2A capture isolation

- New isolated module `capture/gamescope_capture.py` implements a minimal
  `gamescope_control` Wayland client via ctypes + `libwayland-client`.
- Uses `take_screenshot` with `base_plane_only` (type 1) so Steam UI, QAM,
  mangoapp and ClarifyDeck's own overlay are excluded from OCR input.
- Single-shot only: no loop, no OCR, never started at plugin boot.
- Fail-closed: `gamescope_socket_not_found`, `gamescope_control_not_found`,
  `gamescope_protocol_incompatible`, `base_plane_capture_unsupported`,
  `capture_failed`, `capture_timeout`, `invalid_frame`. It never silently falls
  back to `full_composition`.
- `full_composition` exists only as an explicit debug mode for comparison.
- Output defaults to `$XDG_RUNTIME_DIR/clarifydeck/capture-test/`.
- Manual paths: `python3 -m capture.gamescope_capture --mode base_plane_only`,
  `scripts/gamescope_capture_test.py`, and the backend RPC
  `capture_test_base_plane` (one shot, 15 s subprocess timeout, no overlay/OCR).

## Phase 1C.2 safety invariants

- Persistent external overlay is **OFF by default**; `_main()` only creates the
  `OverlayManager` object and never spawns the renderer.
- Only one backend is `role=leader` (kernel flock `/run/clarifydeck/backend-leader.lock`).
  Standby backends serve RPC/status but run no capture/OCR/overlay.
- Only one renderer can exist, guarded by a global flock
  (`$XDG_RUNTIME_DIR/clarifydeck/renderer.lock`) acquired **before any X11 call**.
- `update`/`hide` never start or restart the renderer; they are no-ops unless the
  overlay is explicitly enabled.
- No automatic restart: a dead renderer becomes `FAILED` until the user re-enables.
- The renderer is a direct child (no `setsid`), stopped by exact PID only, with a
  `--parent-pid` watchdog plus best-effort `PR_SET_PDEATHSIG`.
- Runtime dir is owned by `deck:deck`, mode `0700`; the renderer is spawned as the
  deck user via `Popen(user="deck")` (sudo only as a fallback).

## Phase 1C architecture

```
Decky QAM (settings/control only)
        |
Python backend (main.py)
  Capture -> OCR -> OverlayManager.update(text)
                          |
                 AF_UNIX SOCK_STREAM, newline-delimited JSON
                 $XDG_RUNTIME_DIR/clarifydeck/overlay.sock
                          |
                  overlay/renderer.py (one long-lived process, one X11 window)
                          |
                 DISPLAY=:0 (Steam/mangoapp XWayland)
                          |
             GAMESCOPE_EXTERNAL_OVERLAY=1 + GAMESCOPE_NO_FOCUS=1
                          |
                        Game
```

Key properties:

- Renderer is a standalone process; QAM mount/unmount does not affect it.
- One renderer, one X11 window for the whole session; updates never recreate the
  window.
- IPC messages: `{"type":"show","text":...}`, `{"type":"update","text":...}`,
  `{"type":"hide"}`, `{"type":"shutdown"}`, `{"type":"ping"}` -> `{"type":"pong"}`.
- Text is truncated to 4096 chars; messages capped at 64 KB.
- `OverlayManager` deduplicates text, hides on empty text, detects renderer
  death and restarts it at most once.
- Display discovery: `CLARIFYDECK_OVERLAY_DISPLAY` override, then mangoapp's
  `DISPLAY` from `/proc/<pid>/environ`, then `/tmp/.X11-unix`, then `:0`.
- Text rendering uses cairo (UTF-8/CJK via fontconfig `fc-match :lang=zh`) when
  available; otherwise an ASCII-only Xlib fallback.
- Frontend (historical): the Phase 1C-era hard-false flags
  `ENABLE_LEGACY_SUBTITLE_OVERLAY=false` / `ENABLE_NOTIFICATION_KEEPALIVE=false`
  gated the disabled React caption overlay and notification keepalive; both paths
  (and the flags) were removed in Post-2I.3 Cleanup C2. Region preview (QAM open)
  is retained.
- Cleanup: plugin `_unload`/`_uninstall` calls `OverlayManager.stop()` which
  sends `shutdown`, waits, then destroys the window and removes the socket.

