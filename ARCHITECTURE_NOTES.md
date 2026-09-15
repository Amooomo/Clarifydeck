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
- Frontend flags: `ENABLE_LEGACY_SUBTITLE_OVERLAY=false`,
  `ENABLE_NOTIFICATION_KEEPALIVE=false`. Region preview (QAM open) is retained.
- Cleanup: plugin `_unload`/`_uninstall` calls `OverlayManager.stop()` which
  sends `shutdown`, waits, then destroys the window and removes the socket.

