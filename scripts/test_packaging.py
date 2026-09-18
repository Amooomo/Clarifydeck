#!/usr/bin/env python3
"""Phase 2O.1 tests: production packaging boundary (explicit allowlist).

Deterministic, no real 400 MB runtime bundle: uses a tiny synthetic tree.

Run:
    python3 scripts/test_packaging.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import package_plugin as pk  # noqa: E402


def _touch(root: Path, rel: str, content: bytes = b"x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _fixture(root: Path) -> None:
    # Required runtime content.
    for rel in (
        "main.py",
        "overlay_manager.py",
        "backend_leader.py",
        "plugin.json",
        "package.json",
        "LICENSE",
        "README.md",
        "backend/ocr_worker.py",
        "backend/ocr_transport.py",
        "capture/producer.py",
        "capture/backend.py",
        "ocr/runtime.py",
        "ocr/bootstrap.py",
        "overlay/renderer.py",
        "overlay/protocol.py",
        "scripts/ocr_worker.py",
        "scripts/ocr_test.py",
        "runtime/ocr/bundle_manifest.json",
        "runtime/ocr/site-packages/rapidocr/__init__.py",
        "models/ppocrv6/PP-OCRv6_det_small.onnx",
        "models/ppocrv6/manifest.json",
        "lib/libtesseract.so.5.0.5",
        "share/tessdata/eng.traineddata",
        "bin/tesseract",
        "defaults/defaults.txt",
        "py_modules/.keep",
        "dist/index.js",
    ):
        _touch(root, rel)

    # Development / build / cache material that must never ship.
    for rel in (
        "node_modules/rollup/package.json",
        ".pnpm-store/v3/files/x",
        ".git/HEAD",
        ".vscode/tasks.json",
        "src/index.tsx",
        "tsconfig.json",
        "rollup.config.js",
        "pnpm-lock.yaml",
        ".npmrc",
        "environment.yml",
        "decky.pyi",
        "ARCHITECTURE_NOTES.md",
        "project_plan.md",
        "PHASE_1C2_AUDIT.md",
        "RECOVERY.md",
        "DEVELOPMENT_REQUIREMENTS.md",
        "assets/logo.png",
        "backend/Dockerfile",
        "backend/Makefile",
        "backend/entrypoint.sh",
        "backend/src/main.c",
        "scripts/capture_spike.py",
        "scripts/build_ocr_runtime_bundle.py",
        "scripts/test_ocr_runtime.py",
        "scripts/overlay_poc/overlay_poc.py",
        "capture/__pycache__/producer.cpython-313.pyc",
        "ocr/__pycache__/runtime.cpython-313.pyc",
        "dist/index.js.map",
        "runtime/ocr/wheels/rapidocr.whl",
    ):
        _touch(root, rel)


class ProductionAllowlistTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="clarifydeck-pkg-")
        self.root = Path(self._tmp.name)
        _fixture(self.root)
        self.addCleanup(self._tmp.cleanup)

    def _files(self):
        return set(pk.iter_production_files(self.root))

    def test_required_runtime_files_included(self) -> None:
        files = self._files()
        for rel in (
            "main.py",
            "backend/ocr_worker.py",
            "capture/producer.py",
            "ocr/runtime.py",
            "overlay/renderer.py",
            "scripts/ocr_worker.py",
            "scripts/ocr_test.py",
            "runtime/ocr/bundle_manifest.json",
            "runtime/ocr/site-packages/rapidocr/__init__.py",
            "models/ppocrv6/PP-OCRv6_det_small.onnx",
            "lib/libtesseract.so.5.0.5",
            "share/tessdata/eng.traineddata",
            "bin/tesseract",
            "dist/index.js",
        ):
            self.assertIn(rel, files, rel)

    def test_dev_and_cache_material_excluded(self) -> None:
        files = self._files()
        for rel in (
            "node_modules/rollup/package.json",
            ".pnpm-store/v3/files/x",
            ".git/HEAD",
            ".vscode/tasks.json",
            "src/index.tsx",
            "tsconfig.json",
            "rollup.config.js",
            "pnpm-lock.yaml",
            "environment.yml",
            "decky.pyi",
            "ARCHITECTURE_NOTES.md",
            "project_plan.md",
            "assets/logo.png",
            "backend/Dockerfile",
            "backend/Makefile",
            "backend/entrypoint.sh",
            "backend/src/main.c",
            "scripts/capture_spike.py",
            "scripts/build_ocr_runtime_bundle.py",
            "scripts/test_ocr_runtime.py",
            "scripts/overlay_poc/overlay_poc.py",
            "dist/index.js.map",
            "runtime/ocr/wheels/rapidocr.whl",
        ):
            self.assertNotIn(rel, files, rel)

    def test_no_python_caches_or_source_maps(self) -> None:
        for rel in self._files():
            self.assertNotIn("__pycache__", rel)
            self.assertFalse(rel.endswith(".pyc"), rel)
            self.assertFalse(rel.endswith(".map"), rel)

    def test_only_production_scripts_ship(self) -> None:
        scripts = {rel for rel in self._files() if rel.startswith("scripts/")}
        self.assertEqual(scripts, {"scripts/ocr_worker.py", "scripts/ocr_test.py"})

    def test_manifest_totals(self) -> None:
        manifest = pk.build_manifest(self.root)
        self.assertEqual(manifest["plugin"], "ClarifyDeck")
        self.assertEqual(manifest["file_count"], len(manifest["files"]))
        self.assertEqual(manifest["total_bytes"], sum(entry["bytes"] for entry in manifest["files"]))

    def test_stage_and_zip(self) -> None:
        dest = self.root / "out" / "ClarifyDeck"
        manifest = pk.stage(self.root, dest)
        self.assertTrue((dest / "main.py").is_file())
        self.assertTrue((dest / "runtime/ocr/site-packages/rapidocr/__init__.py").is_file())
        self.assertFalse((dest / "node_modules").exists())
        self.assertFalse((dest / "dist/index.js.map").exists())
        self.assertFalse((dest / "ARCHITECTURE_NOTES.md").exists())
        result = pk.make_zip(dest, self.root / "out" / "ClarifyDeck.zip")
        self.assertGreater(result["file_count"], 0)
        self.assertTrue((self.root / "out" / "ClarifyDeck.zip").is_file())
        self.assertEqual(manifest["file_count"], result["file_count"])

    def test_dry_run_does_not_create_output(self) -> None:
        out = self.root / "out"
        code = pk.main(["--source", str(self.root), "--out", str(out), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
