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

## Product Direction Correction — Translation Removed

Status: PASS / CLOSED

Phase 2J.1 translation foundation was technically validated but removed after
product scope clarification.

ClarifyDeck is an accessibility/readability plugin, not a translation plugin.

Authoritative product flow:

game base-plane capture
-> Recognition ROI
-> OCR / recognition
-> stabilization
-> StableTextEvent
-> enlarged/reflowed persistent transparent overlay

Translation is not part of the current product pipeline.

The previously proposed Phase 2J.2 translation bridge is cancelled and must not
be implemented.

Historical note: Phase 2J.1 (commit `89beb38`) added `backend/translation.py` and
`scripts/test_translation.py` but never wired them into production. Both files
were deleted in a forward corrective commit after the product direction was
clarified; Git history is preserved. Translation must not be reintroduced as
product scope without an explicit product decision.

No OCR, transport, worker, ROI, capture, renderer, or overlay behavior changed as
part of this correction.

## Phase 2K.1 — Accepted StableTextEvent → Overlay Action Foundation

Status: LOCAL PASS / DEVICE NOT APPLICABLE

Product flow:
game base-plane capture
-> Recognition ROI
-> OCR
-> stabilization
-> accepted transport event
-> OverlayTextCoordinator
-> OverlayTextAction
-> [future main-loop delivery]
-> persistent renderer

- `backend/ocr_transport.py` gains `AcceptedStableTextEvent` (immutable view built
  only after decode/schema/order validation; exact text preserved) and one
  optional synchronous `OCRTransportObserver` (`begin_session` +
  `on_accepted_event`). `observer=None` is the production default. The receiver
  commits authoritative state/counters BEFORE notifying the observer, so
  downstream overlay problems can never invalidate OCR acceptance. Rejected
  (malformed/invalid/oversized/duplicate/out-of-order) events never notify the
  observer. Observer exceptions are isolated into separate diagnostics
  (`observer_errors`, `last_observer_error`) and do not change
  `transport_messages_rejected` / `transport_out_of_order` or the committed state.
- `backend/overlay_text.py` (pure stdlib; consumes the transport seam; never
  imported by `backend.ocr_transport`) defines `OverlayTextAction`
  (`action_seq`, `kind` text|hide, session/event identity, exact `text`,
  confidence/source_seq/timestamp; no styling/geometry fields),
  `OverlayTextCoordinator`, and a thin `OverlayTextTransportObserver` adapter.
- Frozen:
  - only accepted transport events can drive overlay actions
  - `worker_session_id` + `event_seq` define source identity (`source_seq` is
    metadata only)
  - text maps to an exact text action; `clear` maps to `hide`
  - no string-based dedupe (identical text on distinct events stays distinct)
  - session reset is explicit via `begin_session` and emits no hide action
    (renderer hide-on-restart is deferred to the production delivery gate)
  - observer failures cannot invalidate OCR acceptance
  - production observer remains disabled (`OCRTransportReceiver(observer=None)`)
  - no renderer/OverlayManager calls, no asyncio, no threads/sockets in this phase
- Thread boundary (explicitly deferred): `OCRTransportReceiver.handle_line` runs
  on the `ocr-worker-stdout` thread while `OverlayManager.update/hide` are async.
  Phase 2K.1 intentionally adds NO unsafe shortcut (`asyncio.run`,
  `new_event_loop`, `run_until_complete`, awaiting from the reader thread, or
  renderer access from that thread). The safe main-asyncio-loop handoff is the
  next gate.

Translation remains out of scope.

## Phase 2K.2 — Thread-Safe Stable Text → Persistent Overlay Delivery

Status: LOCAL PASS / DEVICE PENDING

Production flow:
accepted OCR event
-> OverlayTextCoordinator
-> OverlayTextAction
-> MainLoopOverlayDelivery
-> thread-safe scheduling to plugin asyncio loop
-> existing OverlayManager.update/hide
-> existing renderer

- `backend/overlay_delivery.py` (pure stdlib) adds `MainLoopOverlayDelivery`
  (bounded capacity=1 newest-wins pending, serial drain, non-blocking
  `submit`) and `OverlayDeliveryObserver` (coordinator → delivery). It never
  enables/starts the renderer and never constructs an `OverlayManager`; it uses a
  non-creating peek accessor.
- Production wiring (`main.py`): `_main` (leader) captures the already-running
  loop via `asyncio.get_running_loop()` and installs the observer on the SAME
  authoritative shared `OCRTransportReceiver` used by `OCRWorkerManager` and the
  QAM diagnostics. `_transport_receiver()` now passes `observer=self._overlay_observer`
  at creation; `init_overlay_delivery` attaches via `receiver.set_observer(...)`
  if the receiver already exists. One receiver is preserved (no duplicate-receiver
  regression).
- Frozen:
  - OCR stdout thread never awaits/touches the renderer; handoff is only
    `loop.call_soon_threadsafe(...)` onto the single existing main loop
  - no second event loop, no `asyncio.run`/`new_event_loop`/`run_until_complete`,
    no blocking `.result()`, no background overlay thread
  - non-blocking submit; pending capacity=1 newest-wins; serial renderer delivery
  - renderer must already be explicitly enabled; an OCR event never auto-starts
    the renderer, and overlay enable never auto-starts OCR
  - disabled overlay drops actions (no indefinite queueing); no old-text replay
    when overlay is later enabled
  - text is passed exactly; `clear` calls `hide`
  - session switch invalidates not-yet-delivered old-session actions; an action
    already executing is allowed to finish (serial order preserved)
  - delivery/loop-closed failures are isolated (`delivery_errors`,
    `last_delivery_error`) and cannot invalidate OCR transport acceptance
  - no styling/reflow/font settings added; existing renderer presentation defaults
- Lifecycle: `_unload`/`_uninstall` order is
  `stop() → shutdown_capture() → stop_ocr_worker() → close_overlay_delivery()
  → stop_overlay()`.
- Final closure requires Steam Deck device validation (worker thread → main loop
  → OverlayManager → AF_UNIX IPC → Gamescope transparent window).

### Phase 2K.2.1 — Stable clear emission fix

Status: LOCAL PASS / DEVICE RETEST PENDING

Device finding:
- live text delivery passed
- when the ROI became empty for > stale timeout, the QAM retained prior stable
  text
- therefore no authoritative `StableTextEvent(kind="clear")` reached backend
  transport

Root cause:
- `OCRDiagnostic._process_and_record` handled a change-gated `skip` by calling
  `stabilizer.tick(...)` and **discarding its return value**. `tick` legitimately
  emits one clear once the stale timeout completes after a real no-text
  observation, but that event was never written to the machine JSONL stream, so
  the backend/QAM never cleared. (With the change gate OFF, clears already flowed
  via `observe`; the loss was specific to the skipped-frame `tick` path.)

Fix:
- Extracted `OCRDiagnostic._emit_stable_events(events)` (writes stabilizer events
  to the transport stream) and call it from both `_stabilize` (real OCR
  observations) and the change-gated `skip` path after `stabilizer.tick(...)`.

Preserved:
- skipped frames do not fake OCR (a clear from `tick` still requires an earlier
  real no-text observation to have set the stale clock)
- OCR/capture/detector errors do not synthesize clear
- change detection is not correctness authority
- clear remains exactly-once after stale; clear fields unchanged
  (`kind="clear"`, `text=""`, `confidence=None`, `source_seq=None`)
- new stable text after clear emits normally (overlay can re-show)
- overlay delivery unchanged

Device retest required:
StableTextEvent(clear)
-> QAM clear
-> overlay hide
-> later new stable text restores overlay

### Phase 2K.2.2 — OCR false-positive evidence instrumentation

Status: LOCAL PASS / DEVICE SAMPLE COLLECTION PENDING

Device finding:
- visually empty game regions still produced OCR texts such as `w`, `中`, `10`
- therefore stale clear could not be exercised reliably
- no filter heuristic has been added yet

Added:
- opt-in OCR evidence diagnostics (`--diagnostic-ocr-evidence`, default **OFF**),
  emitted as one bounded `[ocr-evidence] {json}` record per **real OCR attempt**
  to **stderr** (never stdout). Fields: `frame_seq`, `timestamp_monotonic`,
  `roi_pixel_size`, `change_gate_enabled`, `ocr_trigger_reason`,
  `real_ocr_attempt`, `raw_line_count`, `usable_line_count`, `lines`
  (`text`/`confidence`/`box` in ROI-local pixels, capped at
  `MAX_EVIDENCE_LINES` with `lines_truncated`), `candidate_text`,
  `candidate_confidence`, `candidate_source_seq`, `no_usable_text`.
- skipped change-gated frames emit a distinct `[ocr-schedule]` record with
  `real_ocr_attempt: false` (never mixed with OCR evidence).
- `OCRStabilizer` gains read-only `last_observed_candidate` (None on real no-text)
  and `min_line_confidence` accessors used only for diagnostics; the candidate
  reaching the stabilizer is now directly observable.
- normal production launch from `main.py` never enables diagnostics.

Frozen:
- diagnostics OFF by default
- stdout JSONL unchanged (evidence goes to stderr only)
- no OCR threshold/filter behavior changed
- no stabilizer/transport/overlay/renderer/QAM behavior changed

Deferred:
- Change Gate QAM display resets to OFF after QAM reopen while the OCR worker
  stays RUNNING; it is unclear whether this is a frontend local-toggle reset or a
  real backend/effective-state mismatch. Not fixed here; a later gate will decide
  whether QAM should show the configured next-start value, the running effective
  value, or both.

## Phase 2L.1 — Multi-Region Recognition Foundation

Status: LOCAL PASS / DEVICE NOT APPLICABLE

Purpose: audit the existing Regions UI, and establish the authoritative
multi-region configuration model + persistence contract. No multi-region OCR
execution and no multi-block overlay rendering yet.

### Existing QAM Regions section audit

- **Add region:** `add_box` creates an in-memory `BoxState` (`uuid4().hex[:8]`,
  screen-pixel x/y/w/h) in `ClarifyDeckEngine.boxes`; emits `boxes_changed`. This
  is the legacy Phase-1 box editor for the old in-plugin capture loop
  (`start_plugin`/`_capture_loop`/`run_ocr_now`), NOT the production OCR worker.
- **Remove selected:** `remove_box` pops the in-memory box. Same legacy path.
- **frontend region state:** `useClarifyDeckState().boxes` via
  `list_boxes`/`add_box`/`update_box`/`remove_box`; also drawn by the QAM region
  preview.
- **persistence:** NO — the `BoxState` list is in-memory only.
- **backend RPC support:** yes, for the legacy box editor only.
- **production OCR influence:** NO — `scripts/ocr_worker.py` resolves exactly one
  authoritative ROI through `capture/recognition_roi.py`; the box list does not
  affect the OCR worker/StableTextEvent pipeline.
- **stable IDs currently exist:** NO for the legacy boxes (session-only, not
  persisted).

### Existing single-ROI audit

- legacy file/schema: `recognition_roi.json` v1
  (`{"version":1,"default_roi":{x,y,width,height}|null,"games":{"<app_id>":{"roi":{...}}}}`),
  strict validation, atomic temp+`os.replace` write.
- current precedence: per-game user > game profile preset > global user > built-in
  default (`ActiveROIResolver`).
- preset semantics: built-in `ROIProfileStore` map (`DEFAULT_PROFILE_STORE`) — a
  built-in authoring shortcut, not user-persisted authority.
- current production resolver: `capture/recognition_roi.resolve_active_recognition_roi`
  / `extract_recognition_roi` (single ROI).
- current ROI RPCs: `roi_config_get`/`roi_config_set`/`roi_config_reset`/
  `roi_config_preview` (unchanged by 2L.1).

### New multi-region model

- module: `capture/recognition_regions.py` (pure stdlib).
- `RecognitionRegion`: `region_id`, `x`, `y`, `w`, `h`, `enabled`, `name`
  (optional). Normalized `0..1` geometry, strict validation (rejects, never
  clamps; `MIN_ROI_SIZE = 0.02`, `x+w<=1`, `y+h<=1`, finite only).
- region_id strategy: opaque stable string (UUID assigned on first explicit v2
  write); never array-index-derived. Legacy fallback uses deterministic
  `legacy-primary` / `builtin-default`.
- `MAX_REGIONS = 8` (bounded; OCR cost grows with enabled regions).
- `RecognitionRegionSet`: ordered, unique IDs, bounded count; `enabled_regions()`
  and transitional `primary()` (first enabled).
- enabled semantics: a disabled region is valid and round-trips; an empty/fully
  disabled v2 set is valid (future OCR simply has no work) and does NOT
  auto-create a full-frame region.

### Persistence

- schema version: `2` (evolves the same `recognition_roi.json`).
- global format: `{"global": {"regions": [...]}}`.
- per-game format: `{"per_game": {"<app_id>": {"regions": [...]}}}`.
- atomic write: temp file + `os.fsync` + `os.replace` (retained).
- malformed config behavior: top-level malformed/unsupported version → safe empty
  config + `last_error`; a region list with any invalid entry is rejected whole
  (no partially trusted geometry), so resolution falls back to a trusted lower
  layer. An explicit empty list is valid.

### Compatibility

- legacy single ROI read: v1 files are read into a legacy view; effective
  resolution falls back to it with exact geometry.
- migration behavior: read is side-effect free; `adopt_effective_regions()`
  assigns fresh stable UUIDs and persists on first explicit v2 write.
- legacy APIs preserved: `roi_config_*` RPCs and `ROIConfigStore` are unchanged
  (production RPC wiring to the v2 store is deferred so the v1 file is never
  clobbered by two stores).
- primary-region rule: transitional only — first enabled effective region; not a
  future priority semantic. No automatic reordering.
- production OCR still single-region: YES.

### Why multi-region next (false-positive evidence relationship)

2K.2.2 device evidence showed visually empty areas can produce very high
confidence false positives (`01` ~0.999, `hils` ~0.867, `X` ~0.708) while valid
short text can be small (`交谈`, `Ⅱ`). Confidence/size-only suppression is
therefore unsafe as a primary fix; user-defined regions reduce irrelevant visual
input before OCR. The 2K.2.2 diagnostics are retained and remain OFF by default.

### Explicitly NOT changed

- StableTextEvent / JSONL schema (no `region_id` yet)
- OCR multi-region execution (still one transitional primary ROI crop)
- `ocr/stabilizer.py` behavior, transport schema, overlay protocol/renderer,
  `overlay_manager.py`, `backend/overlay_*`
- frontend multi-region editor (no QAM redesign)
- translation (absent)

Deferred (unchanged from 2K.2.2): Change Gate QAM toggle display resets after QAM
reopen while OCR may still run with its start-time configuration.

## Phase 2L.2 — Multi-Region OCR Execution Foundation

Status: LOCAL PASS / DEVICE NOT APPLICABLE

- `ocr/multi_region.py` (pure/lightweight; no numpy/cv2/rapidocr import) adds
  `MultiRegionOCRCoordinator`, `RegionStableTextEvent`, and `region_pixel_rect`.
- one frame decode -> many region crops: `process_frame` decodes once via
  `decode_png_ex`, then crops each enabled region using the existing
  `resolve_roi`/`crop_rgba` rules (no second geometry interpretation).
- one OCR runtime reused sequentially: the injected `OCRRuntime` is called once
  per enabled region, in collection order, with `sequence=frame.sequence` so all
  region results share the authoritative source frame sequence. No threads, no
  per-region runtime, no parallel OCR.
- independent per-region stabilizers keyed by stable `region_id` (never index):
  consensus/history/stale/clear state is fully isolated per region; no global
  stabilizer.
- lifecycle: removed region -> transient state discarded silently (no synthetic
  clear); disabled region -> state discarded, no clear; re-enable -> fresh
  stabilizer; geometry change -> only that region resets; reorder -> state
  preserved (identity is `region_id`).
- internal `RegionStableTextEvent(region_id, event)` wraps the canonical
  `StableTextEvent`; emitted in deterministic region collection order. NOT
  serialized to the v1 production JSONL transport.
- production worker remains single-region (transitional primary ROI -> one OCR
  stream -> v1 transport). Transport/receiver/QAM/renderer unchanged. No
  false-positive heuristic added. Change-gated multi-region scheduling is
  deferred (no per-region detectors in this phase).

Deferred finding (NOT fixed here; for the next region-tagged transport gate): the
stabilizer's `observe([])` clear event carries `source_seq=<frame seq>`, but the v1
transport `decode_envelope` requires `source_seq is None` for `kind="clear"`, so
that clear is rejected by the backend receiver. Only `tick`-emitted clears (which
use `source_seq=None`) are accepted. This is a transport/stabilizer contract
mismatch, outside 2L.2's frozen scope. **Resolved in Phase 2L.3 (see below).**

## Phase 2L.3 — Region-Tagged StableText Transport v2

Status: LOCAL PASS / DEVICE NOT APPLICABLE

- v1 remains supported and is the production default; the production worker still
  emits v1 single-region JSONL.
- v2 adds a required `region_id` (non-empty string, `1..128` chars, no
  normalization). One line = one event; no arrays.
- `event_seq` is worker-global: one strict increasing sequence across v1/v2 and
  all regions (no `region_event_seq`).
- `source_seq` is capture/frame metadata and may repeat across regions from the
  same frame; it is not used for transport ordering.
- clear fields are canonical: `text=""`, `confidence=None`, `source_seq=None`.
- backend `OCRTransportReceiver` keeps legacy latest stable state (v1) separate
  from a new per-region authoritative map (`region_id -> latest state`); clear is
  per-region state, not region deletion; `begin_session` resets both.
- `AcceptedStableTextEvent` gains `region_id` (None for v1) and
  `transport_version`; the optional observer receives exact region identity.
- legacy latest stable text (`state`/`state_dict`/`status`) stays v1-based and is
  not redefined as "last event from any region".
- defensive guard: the production single-block overlay adapter
  (`OverlayDeliveryObserver`) ignores region-tagged (v2) events until multi-block
  rendering exists; no renderer call.
- renderer/protocol/QAM/frontend unchanged; no multi-region scheduler; no
  multi-region worker switch.

Resolved clear mismatch: `OCRStabilizer._maybe_clear` now always emits
`source_seq=None`, so both `observe([])` and `tick()` clear paths are canonical at
source. `ocr.transport.envelope_from_event` additionally canonicalizes clear at
the transport boundary (defense in depth), so no producer can emit a second clear
shape. Stale timing, consensus, and exactly-once behavior are unchanged.

## Phase 2L.4 — Multi-Region OCR Worker Integration

Status: LOCAL PASS / DEVICE WORKER RETEST PENDING

- explicit `--multi-region` opt-in on `scripts/ocr_worker.py` (and the standalone
  `scripts/ocr_test.py` CLI); default **OFF**. The backend launcher does not pass
  it in this phase, so normal QAM "Start OCR" stays legacy single-region/v1.
- effective region resolution uses the authoritative 2L.1 resolver
  (`RegionResolver` + legacy `ActiveROIResolver`): v2 per-game > v2 global >
  legacy single-ROI fallback > built-in default. No second resolver in the worker.
- one capture frame -> one decode -> many region crops through
  `MultiRegionOCRCoordinator`; one `OCRRuntime` reused sequentially. The v1
  compatibility projection adds zero OCR calls (no duplicate primary OCR).
- every region stable event emits a v2 line
  (`encode_region_stable_text_event`); the primary region (first enabled) also
  emits a semantically equivalent v1 line for the current QAM/single-block
  overlay. Wire order is v2 then v1, with one worker-global `event_seq` across all
  lines. Non-primary regions emit v2 only.
- clear projection remains canonical (`text=""`, `confidence=None`,
  `source_seq=None`).
- multi-region + `--change-gate` is explicitly rejected before the OCR loop
  (`multi_region_change_gate_unsupported`); legacy single-region change-gate
  behavior is unchanged. Per-region change-gated scheduling is deferred.
- region configuration is resolved at worker session start (no live reload); this
  matches existing worker behavior.
- `--diagnostic-ocr-evidence` remains functional in legacy mode; per-region
  evidence records (with `region_id`) are deferred.
- renderer/overlay/QAM/frontend/transport semantics unchanged. Region OCR
  exceptions propagate to the existing `_consume` error policy (not converted to
  no-text).
- narrow correction: `RegionResolver` now assigns `builtin-default` (not
  `legacy-primary`) when the legacy resolver reports the built-in default source;
  no persistence schema change.

The primary-region v1 compatibility projection is **temporary** and should be
removed only after QAM/overlay consume explicit per-region state (multi-block).

## Phase 2L.5 — Per-Region Change-Gated Scheduling

Status: LOCAL PASS / DEVICE RETEST PENDING

- `ocr/multi_region.py` gives each enabled stable `region_id` its own
  `OCRChangeGate` via an optional `gate_factory` (never a shared gate, never keyed
  by index). `RegionRecognitionState` now holds `stabilizer + gate + geometry`.
- one frame decode is retained; each enabled region is cropped once and the same
  crop is used for both the change check and (when scheduled) OCR. No recapture,
  no per-region decode.
- independent decisions: unchanged region -> skip (no OCR); changed region -> OCR.
  One region's skip never suppresses another; forced refresh is per region
  (independent `force_interval_sec` timer); detector failure fail-opens only the
  affected region.
- skip is never converted to no-text: a skipped region calls its own
  `stabilizer.tick()` and any tick-produced clear is forwarded as a
  `RegionStableTextEvent` (the 2K.2.1 bug is not repeated).
- lifecycle: geometry change resets only that region's stabilizer + gate;
  remove/disable discard transient state (no synthetic clear); re-enable starts
  fresh; reorder preserves stabilizer/gate/forced-refresh state.
- `--multi-region --change-gate` is now supported (the 2L.4 fail-fast is removed).
  The worker builds a per-region gate factory from `--force-ocr-interval-sec`
  (default 3 s); the single-region gate instance is never shared across regions.
  Legacy single-region change-gate behavior is unchanged.
- v2/v1 compatibility projection is unchanged: primary region emits v2 then v1;
  secondary regions emit v2 only; one worker-global `event_seq`. A primary
  tick-generated clear emits v2 clear then v1 clear; a secondary tick clear emits
  v2 only and never touches legacy QAM/overlay.
- backend launcher still does not enable multi-region by default; QAM/frontend/
  renderer unchanged; no new dependencies; diagnostics remain stderr-only.

## Phase 2L.6 — Backend Production Launcher Activation

Status: LOCAL PASS / DEVICE RETEST PENDING

- `backend/ocr_worker.py::build_command` now always appends `--multi-region`, so
  the normal QAM **Start OCR** path launches the multi-region worker
  (`scripts/ocr_worker.py --multi-region`). `--change-gate` is still added only
  when the user's Change Gate choice is ON. All prior argv
  (`--parent-pid`, `--fps`, `--model-dir`, `--roi-config`) is unchanged.
- legacy v1 `recognition_roi.json` needs no migration: it resolves to one
  synthesized effective `RecognitionRegion`, emits a v2 event, and also emits the
  primary-region v1 compatibility projection. No config rewrite occurs on start.
- an existing v2 region config is consumed as-is; the first enabled effective
  region is the compatibility primary. An explicit v2 config with zero enabled
  regions runs with no region work and no stable events (no legacy fallback).
- backend receiver (unchanged) now populates real v2 per-region state in
  production while the legacy latest state follows only the primary v1 projection.
  Secondary regions cannot overwrite legacy state.
- Change Gate is per-region (2L.5) and is enabled through the existing QAM
  choice; default remains OFF; forced-refresh interval unchanged.
- QAM/overlay/frontend/renderer unchanged: current QAM stable text and the single
  overlay block keep working through the primary v1 projection. Secondary regions
  are not yet visible in QAM/overlay.
- no OCR/renderer auto-start or auto-restart behavior changed; capture_conflict,
  exact-PID ownership, parent-death, and single/fresh-session contracts unchanged.
- v2 region RPC/editor and multi-block renderer remain deferred.
- Known non-blocking: legacy aggregate `[ocr-scheduler]` counters can read zero in
  multi-region mode while per-region scheduling works; diagnostics refactor
  deferred.

## Phase 2L.7 — Persistent QAM Multi-Region Editor

Status: LOCAL PASS / DEVICE RETEST PENDING

- The old QAM `Regions` / `Recognition Area` sections were never production OCR
  (legacy in-memory `BoxState` + single-ROI editor). They are now relabeled
  `Legacy ... (Advanced)` and are no longer presented as the production region
  editor.
- New backend v2 RPCs (`main.py`): `region_config_get(app_id?)`,
  `region_config_set(regions, app_id?)`, `region_config_reset(app_id?)`, backed by
  `capture/recognition_regions.RegionConfigStore`/`RegionResolver` (the 2L.1
  authoritative model). They return `scope`, `configured` vs `effective_regions`,
  `source`, `max_regions`, `last_error`, and never expose internal objects.
- scopes: Global and This Game (per-game). No app_id source exists in the QAM yet,
  so This Game is shown disabled. Global/per-game writes are isolated; editing one
  scope never mutates the other.
- stable IDs: existing IDs are preserved on rename/geometry/enable/reorder/save.
  New/blank region IDs are assigned by the backend (`uuid4().hex`). Legacy/inherited
  regions are read without side effects and are adopted with fresh stable IDs only
  on explicit Apply (`draftsForApply` strips IDs when the scope is not configured).
- new QAM editor (`src/components/RegionEditor.tsx`, pure logic in
  `src/regionEditor.ts`): scope, region list keyed by `region_id`, add/remove,
  enable, optional name, X/Y/W/H sliders, Move up/down, Primary marker (first
  enabled effective region), Apply/Reset, backend validation errors surfaced.
  `MAX_REGIONS = 8` enforced; Add disabled at the cap.
- save/apply never starts/stops/restarts OCR or the renderer, and does not live
  reload a running worker: changes apply on the next explicit OCR start. The UI
  states this.
- The single-block overlay remains Primary-only via the v1 compatibility
  projection; secondary regions are recognized in backend v2 state but not rendered
  yet (multi-block renderer deferred).
- Known deferred: device-observed slight update latency (no cadence/consensus/
  stale tuning here); legacy aggregate scheduler counters; region preview still
  draws the legacy boxes, not v2 regions; legacy box/ROI frontend code retained as
  dead-but-compiling code for a later cleanup gate.

## Phase 2L.8 — v2 Recognition Region Live Preview

Status: LOCAL PASS / DEVICE RETEST PENDING

- The existing QAM on-screen preview (`Overlay` in `src/index.tsx`, viewport
  measured via `getBoundingClientRect`, gated by `useQuickAccessVisible`) now also
  renders the v2 Recognition Region **draft** state. This is editor/preview only:
  it is NOT the persistent OCR text renderer and does not route OCR text.
- `src/regionEditor.ts` adds pure `regionScreenRect`/`regionScreenRects`
  (normalized `x/y/w/h` -> `x*vw`, `y*vh`, `w*vw`, `h*vh`) and a module-level
  preview store (`setRegionPreview`/`getRegionPreview`/`clearRegionPreview`/
  `subscribeRegionPreview`, EventTarget-based).
- `RegionEditor.tsx` publishes `{drafts, selectedId, primaryId}` to the store
  whenever draft state changes, clears it on unmount, and reloads persisted config
  from the backend when the QAM becomes visible (so unsaved drafts are not kept
  across reopen).
- all draft regions render simultaneously; the selected region (keyed by
  `region_id`) gets the prominent outline; the first enabled draft is marked
  `[Primary]`; disabled regions stay visible at reduced opacity with a dashed
  outline and an `(off)` label. Labels use name or `Region N` (no raw UUIDs).
- slider/edit/Add/Remove/Reorder update the preview immediately from draft state;
  nothing is persisted until explicit Apply. No OCR restart or renderer lifecycle
  is triggered.
- base-plane capture isolation is unchanged (`gamescope_control
  take_screenshot(base_plane_only)`); the preview is QAM UI only.
- legacy `boxes` preview remains for the Advanced section (normally empty in
  production); the production editor uses the v2 draft preview.

### Phase 2L.8.1 — Real Game-Surface Region Preview

Status: LOCAL PASS / DEVICE RETEST PENDING

- 2L.8 device failure root cause: `mountOverlay` preferred the
  `createRoot`-into-`document.body` path. Phase 1C already proved a plain DOM node
  appended to `document.body` is **invisible** over the game even while the QAM is
  open (`CD raw` probe never appeared); only `routerHook.addGlobalComponent` (the
  Steam UI app tree) is composited over the game while the Steam UI layer is
  active (i.e. while the QAM is open). The React preview tree was therefore never
  game-visible; the legacy box preview was not truly game-visible either.
- fix: `mountOverlay` now always mounts the preview via
  `routerHook.addGlobalComponent("ClarifyDeckOverlay", Overlay)`; the body-mounted
  `createRoot` path (and its `findModule`/`ReactNode` helpers) was removed. This is
  a frontend-only change; no Python/X11 renderer protocol change was needed.
- the preview uses v2 editor **draft** state (module store), maps normalized
  `x/y/w/h` to the measured game viewport (`x*vw`, `y*vh`, `w*vw`, `h*vh`), and
  renders all drafts with selected/Primary/disabled distinctions. Slider/Add/
  Remove/Reorder update it live with no persistence until Apply.
- preview is gated by `useQuickAccessVisible` and cleared on editor unmount, so it
  disappears when the QAM closes. It is independent of the persistent OCR text
  overlay (the Python renderer) and does not require enabling it.
- device diagnostics: when the draft preview changes while the QAM is visible, the
  viewport and computed pixel rects are logged once (`[region-preview]`).
- Region Name input removed from the production QAM editor (Steam Deck text entry
  is impractical); labels are order-derived `Region 1/2/...` with `[Primary]` and
  `(off)`. The optional backend `name` field is retained in persistence and
  round-trips unchanged; `region_id` remains the identity.
- capture isolation unchanged (`gamescope_control take_screenshot(base_plane_only)`);
  the preview is QAM UI only and cannot enter OCR input.

### Phase 2L.8.2 — Explicit Renderer-Based Region Preview

Status: LOCAL PASS / DEVICE RETEST PENDING

- Device outcome: 2L.8 (body-mounted React preview) and 2L.8.1 (Steam-UI-tree
  React preview) both rendered nothing over the game. Decision: stop attempting
  implicit React/Steam-UI preview paths and reuse the proven Python/X11 external
  overlay renderer for region rectangles.
- Explicit control: **Show Region Preview** toggle in the QAM Recognition Regions
  section. Default **OFF**, session/UI-local (not persisted, not in
  `recognition_roi.json`). Plugin boot / QAM open / opening the editor never shows
  boxes.
- Renderer ownership is now reason-based: the shared renderer stays alive while
  the persistent text overlay OR the region preview needs it, and stops only when
  both are off. `OverlayManager.status()` exposes `enabled` (text) plus
  `preview_enabled`/`preview_region_count`. `stop()` is the hard teardown used by
  unload/uninstall.
- Protocol: `set_region_preview {regions:[...]}` and `clear_region_preview`
  (normalized `x/y/w/h`, `selected`/`primary`/`enabled`/`label`); sanitized in
  `overlay/protocol.sanitize_preview_regions` (bounded `MAX_PREVIEW_REGIONS`,
  drops invalid entries, never crashes). Renderer keeps independent text and
  preview state; text update/hide never clears preview and vice versa.
- Renderer draws outline-only rectangles with a small label (`Primary · Region N`
  / `Region N (off)`), mapping `x*surface_width`, `y*surface_height` against the
  actual renderer window (not QAM dimensions). Distinctions use line width,
  opacity, and label (not color alone). A narrow `[renderer] preview count=...
  surface=... rects=...` debug line is emitted only when debug/ preview is on.
- Preview data comes from live frontend draft regions; slider/Add/Remove/Reorder/
  selection update it with no Apply and no config write. QAM close clears the
  preview and disables preview (renderer stops only if the text overlay is off).
- Preview never starts/stops/restarts OCR; it is independent of the OCR worker
  lifecycle. No text replay is triggered. Base-plane capture
  (`take_screenshot(base_plane_only)`) is unchanged and excludes the overlay.
- Region Name input remains removed; automatic `Region N` labels remain.

## Phase 2M.1 — Per-Region Persistent OCR Text Blocks

Status: LOCAL PASS / DEVICE RETEST PENDING

- v2 region events are now the authoritative persistent-overlay input. Each
  accepted v2 event drives one text block keyed by `worker_session_id + region_id`
  (never index/label). The paired primary v1 compatibility projection still
  updates backend legacy/QAM state but is ignored by the overlay (no duplicate
  primary block); a true v1-only session keeps the legacy single block.
  `OverlayDeliveryObserver` tracks `UNKNOWN → LEGACY_V1 / REGION_V2`.
- delivery pending is now per-region (`pending_by_region[region_id]`, latest-wins
  within a region, never cross-region overwrite) instead of the legacy global
  capacity-1 slot. Legacy v1 actions keep the single slot.
- region geometry is snapshotted at explicit OCR start
  (`ClarifyDeckEngine._resolve_region_layout` → `MainLoopOverlayDelivery.set_region_layout`)
  using the same 2L.1 resolver as the worker; QAM edits do not live-move running
  blocks; Stop/Start picks up new geometry. Unknown/disabled regions are not
  rendered (bounded diagnostic), never crash.
- protocol: `set_region_text {region_id, rect, text}`, `hide_region_text`,
  `clear_all_region_text`; `overlay/protocol.sanitize_region_text` validates
  normalized geometry. One renderer process holds three independent layers:
  legacy text, per-region text blocks, region preview. Text update/hide never
  clears preview and vice versa.
- text blocks: outline-free (text only), rendered inside the region rectangle
  (`x*surface_width`, `y*surface_height`), character-wrapped to the block width
  (`protocol.wrap_text`) and vertically clipped (`protocol.clip_lines` + cairo
  clip). Fixed default font size (20px); no font/scroll controls yet.
- lifecycle: overlay OFF drops incoming region actions (no replay); enabling
  overlay starts blocks empty; disabling overlay clears all text blocks but never
  the preview layer; new session clears old pending + old blocks. Renderer
  ownership (text/preview reasons) and no-replay/no-auto-start contracts are
  preserved. OCR/transport/change-gate/capture semantics unchanged.
- font-size/scroll UX is deferred to Phase 2M.2.

## Phase 2M.1.1 — Short-Region Glyph Render Guard

Status: DEVICE PASS / CLOSED

Device validation (from `04e6b45c92b41da86ea678f0194be3b6d2149766`):
- `h=0.04` Stable Text remained present
- `h=0.04` overlay glyph rendered
- exact region clipping preserved (no glyph leak)
- Region 2 unchanged and independent
- Preview / Persistent Overlay lifecycle remained stable
- no new Python Exception

- real device reproduction at `h=0.04`: Region 1 stable text was recognized and
  its text block existed, but no glyph was drawn, while Region 2 (`h=0.13`)
  rendered normally. Changing only Region 1 to `h=0.08` (Apply / Stop OCR /
  Start OCR) made its text reappear, isolating a short-region rendering
  boundary rather than OCR, transport, region config, or multi-region delivery.
- root cause: `protocol.clip_lines(lines, line_height, max_height)` computed
  `int(max_height // line_height)`. At `h=0.04` on 1280x800 the region is 32px
  tall; with 8px padding the drawable inner height is 16px, below the 26px
  nominal line height, so the visible-line capacity was `0` and the glyph loop
  received an empty list even though the region was drawable.
- fix: `clip_lines` now returns at least one line whenever the drawable height
  is positive (`max_height > 0`) and the line height is valid, regardless of
  whether a full nominal line fits. A region with no drawable area
  (`max_height <= 0`) or an invalid line height still returns no lines.
- minimum one visible line only when the drawable area is positive; the renderer
  keeps its existing cairo clip rectangle at the exact configured region bounds,
  so the single line is vertically clipped and no glyph leaks into adjacent
  regions.
- normal-height regions are unchanged: capacity for regions that already fit N
  full lines is still exactly N; only the zero-line edge case changes.
- no OCR / transport / change-gate / capture / region-persistence / renderer
  lifecycle / preview lifecycle changes; frontend untouched.

Accepted lifecycle observation (frozen, not part of any gate):
- Persistent Overlay re-enable preserves no-replay semantics; old backend Stable
  Text is not replayed automatically. In the tested device flow, Stop OCR ->
  Start OCR caused text to render again. This is accepted/deferred.

## Phase 2M.2A — Per-Region Semi-Transparent Text Panels

Status: DEVICE PASS / CLOSED

Device validation (from `69ef7807ec7041505be3aa441a3fa584b35b84cd`):
- `h=0.04` short Region remained readable with panel
- two inverse styles worked simultaneously
- live style update remained per-region
- clear removed only the target region panel/text
- Preview + panels coexisted
- Persistent Overlay OFF removed panels and kept Preview
- accepted no-replay behavior remained
- no OCR restart from style changes
- runtime style reset on restart as designed
- no new Python Exception / renderer lifecycle regression

- every visible per-region OCR text block now draws a semi-transparent panel
  filling its exact configured region rectangle, with the text on top; the
  Region Preview outline/label is still drawn last, so editing stays legible.
- exactly two fixed styles: `WHITE_ON_BLACK` (opaque white text, translucent
  black panel) and `BLACK_ON_WHITE` (opaque black text, translucent white panel).
  One fixed panel alpha (`protocol.PANEL_ALPHA = 0.65`). No opacity UI.
- style is per-region, keyed by stable `region_id` (never index/label/order),
  default `WHITE_ON_BLACK`. Runtime/in-memory only: `OverlayManager._region_style`
  remembers a region's style across text updates and across overlay
  disable/enable within one backend lifetime; it resets on backend restart.
- `set_region_text` carries the region's effective style to the renderer; a
  narrow `set_region_style {region_id, style}` renderer command updates an
  already-visible block live. A style change never creates an empty panel, never
  starts/stops OCR, never starts the renderer, and never changes geometry.
- narrow backend API: `region_panel_style_get(region_id)` (missing ->
  `WHITE_ON_BLACK`) and `region_panel_style_set(region_id, style)` (exactly the
  two styles; invalid rejected safely, previous value preserved). The old broad
  `region_presentation_get` is NOT restored.
- minimal QAM control on the selected region ("Dark panel" / "Light panel",
  session-only). No font-size, scroll, opacity, or custom-color controls.
- panel/clear semantics: a panel exists only with a visible text block;
  `hide_region_text` and `clear_all_region_text` remove text and panel together
  and never leave an empty translucent rectangle. Style memory is not a panel.
- the 2M.1.1 short-region guard is preserved: `h=0.04` renders the exact 32px
  panel plus at least one clipped text line, with no panel/glyph leak.
- no persistence: no `overlay_presentation.json`, no `recognition_roi.json`
  schema change, no new settings file. No OCR/transport/change-gate/capture
  changes; no scrolling; no touch/input work; renderer ownership and the
  no-replay lifecycle are unchanged.

## Phase 2M.2B — Per-Region Font Size

Status: DEVICE PASS / CLOSED

Device validation (from `15f96a5427fa9018aa081436d3a4bb98cc6eb2f1`):
- per-region font sizes changed live and rewrapped visible text
- two regions used different font sizes simultaneously
- font changes stayed independent from panel style
- short `h=0.04` region still rendered at small/default/large sizes
- clear removed only the target region text/panel
- Preview + panels + custom sizes coexisted
- Persistent Overlay no-replay behavior remained
- no OCR restart from font changes; runtime size reset on restart as designed
- no new Python Exception / renderer lifecycle regression

- per-region runtime font size, keyed by stable `region_id` (never
  index/label/order), default `20`. Runtime/in-memory only:
  `OverlayManager._region_font_size` remembers a region's size across text
  updates, clear/hide, and overlay disable/enable within one backend lifetime;
  it resets to `20` on backend restart.
- bounded and validated range `14 .. 48` (integer; UI step `2`). `protocol.
  sanitize_region_font_size` accepts only in-range integers (or integral finite
  floats) and rejects bools, non-numeric types, non-integral floats, NaN/Infinity,
  and out-of-range values. Invalid input is rejected with `ok:false`,
  `error:"invalid_font_size"`, previous value preserved, no partial mutation.
- line height keeps the frozen rule `font_size * 1.3`
  (`protocol.region_line_height`). The renderer sets the cairo font per block and
  rewraps the block's full retained text, so a size change reruns
  wrap/line-height/capacity/clip from the original text (not a scale of
  precomputed lines). Exact panel geometry and style are untouched.
- `set_region_text` carries the region's effective `font_size`; a narrow
  `set_region_font_size {region_id, font_size}` renderer command updates an
  already-visible block live. A font change never creates an empty panel, never
  starts/stops OCR, never starts the renderer, never changes geometry, and never
  changes style.
- narrow backend API: `region_font_size_get(region_id)` (missing -> `20`) and
  `region_font_size_set(region_id, font_size)` (validated range).
- minimal QAM slider on the selected region ("Size", min 14 / max 48 / step 2,
  session-only). Selection change reloads that region's runtime size; the style
  selector is preserved and independent.
- 2M.1.1 short-region guard preserved at every allowed size: `h=0.04` still
  schedules at least one clipped line at 14/20/32/48; a large font may be heavily
  clipped but never produces a panel with no text.
- no persistence: no `overlay_presentation.json`, no `recognition_roi.json`
  schema change, no new settings file. No OCR/stabilizer/change-gate/transport/
  capture changes; no scrolling; no touch/input work; panel alpha and styles
  unchanged; renderer ownership and the no-replay lifecycle unchanged.

## Phase 2M.2C — Region Profile JSON Management + Dropdown Region CRUD

Status: DEVICE PASS / CLOSED

Device validation (from `a61c4b673009ec4273437aa0c8ae3b84a85af2a4`):
- Region Set add/delete and independent JSON files work
- last-Region-Set delete guard holds
- profile switch does not restart OCR; the running session keeps its snapshot
- next Stop/Start uses the selected Region Set
- Region add/delete and MAX_REGIONS hold
- no new Python Exception / renderer lifecycle regression

Earlier device failures (from `79a5c6b7838d2d7321e6d8c2f3d13e5dc802b77d`):
- oversized `+`/`-` controls beside Region Set and Region disrupted QAM use
- a deleted Region Set display number was not reused (monotonic labels)
- the Region dropdown did not actually select Region 2 (editor stayed on Region 1)
- Region Preview visually disappeared while a Dropdown popup was open

- Region Sets ("Region Profiles") are independently persisted JSON files under
  `<settings>/region_profiles/`. New module `capture/region_profiles.py`
  (`RegionProfileStore`, pure stdlib) owns the directory, index, bootstrap,
  migration and CRUD; each profile file reuses the proven v2 RecognitionRegion
  schema via `RegionConfigStore` (no schema duplication/change).
- storage layout: `region_profiles/index.json` + `region_profiles/
  profile_<profile_id>.json`. Index schema v1:
  `{version, active_profile_id, next_label_number, profiles:[{profile_id,
  label, file}]}`. Index is authoritative; orphan files are ignored, never
  auto-imported. Index validation rejects unsafe filenames (separators, `..`,
  absolute paths), duplicate ids/files, bad labels, and > `MAX_REGION_PROFILES`
  (16). `profile_id` is a stable UUID and the only identity; display order/label
  are never identity.
- bootstrap/migration: when `index.json` is absent, one profile is created. A
  valid legacy `recognition_roi.json` (v2 or v1) is imported once, preserving
  geometry and existing `region_id` values exactly; otherwise the built-in
  fallback region is used. After bootstrap the profile store is authoritative
  and the legacy file is never written again (no dual-write) and is no longer
  consulted as a resolution fallback layer. The legacy file is left on disk as a
  rollback artifact (not deleted). A corrupt index is quarantined to
  `index.corrupt-<stamp>.json` (evidence preserved) and safely rebuilt; a
  missing/corrupt profile is skipped and a valid remaining profile (or a fresh
  default) becomes active. All index/profile writes are atomic (temp + fsync +
  `os.replace`) with `0700` dir / `0600` files where the platform permits.
- backend API: `region_profiles_get()`, `region_profile_select(profile_id)`,
  `region_profile_add()`, `region_profile_delete(profile_id)`. Add creates an
  independent profile file first, then references it in the index; delete writes
  a valid index without the target first, then unlinks (orphan tolerated).
  Deleting the last profile is rejected (`cannot_delete_last_profile`). The
  existing `region_config_get/set/reset` now operate on the active profile
  (Region add/delete/edit stay draft + Apply, writing only the active profile).
  New Regions get fresh globally-unique `region_id` UUIDs; `MAX_REGIONS = 8`
  preserved.
- QAM: a "Region Set" `Dropdown` (profile_id) with compact `+`/`-` and a
  "Region" `Dropdown` (region_id) with compact `+`/`-`. Switching sets reloads
  that set's Regions and discards unsaved drafts; profile add/delete/select
  persist immediately, Region add/delete are draft operations committed by
  Apply. Style/font selectors remain and are keyed by the new region_id.
- OCR session semantics frozen: switching the active Region Set does NOT
  hot-swap a running OCR worker. The worker keeps its Start-time region
  snapshot; the next explicit Stop -> Start uses the newly active Region Set.
  Profile operations never start/restart OCR, never start the renderer while
  Preview is OFF, and never replay Stable Text. Preview ON reflects the selected
  set's geometry.
- no presentation persistence: style/font are still runtime-only; no
  `overlay_presentation.json`. No scrolling, no touch/input, no renderer/
  protocol rendering changes, no OCR/stabilizer/change-gate/transport/capture
  changes.
- roadmap: 2M.2D Presentation Persistence (style + font) -> 2M.2E Direct Touch
  Input Probe -> 2M.2F Direct Touch Scroll only if the probe proves safe.

## Phase 2M.2C.1 — Device Remediation

Status: DEVICE PASS / CLOSED

Device validation (from `a61c4b673009ec4273437aa0c8ae3b84a85af2a4`):
- compact `+`/`-` controls
- smallest-free Region Set label reuse
- fresh identity/data on a reused label
- Region dropdown correctly selects non-Primary Regions
- style/font follow the selected Region
- Preview remains visible with a dropdown popup
- no OCR/renderer lifecycle regression

- F1 compact controls: the four `+`/`-` actions are now fixed 30x30px native
  buttons (`compactButtonStyle`) in a `minmax(0, 1fr) 30px 30px` grid, so the
  Dropdown keeps the row width and the actions no longer flex-grow or overflow.
- F2 label reuse: Region Set display numbers now allocate the smallest unused
  positive integer (`next_available_region_set_number`, parsed from canonical
  `Region Set N` labels) instead of a monotonic counter. Deleting Region Set 2
  and adding again yields `Region Set 2` with a fresh `profile_id` and a fresh
  `profile_<uuid>.json` (no identity/data resurrection). `next_label_number`
  remains in the index for backward compatibility but is no longer the allocator.
- F3 Region dropdown selection: root cause is that the QAM editor can remount
  while a Dropdown context menu is open, which discarded the editor-local
  `selectedId` (the Region Set dropdown only appeared to work because the active
  profile is persisted to the backend index). Fix: `selectRegion` is now the
  single selection path, Dropdown payloads are normalized
  (`dropdownOptionValue`, accepts `{data}` or a raw value), option arrays are
  memoized, and selection is remembered in a module-level editor session
  (`rememberRegionSelection`) so it survives a transient remount. `[Primary]`
  remains display-only and never forces selection.
- F4 Preview/dropdown coexistence: frontend audit found no RPC that clears or
  disables Preview on Dropdown open; the only path was the unmount cleanup
  disabling Preview when the editor remounted. Fix: `previewOn` intent is
  remembered in the same editor session and restored on remount, and the unmount
  cleanup now only tears Preview down when the QAM is genuinely closing
  (`qamVisibleRef`), while a QAM-close effect also disables Preview and resets the
  session. No renderer/X11/input/ShapeInput change was made. If the visual
  disappearance persists on device with backend `preview_enabled=true`, it is a
  Steam/Gamescope popup compositor limitation and must be reported, not fixed in
  the renderer.
- unchanged: profile/index schema, profile_id and region_id identity, active
  profile semantics, atomic writes, Region CRUD, OCR Start-time snapshot, no
  replay, Preview default OFF, no presentation persistence, no scrolling, no
  touch/input, no renderer draw/ownership changes.

## Phase 2M.2D — Per-Region Presentation Persistence

Status: DEVICE FAIL / REMEDIATION REQUIRED

Device reproduction (from `ff7ce16681df97b913f6f45acd40089935644dd7`):
- Persistent Overlay OFF -> ON -> Stop OCR -> Start OCR
- Stable Text present; Region Preview present; region text absent

- new file `<settings>/overlay_presentation.json`, separate from
  `recognition_roi.json` and the `region_profiles/` files (which stay geometry/
  config only). New pure-stdlib module `overlay/presentation.py`
  (`PresentationStore`). Schema v1:
  `{"version": 1, "regions": {"<region_id>": {"style": "white_on_black",
  "font_size": 20}}}`. The canonical `protocol` style values (lowercase) and the
  `14..48` font range are reused; unknown extra fields are ignored.
- identity is the globally-unique stable `region_id` only. Never keyed by
  profile label, Region Set number, display index, `Region N`, or Primary. A
  reused Region Set label gets a fresh region_id, so it never inherits the
  deleted set's presentation.
- persisted fields are exactly `style` and `font_size`. Defaults remain
  `WHITE_ON_BLACK` / `20`; a missing entry (or missing file) uses defaults.
- `OverlayManager(presentation_path=...)` loads at construction (state
  restoration only: no OCR/renderer start, no Preview/overlay enable, no text
  block, no Stable Text replay) and seeds `_region_style` / `_region_font_size`.
  The existing `region_panel_style_get` / `region_font_size_get` return the
  restored values.
- explicit save: new RPC `region_appearance_save(region_id)` persists the
  manager's current authoritative runtime style/font for that region. It never
  starts/stops OCR or the renderer, never changes geometry/style/font, and never
  creates a block. Live style/font edits are NOT auto-saved.
- save is atomic (temp + fsync + `os.replace`, mode `0600`) and preserves every
  other entry, including entries for regions in inactive/deleted profiles
  (orphans). Save of one region never overwrites the others. Save failure is
  surfaced (`presentation_write_failed`) without corrupting the previous file and
  without rolling back runtime appearance.
- safe load: missing file -> defaults, no file created; corrupt JSON /
  unsupported version -> defaults + `last_error`, evidence preserved (never
  overwritten on load); partially invalid entries -> valid entries load, invalid
  ignored; deleted Region/Region Set entries are ignored safely (no GC in this
  gate).
- QAM: one compact "Save appearance" action on the selected Region (targets the
  exact `region_id`). The style selector and font slider remain live; no
  auto-save. The 2M.2C.1 compact `+`/`-` layout, Region dropdown selection, and
  Preview/dropdown coexistence are unchanged.
- unchanged: OCR/stabilizer/change-gate/transport, Region Profile schema,
  RecognitionRegion schema, renderer draw path, panel alpha/styles, font range/
  line-height, the h=0.04 one-line guard, renderer ownership, Persistent Overlay
  no-replay, no scrolling, no touch/input.
- roadmap: 2M.2E Direct Touch Input Probe -> 2M.2F Direct Touch Scroll only if
  the probe proves safe.

## Phase 2M.2D.1 — Persistent Overlay Re-enable Text Delivery Recovery

Status: LOCAL PASS / DEVICE RETEST PENDING

- proven root cause (region-source divergence, introduced by 2M.2C): the overlay
  resolves its per-region layout from the authoritative active Region Profile
  (`region_profiles/profile_<uuid>.json`), but the OCR worker still resolved
  regions from the frozen legacy `recognition_roi.json` (`scripts/ocr_test.py
  ._resolve_effective_regions` via `recognition_roi.get_store().path`). Once the
  active profile diverges from the legacy file (e.g. after a Region edit or a new
  Region Set), the worker emits v2 events whose `region_id`s are absent from the
  overlay layout, so `MainLoopOverlayDelivery._deliver_region` silently drops
  them as `actions_dropped_unknown_region`. Result: Stable Text present, Preview
  present, region text absent. The overlay OFF/ON toggle in the report was not
  causal; the divergence was.
- delivery/manager audit (negative evidence): the coordinator session reset,
  `set_session`, per-region pending, `text_enabled`, `_is_running`, and the
  renderer `set_region_text` IPC all behave correctly across OFF -> ON ->
  Stop/Start. No text-based duplicate suppression exists in the delivery path;
  transport `event_seq` protection and the coordinator `stale_or_duplicate`
  guard are preserved unchanged.
- fix: at each explicit OCR Start, the engine passes the active Region Profile
  file to the worker as `--roi-config` (`ClarifyDeckEngine
  .active_region_config_path()` -> `OCRWorkerManager.start(roi_config=...)`), so
  the worker and the overlay resolve the SAME authoritative region source and the
  same `region_id`s. The Start-time snapshot semantics are preserved (both sides
  resolve once at Start; QAM edits apply on the next Start). The legacy
  `recognition_roi.json` is no longer passed to the worker.
- invariant restored: Persistent Overlay may clear rendered text, but it never
  poisons future fresh Stable Text delivery. OFF clears text; ON alone does not
  replay; a new OCR session's fresh accepted v2 event (same or changed string)
  renders again for one or more regions, using restored style/font.
- unchanged: no OCR recognition/model/stabilizer/change-gate/capture change; no
  transport v2 schema change; no Region Profile/RecognitionRegion schema change;
  no renderer/`overlay/renderer.py` change; no `ShapeInput`/touch/scroll work; no
  presentation-persistence change; Preview independence preserved.

## Phase 2N.2 — Steam Performance HUD coexistence

Status: ACCEPTED LIMITATION (product decision)

- device A/B: the Steam Deck built-in Performance/FPS HUD cannot coexist with
  ClarifyDeck's external X11 overlay; the HUD fails as soon as the renderer exists
  and returns immediately when the renderer exits. Removing
  `GAMESCOPE_EXTERNAL_OVERLAY` restores the HUD but makes the ClarifyDeck overlay
  disappear from the game. This is accepted for now; no further Gamescope
  coexistence work is planned.
- touch scrolling remains deferred/abandoned for the current roadmap; the touch
  research commits are not part of the production baseline.

## Phase 2N.3 — End-to-End OCR Text-Update Latency Audit

Status: LOCAL PASS / DEVICE LATENCY MEASUREMENT PENDING

- optional, runtime-only latency instrumentation for changed Stable Text; no
  behavioral tuning (capture FPS, OCR FPS, change gate, model, detector limits,
  threads, stabilizer thresholds, renderer behavior all frozen).
- worker: `MultiRegionOCRCoordinator` times decode, per-region crop, OCR and
  stabilizer acceptance, records a bounded (8) sample on each changed text, and
  `scripts/ocr_test.py` prints one `[latency]` line per change to stderr
  (`capture_age_at_ocr_start_ms`, `decode_ms`, `roi_ms`, `ocr_ms`,
  `stabilizer_accept_ms`, `worker_total_ms`); no text content is logged.
- stabilizer: new read-only `first_candidate_timestamp(text)` (no behavior change).
- transport: optional `captured_monotonic` field on the v2 event (absent on older
  events; validated when present) carried through `AcceptedStableTextEvent` ->
  `OverlayTextAction` -> `set_region_text` payload. Not a required-schema change.
- manager: `set_region_text` accepts optional `source_seq` /
  `stable_text_monotonic` / `captured_monotonic`, includes them in the renderer
  payload, and logs `[latency] region=… frame=… stable_text_to_send_ms=…`.
- renderer: logs `[latency] region=… frame=… renderer_ms=… frame_age_at_render_ms=…`
  after drawing a changed text block (read-only, no rendering change).
- no new dependency, no persisted data, no new RPC/QAM UI; instrumentation is
  bounded and only fires on changed text.

## Phase 2N.3 — device latency result

Status: DEVICE MEASUREMENT (partial)

- typical `frame_age_at_render_ms` ≈ 0.55–0.82 s; OCR (`ocr_ms`) ≈ 0.57–0.77 s
  dominates the accepted-frame path; `decode_ms` ≈ 31–37 ms; `roi_ms` < 1 ms;
  `capture_age_at_ocr_start_ms` ≈ 33–38 ms (no meaningful queue backlog in the
  typical samples); `stable_text_to_send_ms` ≈ 1 ms; `renderer_ms` ≈ 2–5 ms.
- `stabilizer_accept_ms` ≈ 0.73–1.03 s observed; it spans first-candidate to
  acceptance across OCR cycles and is not additive with `worker_total_ms`.
- 2N.3.1 hotfix: the renderer used `time.monotonic()` without importing `time`,
  crashing the renderer on the first latency-enabled text update
  (`NameError`); fixed by adding `import time` (`bcedd617`). Regression test
  `scripts/test_renderer_latency_hotfix.py` executes the latency path.

## Phase 2N.4 — Stabilizer First-Candidate Reliability Audit

Status: LOCAL PASS / DEVICE RELIABILITY MEASUREMENT PENDING

- observational audit only; `consensus_required=2`, `history_size=3`,
  `min_line_confidence=0.70`, `stale_timeout_sec=2.0`, exact-string and clear
  semantics are unchanged, and no live fast-accept path exists.
- `OCRStabilizer` tracks a transition trial (first eligible candidate after the
  current Stable Text) and, on each acceptance, records: region_id, frame seq,
  first/accepted candidate seq, candidate count until accept, first/accepted
  confidence, `first_matches_final`, lengths, `first_candidate_to_accept_ms`,
  `theoretical_fast_accept_saving_ms`, distinct-candidate count, and short
  process-local digests (no OCR text is logged). Bounded: 16 recent records, 64
  savings samples, aggregate counters with confidence buckets
  (0.70–0.79 / 0.80–0.89 / 0.90–0.94 / 0.95–1.00).
- confidence semantics: the candidate confidence is the existing
  minimum-line-confidence already used for eligibility (no new formula).
- clear transitions are counted separately and excluded from the
  text-replacement reliability metric; repeated already-stable text never starts
  a trial; trial state resets on acceptance, clear, `reset()` (session), and
  region removal; state is per `region_id`.
- output: `scripts/ocr_test.py` prints one `[stabilizer-audit]` line per accepted
  transition (stderr) and a `[stabilizer-audit-summary]` aggregate on shutdown.
- no capture/OCR/stabilizer-behavior/transport/overlay/renderer changes; no new
  dependency; no persisted telemetry; no QAM UI.

## Phase 2N.4 — device reliability result

Status: DEVICE MEASUREMENT (one game, one Region, normal dialog)

- 52 accepted text transitions collected during normal Lies of P dialog
  progression in one Region.
- `first_matches_final` = 51 / 52 (overall first/final match rate 98.08%).
- confidence buckets: `0.95-1.00` count 48, matches 48 (100%); `0.90-0.94`
  count 2, matches 1 (50%); `0.80-0.89` count 2, matches 2 (100%). So `>=0.95`
  coverage = 48/52 (~92.3%) with 48/48 matches.
- median first-candidate-to-accept ≈ 998.123 ms; `0.95-1.00` saving sum
  47566.783 ms.
- the single mismatch had `first_conf=0.93356`, `candidate_count=3`,
  `distinct_candidates=2`, `first_to_accept_ms=1966.478`.
- limitation: this is strong evidence for this game/session, not proof of
  universal correctness across all games/languages.

## Phase 2N.5A — Conservative High-Confidence Fast Accept

Status: LOCAL PASS / DEVICE RETEST PENDING (not a new stable baseline)

- narrow, reversible fast path for **replacement-only** high-confidence text;
  the existing consensus path remains the fallback everywhere else.
- `FAST_ACCEPT_MIN_CONFIDENCE = 0.95` (existing minimum-line-confidence
  semantics; no new confidence formula). Not exposed in UI.
- a candidate is fast-accepted only when all hold: non-empty eligible candidate;
  an existing non-empty Stable Text is published; the exact normalized text
  differs from it; confidence >= 0.95; it is the first eligible candidate of the
  current replacement trial; no fast-accept lock is held.
- initial publication is explicitly excluded: with no Stable Text yet the first
  OCR result still requires `consensus_required = 2`.
- fallback unchanged for everything else: `consensus_required = 2`,
  `history_size = 3`, `min_line_confidence = 0.70`, `stale_timeout_sec = 2.0`;
  clear/no-text/skipped-frame semantics are unchanged.
- anti-flapping state (per region): after a fast accept the text is locked; the
  immediately next eligible candidate is shadow-inspected (diagnostic only). A
  match releases the lock; a mismatch leaves it locked until the ordinary
  consensus path accepts a different value. Alternating high-confidence noise
  therefore does not emit one Stable Text event per frame and never emits more
  than the unchanged consensus path would.
- diagnostics (stderr only, no raw OCR text): `[fast-accept]` per fast accept,
  `[fast-accept-confirm]` per resolved shadow check, `[fast-accept-summary]`
  (`fast_accept_total`, `fast_accept_next_match`, `fast_accept_next_diff`,
  `fast_accept_confirm_rate`, `fallback_accept_total`, median realized saving) on
  shutdown; bounded recent records (16). Mirrored to the plugin journal.
- the 2N.4 reliability audit is retained; fast-accepted transitions are tagged
  (`fast_accept` / `fast_accept_transitions`) so the historical consensus=2
  metric is not misread.
- no capture/OCR-runtime/renderer/transport-schema/overlay-delivery changes; no
  new dependency; no persisted telemetry; no QAM UI.

## Phase 2N.5A — cross-game device result

Status: DEVICE PASS (two games)

- validated on Lies of P and Sekiro; no visible text flicker and no A/B
  ping-pong observed.
- Sekiro: 53 fast accepts, 52 next-candidate matches (98.11%); median realized
  saving ≈ 996 ms.
- the fast-accept path and its consensus fallback therefore remained stable
  across two games/fonts. `437ad63` is the cross-game validated functional
  baseline.

## Phase 2N.6 — Gamescope PipeWire capture backend prototype

Status: LOCAL PASS / DEVICE PIPEWIRE RETEST PENDING

Feasibility evidence (Steam Deck device, established before implementation):

- Gamescope publishes a PipeWire node (`node.name=gamescope`,
  `media.class=Video/Source`, `stream.is-live=true`); object id/serial are not
  stable and are never hard-coded.
- continuous stream proven: `pipewiresrc target-object=gamescope
  keepalive-time=33 ! queue max-size-buffers=1 leaky=downstream !
  video/x-raw,format=NV12 ! fakesink`, negotiated 1280x800 NV12.
- in-memory mapping proven: Python `appsink` mapped CPU-readable NV12 at
  ~89.95 FPS; a 1280x800 frame is 1,536,000 bytes (`1280*800*1.5`).
- layer composition (diagnostic images): the persistent ClarifyDeck X11 overlay
  was absent from the tested PipeWire frame; a bottom-right Steam/Decky
  notification was visible. This is observed device behavior, not a universal
  Gamescope guarantee (device regression test required).
- connection readiness may transiently report `target not found`; the system
  plugin was `/usr/lib/gstreamer-1.0/libgstpipewire.so` 1.6.4.
- a one-off `gst_mini_object_unref` GStreamer-CRITICAL warning was observed at
  startup while frames flowed normally; it is recorded as known non-fatal (a real
  bus ERROR or no frames remains fatal).

Prototype architecture:

- `capture/backend.py` selects between `ScreenshotCaptureBackend` (default,
  unchanged) and `PipeWireCaptureBackend`; both expose
  `start()/stop()/capture_frame()/status()` and feed the existing
  `CaptureProducer` -> `LatestFrameQueue` -> consumer.
- `capture/pipewire_capture.py` lazily imports `gi`/`Gst` (never at module
  import), builds `pipewiresrc target-object=gamescope keepalive-time=33 !
  video/x-raw,format=NV12 ! queue max-size-buffers=1 leaky=downstream !
  appsink max-buffers=1 drop=true sync=false`, and maps/converts exactly one
  sample per capture tick. There is no per-source-frame Python callback and no
  unbounded queue. Startup retries are bounded (~5 s window, ~0.5 s backoff) and
  cancellable by Stop OCR; bus ERROR/EOS/no-frame fail the session clearly.
- the only new boundary is `capture/frame.py::DecodedFrame` +
  `capture/change_detector.py::decode_frame_ex`: the PipeWire backend converts
  NV12 -> RGBA once (OpenCV/numpy when available, dependency-free fallback
  otherwise, honouring stride/offset metadata) and hands the unchanged Region
  crop/coordinator the same decoded representation used by the PNG path.
- `decode_ms` keeps its meaning (decoder wrapper cost, ~0 for a decoded frame);
  the PipeWire-only `frame_conversion_ms` is added to the existing `[latency]`
  line when present. No PNG is encoded/written/decoded on the PipeWire path and
  the screenshot API is never invoked while PipeWire is active (no silent
  fallback).
- selector: development-only `CLARIFYDECK_CAPTURE_BACKEND=pipewire` (default
  `screenshot`). `OCRWorkerManager` reads it and forwards an explicit
  `--capture-backend` to the worker; the worker also accepts
  `--capture-backend`. No QAM UI, no persistence, no default flip.
- device deployment: set the variable in the environment of the Decky plugin
  loader process (or pass `--capture-backend pipewire` to `scripts/ocr_worker.py`
  directly) so the OCR worker inherits it.
- explicitly out of scope for this phase: DMABUF/zero-copy, renderer changes,
  Gamescope X11 property changes, Change Gate + PipeWire combination, OCR/FPS
  tuning, and removing the screenshot backend.

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

