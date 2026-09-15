#!/usr/bin/env python3
"""Phase 2F.1 tests: runtime bundle, bootstrap, model manifest v2, params.

Run:
    python3 scripts/test_ocr_bundle.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import ocr_test  # noqa: E402
import build_ocr_runtime_bundle as bundle_builder  # noqa: E402
from ocr import (  # noqa: E402
    BundleError,
    BundleManifest,
    BundlePackage,
    BundleTarget,
    activate_plugin_ocr_runtime,
    build_bundle_manifest,
    is_target_compatible_wheel,
    load_bundle_manifest,
    load_manifest,
    normalize_dist_name,
    probe_runtime,
    resolver_abis,
    resolver_platforms,
    running_target,
    validate_assets,
    validate_bundle_compat,
    validate_bundle_manifest,
    wheel_matches_target,
    wheel_tags,
)
from ocr.runtime import _rapidocr_params  # noqa: E402

TARGET = running_target()
BUNDLE_TARGET = BundleTarget(python="3.13", abi="cp313", arch="x86_64", platform="manylinux_2_28_x86_64")


def _valid_bundle_manifest(target=None) -> dict:
    target = target or TARGET
    return {
        "format_version": 1,
        "target": {
            "python": target.python,
            "abi": target.abi,
            "arch": target.arch,
            "platform": "manylinux_2_28_x86_64",
        },
        "onnxruntime_version": "1.30.0",
        "rapidocr_version": "3.9.2",
        "packages": [
            {
                "name": "numpy",
                "version": "2.1.0",
                "wheel": "numpy-2.1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
                "sha256": "0" * 64,
            },
            {"name": "omegaconf", "version": "2.3.1", "wheel": "omegaconf-2.3.1-py3-none-any.whl"},
            {
                "name": "antlr4-python3-runtime",
                "version": "4.9.3",
                "wheel": "antlr4_python3_runtime-4.9.3-py3-none-any.whl",
            },
        ],
    }


def _plugin_root(manifest: dict | None = None, site_packages: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="clarifydeck-bundle-"))
    runtime = root / "runtime" / "ocr"
    if site_packages:
        (runtime / "site-packages").mkdir(parents=True)
    if manifest is not None:
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _model_dir_v2(det_hash=None, rec_hash=None, dict_mode="embedded", det_name="PP-OCRv6_det_small.onnx") -> Path:
    directory = Path(tempfile.mkdtemp(prefix="clarifydeck-model-")) / "ppocrv6"
    directory.mkdir(parents=True)
    (directory / det_name).write_bytes(b"det-bytes")
    (directory / "PP-OCRv6_rec_small.onnx").write_bytes(b"rec-bytes")
    det = {"path": det_name}
    rec = {"path": "PP-OCRv6_rec_small.onnx"}
    if det_hash:
        det["sha256"] = det_hash
    if rec_hash:
        rec["sha256"] = rec_hash
    manifest = {
        "format_version": 2,
        "engine": "rapidocr",
        "rapidocr_version": "3.9.2",
        "family": "PP-OCRv6",
        "model_type": "small",
        "engine_type": "onnxruntime",
        "files": {"det": det, "rec": rec},
        "dictionary": {"mode": dict_mode},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class BootstrapTest(unittest.TestCase):
    def test_bootstrap_inserts_once(self) -> None:
        root = _plugin_root(_valid_bundle_manifest())
        site_packages = str(root / "runtime" / "ocr" / "site-packages")
        try:
            activated = activate_plugin_ocr_runtime(root, require=True)
            self.assertEqual(str(activated), site_packages)
            self.assertEqual(sys.path.count(site_packages), 1)
        finally:
            while site_packages in sys.path:
                sys.path.remove(site_packages)

    def test_bootstrap_idempotent(self) -> None:
        root = _plugin_root(_valid_bundle_manifest())
        site_packages = str(root / "runtime" / "ocr" / "site-packages")
        try:
            activate_plugin_ocr_runtime(root)
            activate_plugin_ocr_runtime(root)
            self.assertEqual(sys.path.count(site_packages), 1)
        finally:
            while site_packages in sys.path:
                sys.path.remove(site_packages)

    def test_missing_bundle_fails_safely(self) -> None:
        root = _plugin_root(site_packages=False)
        self.assertIsNone(activate_plugin_ocr_runtime(root, require=False))
        with self.assertRaises(BundleError) as ctx:
            activate_plugin_ocr_runtime(root, require=True)
        self.assertEqual(ctx.exception.code, "runtime_bundle_missing")

    def test_invalid_bundle_manifest_fails_safely(self) -> None:
        root = _plugin_root()
        (root / "runtime" / "ocr" / "bundle_manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(BundleError) as ctx:
            activate_plugin_ocr_runtime(root, require=True)
        self.assertEqual(ctx.exception.code, "bundle_manifest_invalid")

    def test_wrong_python_target_rejected(self) -> None:
        manifest = load_bundle_manifest(_write_manifest(_valid_bundle_manifest()))
        bad = type(TARGET)(python="3.12", abi=TARGET.abi, arch=TARGET.arch, platform="")
        with self.assertRaises(BundleError) as ctx:
            validate_bundle_manifest(manifest, bad)
        self.assertEqual(ctx.exception.code, "bundle_target_mismatch")

    def test_wrong_arch_rejected(self) -> None:
        manifest = load_bundle_manifest(_write_manifest(_valid_bundle_manifest()))
        bad = type(TARGET)(python=TARGET.python, abi=TARGET.abi, arch="aarch64", platform="")
        with self.assertRaises(BundleError) as ctx:
            validate_bundle_manifest(manifest, bad)
        self.assertEqual(ctx.exception.code, "bundle_target_mismatch")

    def test_package_source_paths_reported(self) -> None:
        info = probe_runtime()
        self.assertIn("numpy_path", info)
        self.assertIn("rapidocr_path", info)
        self.assertIn("onnxruntime_path", info)
        self.assertIn("cv2_path", info)
        self.assertIn("glibc", info)
        self.assertIn("soabi", info)


class WheelTargetTest(unittest.TestCase):
    def test_accepts_target_wheels(self) -> None:
        self.assertTrue(wheel_matches_target("numpy-2.1.0-cp313-cp313-manylinux_2_28_x86_64.whl", BUNDLE_TARGET))
        self.assertTrue(wheel_matches_target("rapidocr-3.9.2-py3-none-any.whl", BUNDLE_TARGET))

    def test_rejects_foreign_wheels(self) -> None:
        self.assertFalse(wheel_matches_target("numpy-2.1.0-cp313-cp313-win_amd64.whl", BUNDLE_TARGET))
        self.assertFalse(wheel_matches_target("numpy-2.1.0-cp313-cp313-macosx_11_0_arm64.whl", BUNDLE_TARGET))
        self.assertFalse(wheel_matches_target("numpy-2.1.0-cp314-cp314-manylinux_2_28_x86_64.whl", BUNDLE_TARGET))
        self.assertFalse(wheel_matches_target("numpy-2.1.0.tar.gz", BUNDLE_TARGET))
        self.assertFalse(wheel_matches_target("numpy-2.1.0-cp312-cp312-manylinux_2_28_x86_64.whl", BUNDLE_TARGET))


class ModelManifestV2Test(unittest.TestCase):
    def test_model_manifest_v2_valid(self) -> None:
        manifest = load_manifest(_model_dir_v2())
        self.assertEqual(manifest.format_version, 2)
        self.assertEqual(manifest.family, "PP-OCRv6")
        self.assertEqual(manifest.dictionary_mode, "embedded")
        self.assertEqual(manifest.model_type, "small")

    def test_shipped_manifest_v2(self) -> None:
        manifest = load_manifest(ROOT / "models" / "ppocrv6")
        self.assertEqual(manifest.format_version, 2)
        self.assertEqual(manifest.family, "PP-OCRv6")
        self.assertEqual(manifest.dictionary_mode, "embedded")
        self.assertEqual(manifest.sha256["det"], "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f")
        self.assertEqual(manifest.sha256["rec"], "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884")

    def test_det_hash_validated(self) -> None:
        directory = _model_dir_v2()
        good = _hash(directory / "PP-OCRv6_det_small.onnx")
        manifest = load_manifest(_model_dir_v2(det_hash=good))
        validate_assets(manifest)  # no raise

    def test_rec_hash_validated(self) -> None:
        directory = _model_dir_v2()
        good = _hash(directory / "PP-OCRv6_rec_small.onnx")
        validate_assets(load_manifest(_model_dir_v2(rec_hash=good)))  # no raise

    def test_hash_mismatch_rejected(self) -> None:
        manifest = load_manifest(_model_dir_v2(det_hash="0" * 64))
        with self.assertRaises(Exception) as ctx:
            validate_assets(manifest)
        self.assertEqual(getattr(ctx.exception, "code", None), "model_hash_mismatch")

    def test_embedded_dictionary_accepted(self) -> None:
        manifest = load_manifest(_model_dir_v2(dict_mode="embedded"))
        validate_assets(manifest)  # det+rec only, no dict file required
        self.assertEqual(manifest.dictionary_mode, "embedded")

    def test_invalid_dictionary_mode_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            load_manifest(_model_dir_v2(dict_mode="auto"))
        self.assertEqual(getattr(ctx.exception, "code", None), "invalid_dictionary_mode")

    def test_traineddata_not_accepted(self) -> None:
        with self.assertRaises(Exception) as ctx:
            load_manifest(_model_dir_v2(det_name="chi_sim.traineddata"))
        self.assertEqual(getattr(ctx.exception, "code", None), "invalid_model_asset")

    def test_v1_manifest_backward_compatible(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="clarifydeck-model-")) / "ppocrv6"
        directory.mkdir(parents=True)
        for name in ("det.onnx", "rec.onnx", "dict.txt"):
            (directory / name).write_bytes(b"x")
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "engine": "rapidocr",
                    "family": "pp-ocrv6",
                    "files": {"det": "det.onnx", "rec": "rec.onnx", "dict": "dict.txt"},
                }
            ),
            encoding="utf-8",
        )
        manifest = load_manifest(directory)
        self.assertEqual(manifest.format_version, 1)
        self.assertEqual(manifest.dictionary_mode, "external")


class RapidOcrParamsTest(unittest.TestCase):
    def test_params_use_explicit_paths_no_download(self) -> None:
        params = _rapidocr_params(load_manifest(_model_dir_v2()))
        self.assertIn("Det.model_path", params)
        self.assertIn("Rec.model_path", params)
        self.assertTrue(Path(params["Det.model_path"]).is_absolute())
        self.assertTrue(Path(params["Rec.model_path"]).is_absolute())
        for key in params:
            self.assertNotIn("url", key.lower())
            self.assertNotIn("download", key.lower())
        self.assertNotIn("Rec.rec_keys_path", params)  # embedded dictionary

    def test_rapidocr_params_v392_keys(self) -> None:
        from enum import Enum

        class EngineType(Enum):
            ONNXRUNTIME = "onnxruntime"

        class OCRVersion(Enum):
            PPOCRV6 = "PP-OCRv6"

        class ModelType(Enum):
            SMALL = "small"

        stub = SimpleNamespace(EngineType=EngineType, OCRVersion=OCRVersion, ModelType=ModelType, __file__="none")
        params = _rapidocr_params(load_manifest(_model_dir_v2()), stub)
        self.assertIs(params["Det.engine_type"], EngineType.ONNXRUNTIME)
        self.assertIs(params["Det.ocr_version"], OCRVersion.PPOCRV6)
        self.assertIs(params["Det.model_type"], ModelType.SMALL)
        self.assertIs(params["Rec.engine_type"], EngineType.ONNXRUNTIME)
        self.assertIs(params["Rec.ocr_version"], OCRVersion.PPOCRV6)
        self.assertIs(params["Rec.model_type"], ModelType.SMALL)
        self.assertIs(params["Global.use_cls"], False)

    def test_external_dictionary_adds_keys_path(self) -> None:
        directory = _model_dir_v2(dict_mode="external")
        (directory / "dict.txt").write_bytes(b"a\nb\n")
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["dict"] = {"path": "dict.txt"}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        params = _rapidocr_params(load_manifest(directory))
        self.assertIn("Rec.rec_keys_path", params)


class SafetyTest(unittest.TestCase):
    def test_main_imports_no_ocr_native(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("import numpy", source)
        self.assertNotIn("import onnxruntime", source)
        self.assertNotIn("import rapidocr", source)
        self.assertNotIn("import cv2", source)
        self.assertNotIn("OCRRuntime", source)
        self.assertNotIn("ocr.runtime", source)

    def test_probe_starts_no_capture(self) -> None:
        source = (SCRIPTS / "ocr_runtime_probe.py").read_text(encoding="utf-8")
        for forbidden in ("ClarifyDeckEngine", "CaptureProducer", "GamescopeCapture", "OverlayManager"):
            self.assertNotIn(forbidden, source)


class ResolverTagTest(unittest.TestCase):
    """Phase 2F.1a: multi-platform / multi-ABI resolver tag policy."""

    def _ok(self, filename: str) -> bool:
        return is_target_compatible_wheel(
            filename,
            python_version=(3, 13),
            arch="x86_64",
            glibc=(2, 41),
            minimum_manylinux=(2, 17),
            maximum_bundle_floor=(2, 28),
        )

    def test_manylinux_2_28_accepted(self) -> None:
        self.assertTrue(self._ok("pkg-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"))

    def test_manylinux_2_27_accepted(self) -> None:
        self.assertTrue(self._ok("numpy-2.1.0-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"))

    def test_manylinux_2_17_accepted(self) -> None:
        self.assertTrue(self._ok("pkg-1.0-cp313-cp313-manylinux_2_17_x86_64.whl"))

    def test_manylinux2014_accepted(self) -> None:
        self.assertTrue(self._ok("pkg-1.0-cp313-cp313-manylinux2014_x86_64.whl"))

    def test_dual_2_17_and_2014_accepted(self) -> None:
        self.assertTrue(
            self._ok("pyclipper-1.3.0.post6-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
        )

    def test_pure_python_accepted(self) -> None:
        self.assertTrue(self._ok("rapidocr-3.9.2-py3-none-any.whl"))

    def test_abi3_linux_accepted(self) -> None:
        self.assertTrue(self._ok("pkg-1.0-cp37-abi3-manylinux2014_x86_64.whl"))

    def test_cp311_rejected(self) -> None:
        self.assertFalse(self._ok("pkg-1.0-cp311-cp311-manylinux_2_28_x86_64.whl"))

    def test_cp314_rejected(self) -> None:
        self.assertFalse(self._ok("pkg-1.0-cp314-cp314-manylinux_2_28_x86_64.whl"))

    def test_win_rejected(self) -> None:
        self.assertFalse(self._ok("pkg-1.0-cp313-cp313-win_amd64.whl"))

    def test_macos_rejected(self) -> None:
        self.assertFalse(self._ok("pkg-1.0-cp313-cp313-macosx_11_0_arm64.whl"))

    def test_aarch64_rejected(self) -> None:
        self.assertFalse(self._ok("pkg-1.0-cp313-cp313-manylinux_2_28_aarch64.whl"))

    def test_musllinux_rejected(self) -> None:
        self.assertFalse(self._ok("pkg-1.0-cp313-cp313-musllinux_1_2_x86_64.whl"))

    def test_source_dist_rejected(self) -> None:
        self.assertFalse(self._ok("pkg-1.0.tar.gz"))

    def test_platform_list_contains_floor_and_ceiling(self) -> None:
        platforms = resolver_platforms()
        self.assertIn("manylinux_2_28_x86_64", platforms)
        self.assertIn("manylinux_2_27_x86_64", platforms)
        self.assertIn("manylinux_2_17_x86_64", platforms)
        self.assertIn("manylinux2014_x86_64", platforms)
        self.assertNotIn("manylinux_2_12_x86_64", platforms)

    def test_abi_list_contains_stable_abis(self) -> None:
        self.assertEqual(resolver_abis((3, 13)), ["cp313", "abi3", "none"])

    def test_pyclipper_cp313_manylinux2014_example(self) -> None:
        self.assertTrue(
            self._ok("pyclipper-1.3.0.post6-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
        )

    def test_pyclipper_windows_example_rejected(self) -> None:
        self.assertFalse(self._ok("pyclipper-1.3.0.post6-cp313-cp313-win_amd64.whl"))

    def test_wheel_tags_dual_platform(self) -> None:
        tags = wheel_tags("pyclipper-1.3.0.post6-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
        platforms = {tag[2] for tag in tags}
        self.assertIn("manylinux_2_17_x86_64", platforms)
        self.assertIn("manylinux2014_x86_64", platforms)

    def test_manifest_records_resolver_and_tags(self) -> None:
        wheel = _wheel_file("numpy-2.1.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
        manifest = build_bundle_manifest(
            [wheel],
            BUNDLE_TARGET,
            onnxruntime_version="1.30.0",
            rapidocr_version="3.9.2",
            resolver={
                "resolver_python": "3.13",
                "resolver_abis": ["cp313", "abi3", "none"],
                "resolver_platforms": resolver_platforms(),
                "device_glibc": "2.41",
                "bundle_manylinux_floor": "2.17",
                "bundle_manylinux_ceiling": "2.28",
            },
        )
        self.assertEqual(manifest["resolver"]["resolver_python"], "3.13")
        self.assertEqual(manifest["resolver"]["bundle_manylinux_floor"], "2.17")
        package = manifest["packages"][0]
        self.assertEqual(package["name"], "numpy")
        self.assertIn("manylinux2014_x86_64", package["platform_tags"])
        self.assertIn("cp313", package["python_tags"])
        self.assertIn("cp313", package["abi_tags"])


class OcrTestBootstrapTest(unittest.TestCase):
    """Phase 2F.1c: ocr_test.py must activate the plugin-local runtime first."""

    def _fake_bundle_root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="clarifydeck-ctest-"))
        runtime = root / "runtime" / "ocr"
        (runtime / "site-packages").mkdir(parents=True)
        target = running_target()
        manifest = {
            "format_version": 1,
            "target": {
                "python": target.python,
                "abi": target.abi,
                "arch": target.arch,
                "platform": "manylinux_2_28_x86_64",
            },
            "packages": [
                {
                    "name": "numpy",
                    "version": "2.5.3",
                    "wheel": "numpy-2.5.3-cp313-cp313-manylinux_2_28_x86_64.whl",
                    "sha256": "0" * 64,
                },
                {"name": "omegaconf", "version": "2.3.1", "wheel": "omegaconf-2.3.1-py3-none-any.whl"},
                {
                    "name": "antlr4-python3-runtime",
                    "version": "4.9.3",
                    "wheel": "antlr4_python3_runtime-4.9.3-py3-none-any.whl",
                },
            ],
        }
        (runtime / "bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def _run(self, argv) -> tuple[int, str]:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ocr_test.main(argv)
        return code, out.getvalue()

    def test_probe_activates_plugin_local_runtime(self) -> None:
        import ocr_test

        root = self._fake_bundle_root()
        site_packages = str(root / "runtime" / "ocr" / "site-packages")
        try:
            _code, output = self._run(
                ["--probe", "--no-init", "--plugin-root", str(root), "--require-bundle"]
            )
            self.assertIn("runtime_activated=True", output)
            self.assertIn(site_packages, output)
        finally:
            while site_packages in sys.path:
                sys.path.remove(site_packages)

    def test_bootstrap_before_ocr_import(self) -> None:
        import ocr_test

        root = self._fake_bundle_root()
        site_packages = str(root / "runtime" / "ocr" / "site-packages")
        calls: list[str] = []
        original_activate = ocr_test.activate_plugin_ocr_runtime
        original_probe = ocr_test.probe_runtime

        def activate(*args, **kwargs):
            calls.append("activate")
            return original_activate(*args, **kwargs)

        def probe(*args, **kwargs):
            calls.append("probe")
            return original_probe(*args, **kwargs)

        ocr_test.activate_plugin_ocr_runtime = activate
        ocr_test.probe_runtime = probe
        try:
            self._run(["--probe", "--no-init", "--plugin-root", str(root)])
            self.assertEqual(calls[:2], ["activate", "probe"])
        finally:
            ocr_test.activate_plugin_ocr_runtime = original_activate
            ocr_test.probe_runtime = original_probe
            while site_packages in sys.path:
                sys.path.remove(site_packages)

    def test_bootstrap_idempotent_via_main(self) -> None:
        import ocr_test

        root = self._fake_bundle_root()
        site_packages = str(root / "runtime" / "ocr" / "site-packages")
        try:
            self._run(["--probe", "--no-init", "--plugin-root", str(root)])
            self._run(["--probe", "--no-init", "--plugin-root", str(root)])
            self.assertEqual(sys.path.count(site_packages), 1)
        finally:
            while site_packages in sys.path:
                sys.path.remove(site_packages)

    def test_missing_bundle_fails_safely(self) -> None:
        import ocr_test

        root = Path(tempfile.mkdtemp(prefix="clarifydeck-ctest-empty-"))
        code, output = self._run(
            ["--probe", "--no-init", "--plugin-root", str(root), "--require-bundle"]
        )
        self.assertEqual(code, 1)
        self.assertIn("bundle_error=runtime_bundle_missing", output)

    def test_ocr_test_does_not_rely_on_user_site(self) -> None:
        source = (SCRIPTS / "ocr_test.py").read_text(encoding="utf-8")
        self.assertIn("activate_plugin_ocr_runtime", source)
        for forbidden in ("addsitedir", "getusersitepackages", "PYTHONPATH", "~/.local"):
            self.assertNotIn(forbidden, source)

    def test_main_still_imports_no_ocr_native(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        for forbidden in ("import numpy", "import onnxruntime", "import rapidocr", "import cv2", "OCRRuntime"):
            self.assertNotIn(forbidden, source)


class CompatPinTest(unittest.TestCase):
    """Phase 2F.1d: OmegaConf 2.3.1 / ANTLR 4.9.3 compatibility pins."""

    def _manifest(self, packages) -> BundleManifest:
        target = running_target()
        return BundleManifest(
            format_version=1,
            target=target,
            packages=tuple(
                BundlePackage(name=name, version=version, wheel=f"{name}-{version}-py3-none-any.whl")
                for name, version in packages
            ),
            onnxruntime_version=None,
            rapidocr_version=None,
            path=Path("."),
        )

    def test_omegaconf_and_antlr_pins_required(self) -> None:
        validate_bundle_compat(
            self._manifest([("omegaconf", "2.3.1"), ("antlr4-python3-runtime", "4.9.3")])
        )

    def test_missing_omegaconf_rejected(self) -> None:
        with self.assertRaises(BundleError) as ctx:
            validate_bundle_compat(self._manifest([("antlr4-python3-runtime", "4.9.3")]))
        self.assertEqual(ctx.exception.code, "bundle_package_missing")

    def test_omegaconf_2_0_0_rejected(self) -> None:
        with self.assertRaises(BundleError) as ctx:
            validate_bundle_compat(
                self._manifest([("omegaconf", "2.0.0"), ("antlr4-python3-runtime", "4.9.3")])
            )
        self.assertEqual(ctx.exception.code, "bundle_package_rejected")

    def test_omegaconf_2_2_1_rejected(self) -> None:
        with self.assertRaises(BundleError) as ctx:
            validate_bundle_compat(
                self._manifest([("omegaconf", "2.2.1"), ("antlr4-python3-runtime", "4.9.3")])
            )
        self.assertEqual(ctx.exception.code, "bundle_package_rejected")

    def test_antlr_wrong_version_rejected(self) -> None:
        with self.assertRaises(BundleError) as ctx:
            validate_bundle_compat(
                self._manifest([("omegaconf", "2.3.1"), ("antlr4-python3-runtime", "4.13.1")])
            )
        self.assertEqual(ctx.exception.code, "bundle_package_mismatch")

    def test_antlr_wheel_is_universal(self) -> None:
        name = "antlr4_python3_runtime-4.9.3-py3-none-any.whl"
        self.assertTrue(is_target_compatible_wheel(name, python_version=(3, 13), arch="x86_64"))
        self.assertIn("any", {tag[2] for tag in wheel_tags(name)})

    def test_antlr_provenance_recorded(self) -> None:
        wheel = _wheel_file("antlr4_python3_runtime-4.9.3-py3-none-any.whl")
        manifest = build_bundle_manifest(
            [wheel],
            BUNDLE_TARGET,
            provenance={
                normalize_dist_name("antlr4-python3-runtime"): {
                    "source": "PyPI sdist",
                    "source_version": "4.9.3",
                    "built_on": "Linux build host",
                }
            },
        )
        entry = manifest["packages"][0]
        self.assertEqual(entry["source"], "PyPI sdist")
        self.assertEqual(entry["source_version"], "4.9.3")
        self.assertEqual(entry["built_on"], "Linux build host")
        self.assertIn("wheel_sha256", entry)

    def test_install_uses_no_index(self) -> None:
        import inspect

        source = inspect.getsource(bundle_builder._install_command)
        self.assertIn("--no-index", source)
        self.assertIn("--find-links", source)

    def test_install_does_not_resolve_online(self) -> None:
        import inspect

        source = inspect.getsource(bundle_builder._install_command)
        self.assertNotIn("download", source)

    def test_probe_reports_omegaconf_and_antlr(self) -> None:
        info = probe_runtime()
        self.assertIn("omegaconf", info)
        self.assertIn("omegaconf_path", info)
        self.assertIn("antlr4", info)
        self.assertIn("antlr4_path", info)

    def test_plugin_local_resolution(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="clarifydeck-compat-"))
        site_packages = root / "runtime" / "ocr" / "site-packages"
        (site_packages / "omegaconf").mkdir(parents=True)
        (site_packages / "omegaconf" / "__init__.py").write_text('__version__ = "2.3.1"\n', encoding="utf-8")
        (site_packages / "antlr4").mkdir(parents=True)
        (site_packages / "antlr4" / "__init__.py").write_text('__version__ = "4.9.3"\n', encoding="utf-8")
        target = running_target()
        (root / "runtime" / "ocr" / "bundle_manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "target": {
                        "python": target.python,
                        "abi": target.abi,
                        "arch": target.arch,
                        "platform": "manylinux_2_28_x86_64",
                    },
                    "packages": [
                        {"name": "omegaconf", "version": "2.3.1", "wheel": "x.whl"},
                        {"name": "antlr4-python3-runtime", "version": "4.9.3", "wheel": "y.whl"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        saved = {name: sys.modules.pop(name, None) for name in ("omegaconf", "antlr4")}
        try:
            activate_plugin_ocr_runtime(root, require=True)
            info = probe_runtime()
            self.assertEqual(info.get("omegaconf"), "2.3.1")
            self.assertEqual(info.get("antlr4"), "4.9.3")
            self.assertIn(str(site_packages), info.get("omegaconf_path") or "")
            self.assertIn(str(site_packages), info.get("antlr4_path") or "")
        finally:
            while str(site_packages) in sys.path:
                sys.path.remove(str(site_packages))
            for name, module in saved.items():
                sys.modules.pop(name, None)
                if module is not None:
                    sys.modules[name] = module

    def test_rapidocr_not_patched(self) -> None:
        source = (SCRIPTS / "build_ocr_runtime_bundle.py").read_text(encoding="utf-8")
        self.assertNotIn("model_root_dir", source)
        self.assertNotIn("rapidocr/main.py", source)
        self.assertNotIn("rapidocr\\main.py", source)

    def test_omegaconf_accepts_pathlib_path(self) -> None:
        """Reproduce the Gate 0.4 UnsupportedValueType failure against the bundle."""
        site_packages = ROOT / "runtime" / "ocr" / "site-packages"
        if not (site_packages / "omegaconf").is_dir():
            self.skipTest("plugin-local omegaconf not present")
        saved = sys.modules.pop("omegaconf", None)
        sys.path.insert(0, str(site_packages))
        try:
            from pathlib import Path as _Path

            from omegaconf import OmegaConf

            self.assertEqual(OmegaConf.__module__.split(".")[0], "omegaconf")
            cfg = OmegaConf.create({"Global": {"model_root_dir": None}})
            cfg.Global.model_root_dir = _Path("/tmp/models")
            self.assertTrue(str(cfg.Global.model_root_dir))
        finally:
            while str(site_packages) in sys.path:
                sys.path.remove(str(site_packages))
            sys.modules.pop("omegaconf", None)
            if saved is not None:
                sys.modules["omegaconf"] = saved


class ResolverBridgeTest(unittest.TestCase):
    """Phase 2F.1e: main resolver must see the local ANTLR wheelhouse."""

    def _args(self, **overrides):
        import argparse

        base = {
            "python_version": "3.13",
            "abi": "cp313",
            "arch": "x86_64",
            "platform_tag": "manylinux_2_28_x86_64",
            "onnxruntime_version": "1.30.0",
            "rapidocr_version": "3.9.2",
            "omegaconf_version": "2.3.1",
            "antlr_version": "4.9.3",
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def _wheelhouse(self) -> Path:
        wd = Path(tempfile.mkdtemp(prefix="clarifydeck-wheelhouse-"))
        for name in (
            "antlr4_python3_runtime-4.9.3-py3-none-any.whl",
            "numpy-2.5.3-cp313-cp313-manylinux_2_28_x86_64.whl",
            "omegaconf-2.3.1-py3-none-any.whl",
            "rapidocr-3.9.2-py3-none-any.whl",
            "onnxruntime-1.30.0-cp313-cp313-manylinux_2_28_x86_64.whl",
        ):
            (wd / name).write_bytes(b"dummy")
        return wd

    def test_prepare_before_download_and_find_links(self) -> None:
        import contextlib
        import io

        wd = self._wheelhouse()
        out = Path(tempfile.mkdtemp(prefix="clarifydeck-out-"))
        calls = []

        class FakeResult:
            returncode = 0

        def fake_run(command, *args, **kwargs):
            calls.append(command)
            return FakeResult()

        original = bundle_builder.subprocess.run
        bundle_builder.subprocess.run = fake_run
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = bundle_builder.main(
                    ["--wheel-dir", str(wd), "--out", str(out), "--install", "--clean-site-packages"]
                )
        finally:
            bundle_builder.subprocess.run = original

        self.assertEqual(code, 0)
        download = next(command for command in calls if "download" in command)
        install = next(command for command in calls if "install" in command)
        self.assertIn("--find-links", download)
        self.assertNotIn("--no-index", download)
        self.assertIn("--no-index", install)
        index = download.index("--find-links")
        self.assertEqual(download[index + 1], str(wd))
        # wheelhouse is not cleared after the ANTLR prebuild
        self.assertTrue((wd / "antlr4_python3_runtime-4.9.3-py3-none-any.whl").exists())

    def test_download_command_has_find_links_no_index(self) -> None:
        command = bundle_builder._download_command(self._args(), Path("wh"), bundle_builder._resolver(self._args()))
        self.assertIn("--find-links", command)
        self.assertNotIn("--no-index", command)
        self.assertIn("omegaconf==2.3.1", command)

    def test_install_command_uses_no_index(self) -> None:
        command = bundle_builder._install_command(
            self._args(), Path("wh"), Path("sp"), bundle_builder._resolver(self._args())
        )
        self.assertIn("--no-index", command)
        self.assertIn("--find-links", command)

    def test_valid_antlr_wheel_reusable(self) -> None:
        wd = Path(tempfile.mkdtemp(prefix="clarifydeck-antlr-"))
        wheel = wd / "antlr4_python3_runtime-4.9.3-py3-none-any.whl"
        wheel.write_bytes(b"dummy")
        self.assertTrue(bundle_builder._validate_antlr_wheel(wheel, "4.9.3"))
        calls = []
        original = bundle_builder.subprocess.run
        bundle_builder.subprocess.run = lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})()
        try:
            self.assertEqual(bundle_builder._prepare_antlr(self._args(), wd), 0)
        finally:
            bundle_builder.subprocess.run = original
        self.assertEqual(calls, [])  # reused, no pip wheel call

    def test_invalid_antlr_wheel_rejected(self) -> None:
        self.assertFalse(
            bundle_builder._validate_antlr_wheel(
                _wheel_file("antlr4_python3_runtime-4.13.1-py3-none-any.whl"), "4.9.3"
            )
        )
        self.assertFalse(
            bundle_builder._validate_antlr_wheel(
                _wheel_file("antlr4_python3_runtime-4.9.3-cp313-cp313-win_amd64.whl"), "4.9.3"
            )
        )

    def test_antlr_required_by_resolver_requirements(self) -> None:
        requirements = bundle_builder._requirements(self._args())
        self.assertIn("omegaconf==2.3.1", requirements)
        self.assertNotIn("antlr4-python3-runtime==4.9.3", requirements)  # provided via wheelhouse

    def test_source_orders_prepare_before_download(self) -> None:
        import inspect

        source = inspect.getsource(bundle_builder.main)
        self.assertLess(source.index("_prepare_antlr(args"), source.index("_download(args"))


class ExplicitModelPathTest(unittest.TestCase):
    """Phase 2F.1f: explicit ClarifyDeck model paths reach the RapidOCR constructor."""

    def _stub_module(self, captured, *, fail=False, wrong_path=False, with_cls=True):
        from enum import Enum

        class EngineType(Enum):
            ONNXRUNTIME = "onnxruntime"

        class OCRVersion(Enum):
            PPOCRV6 = "PP-OCRv6"

        class ModelType(Enum):
            SMALL = "small"

        class FakeRapidOCR:
            def __init__(self, config_path=None, params=None):
                if fail:
                    raise TypeError("The value of Det.ocr_version must be Enum Type.")
                captured.append(params or {})
                self.params = params or {}
                det = self.params.get("Det.model_path")
                rec = self.params.get("Rec.model_path")
                self.cfg = SimpleNamespace(
                    Det=SimpleNamespace(model_path=("other" if wrong_path else det)),
                    Rec=SimpleNamespace(model_path=rec),
                )

        package_dir = Path(tempfile.mkdtemp(prefix="clarifydeck-rapidocr-"))
        if with_cls:
            (package_dir / "models").mkdir(parents=True, exist_ok=True)
            (package_dir / "models" / "ch_ppocr_mobile_v2.0_cls_mobile.onnx").write_bytes(b"cls")
        return SimpleNamespace(
            RapidOCR=FakeRapidOCR,
            EngineType=EngineType,
            OCRVersion=OCRVersion,
            ModelType=ModelType,
            __file__=str(package_dir / "__init__.py"),
        )

    def _initialize(self, model_dir, captured, **kwargs):
        from ocr import OCRConfig, OCRRuntime

        stub = self._stub_module(captured, **kwargs)
        original = sys.modules.get("rapidocr")
        sys.modules["rapidocr"] = stub
        try:
            runtime = OCRRuntime(OCRConfig(model_dir=model_dir))
            runtime.initialize()
        finally:
            if original is None:
                sys.modules.pop("rapidocr", None)
            else:
                sys.modules["rapidocr"] = original
        return runtime

    def test_det_path_reaches_constructor(self) -> None:
        captured: list = []
        model_dir = _model_dir_v2()
        self._initialize(model_dir, captured)
        self.assertEqual(
            Path(captured[0]["Det.model_path"]).resolve(),
            (model_dir / "PP-OCRv6_det_small.onnx").resolve(),
        )

    def test_rec_path_reaches_constructor(self) -> None:
        captured: list = []
        model_dir = _model_dir_v2()
        self._initialize(model_dir, captured)
        self.assertEqual(
            Path(captured[0]["Rec.model_path"]).resolve(),
            (model_dir / "PP-OCRv6_rec_small.onnx").resolve(),
        )

    def test_paths_are_absolute(self) -> None:
        captured: list = []
        self._initialize(_model_dir_v2(), captured)
        self.assertTrue(Path(captured[0]["Det.model_path"]).is_absolute())
        self.assertTrue(Path(captured[0]["Rec.model_path"]).is_absolute())

    def test_internal_rapidocr_models_not_selected(self) -> None:
        captured: list = []
        self._initialize(_model_dir_v2(), captured)
        for key in ("Det.model_path", "Rec.model_path"):
            path = captured[0][key].replace("\\", "/")
            self.assertNotIn("rapidocr/models/", path)

    def test_missing_det_fails_closed(self) -> None:
        from ocr import OCRError

        model_dir = _model_dir_v2()
        (model_dir / "PP-OCRv6_det_small.onnx").unlink()
        with self.assertRaises(OCRError) as ctx:
            self._initialize(model_dir, [])
        self.assertEqual(ctx.exception.code, "model_assets_missing")

    def test_missing_rec_fails_closed(self) -> None:
        from ocr import OCRError

        model_dir = _model_dir_v2()
        (model_dir / "PP-OCRv6_rec_small.onnx").unlink()
        with self.assertRaises(OCRError) as ctx:
            self._initialize(model_dir, [])
        self.assertEqual(ctx.exception.code, "model_assets_missing")

    def test_embedded_dictionary_no_rec_keys_path(self) -> None:
        captured: list = []
        self._initialize(_model_dir_v2(), captured)
        self.assertNotIn("Rec.rec_keys_path", captured[0])

    def test_classifier_disabled_and_explicit(self) -> None:
        captured: list = []
        self._initialize(_model_dir_v2(), captured)
        self.assertIs(captured[0]["Global.use_cls"], False)
        self.assertIn("Cls.model_path", captured[0])
        self.assertTrue(Path(captured[0]["Cls.model_path"]).is_absolute())

    def test_no_model_root_dir_param(self) -> None:
        captured: list = []
        self._initialize(_model_dir_v2(), captured)
        self.assertNotIn("Global.model_root_dir", captured[0])

    def test_engine_init_failure_not_swallowed(self) -> None:
        from ocr import OCRError

        with self.assertRaises(OCRError) as ctx:
            self._initialize(_model_dir_v2(), [], fail=True)
        self.assertEqual(ctx.exception.code, "engine_init_failed")

    def test_model_path_not_applied_fails_closed(self) -> None:
        from ocr import OCRError

        with self.assertRaises(OCRError) as ctx:
            self._initialize(_model_dir_v2(), [], wrong_path=True)
        self.assertEqual(ctx.exception.code, "model_path_not_applied")

    def test_engine_init_count_is_one(self) -> None:
        runtime = self._initialize(_model_dir_v2(), [])
        self.assertEqual(runtime.engine_init_count, 1)
        self.assertEqual(runtime.model_paths["classifier"], "disabled")


def _wheel_file(name: str) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="clarifydeck-wheel-"))
    path = directory / name
    path.write_bytes(b"dummy-wheel")
    return path


def _write_manifest(payload: dict) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="clarifydeck-bundle-")) / "runtime" / "ocr"
    directory.mkdir(parents=True)
    path = directory / "bundle_manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main(verbosity=2)
