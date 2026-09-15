#!/usr/bin/env python3
"""Phase 2E.2a tests: per-game ROI set/reset device-test harness CLI.

Run:
    python3 scripts/test_roi_config_harness.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import roi_test  # noqa: E402
from capture import recognition_roi as rr  # noqa: E402
from capture.roi import NormalizedROI  # noqa: E402

CUSTOM = (0.10, 0.70, 0.80, 0.20)
GAME_A = (0.10, 0.70, 0.80, 0.20)
GAME_B = (0.15, 0.65, 0.70, 0.25)


def _cfg() -> Path:
    return Path(tempfile.mkdtemp(prefix="clarifydeck-harness-")) / "recognition_roi.json"


def _run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = roi_test.main(argv)
    return code, out.getvalue()


def _set(cfg: Path, roi, app_id=None):
    argv = ["--set-active-roi", ",".join(str(v) for v in roi), "--roi-config", str(cfg)]
    if app_id is not None:
        argv += ["--app-id", str(app_id)]
    return _run(argv)


def _reset(cfg: Path, app_id=None):
    argv = ["--reset-active-roi", "--roi-config", str(cfg)]
    if app_id is not None:
        argv += ["--app-id", str(app_id)]
    return _run(argv)


def _active(cfg: Path, app_id=None):
    argv = ["--active-roi", "--roi-config", str(cfg)]
    if app_id is not None:
        argv += ["--app-id", str(app_id)]
    code, output = _run(argv)
    source = None
    normalized = None
    for line in output.splitlines():
        if line.startswith("[active-roi] source="):
            source = line.split("source=", 1)[1].split()[0]
        if "normalized=(" in line:
            inner = line.split("normalized=(", 1)[1].split(")", 1)[0]
            normalized = tuple(float(p) for p in inner.split(","))
    return code, source, normalized


class HarnessCliTest(unittest.TestCase):
    def test_parse_set_global(self) -> None:
        cfg = _cfg()
        code, output = _set(cfg, CUSTOM)
        self.assertEqual(code, 0)
        self.assertIn("ok=true app_id=None", output)
        self.assertEqual(rr.ROIConfigStore(cfg).get(None), NormalizedROI(*CUSTOM))

    def test_parse_set_per_game(self) -> None:
        cfg = _cfg()
        code, output = _set(cfg, GAME_A, "111111")
        self.assertEqual(code, 0)
        self.assertIn("ok=true app_id=111111", output)
        self.assertEqual(rr.ROIConfigStore(cfg).get("111111"), NormalizedROI(*GAME_A))

    def test_parse_reset_global(self) -> None:
        cfg = _cfg()
        _set(cfg, CUSTOM)
        code, output = _reset(cfg)
        self.assertEqual(code, 0)
        self.assertIn("ok=true app_id=None", output)
        self.assertIsNone(rr.ROIConfigStore(cfg).get(None))

    def test_parse_reset_per_game(self) -> None:
        cfg = _cfg()
        _set(cfg, GAME_A, "111111")
        code, output = _reset(cfg, "111111")
        self.assertEqual(code, 0)
        self.assertIn("ok=true app_id=111111", output)
        self.assertIsNone(rr.ROIConfigStore(cfg).get("111111"))

    def test_invalid_roi_rejected(self) -> None:
        cfg = _cfg()
        _set(cfg, CUSTOM)
        before = cfg.read_bytes()
        code, output = _set(cfg, (0.9, 0.9, 0.5, 0.5))
        self.assertNotEqual(code, 0)
        self.assertIn("ok=false", output)
        self.assertEqual(cfg.read_bytes(), before)  # no partial write


class HarnessPerGameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _cfg()
        _set(self.cfg, CUSTOM)  # global default

    def test_app_a_set(self) -> None:
        code, _ = _set(self.cfg, GAME_A, "111111")
        self.assertEqual(code, 0)
        self.assertEqual(rr.ROIConfigStore(self.cfg).get("111111"), NormalizedROI(*GAME_A))

    def test_app_b_set(self) -> None:
        code, _ = _set(self.cfg, GAME_B, "222222")
        self.assertEqual(code, 0)
        self.assertEqual(rr.ROIConfigStore(self.cfg).get("222222"), NormalizedROI(*GAME_B))

    def test_app_a_resolution(self) -> None:
        _set(self.cfg, GAME_A, "111111")
        code, source, normalized = _active(self.cfg, "111111")
        self.assertEqual(code, 0)
        self.assertEqual(source, rr.SOURCE_USER)
        self.assertEqual(normalized, GAME_A)

    def test_app_b_resolution(self) -> None:
        _set(self.cfg, GAME_B, "222222")
        code, source, normalized = _active(self.cfg, "222222")
        self.assertEqual(code, 0)
        self.assertEqual(source, rr.SOURCE_USER)
        self.assertEqual(normalized, GAME_B)

    def test_unknown_app_global_fallback(self) -> None:
        _set(self.cfg, GAME_A, "111111")
        _set(self.cfg, GAME_B, "222222")
        code, source, normalized = _active(self.cfg, "999999")
        self.assertEqual(code, 0)
        self.assertEqual(source, rr.SOURCE_USER)
        self.assertEqual(normalized, CUSTOM)

    def test_reset_a_global_fallback(self) -> None:
        _set(self.cfg, GAME_A, "111111")
        _reset(self.cfg, "111111")
        code, source, normalized = _active(self.cfg, "111111")
        self.assertEqual(code, 0)
        self.assertEqual(source, rr.SOURCE_USER)
        self.assertEqual(normalized, CUSTOM)

    def test_b_remains_after_reset_a(self) -> None:
        _set(self.cfg, GAME_A, "111111")
        _set(self.cfg, GAME_B, "222222")
        _reset(self.cfg, "111111")
        store = rr.ROIConfigStore(self.cfg)
        self.assertIsNone(store.get("111111"))
        self.assertEqual(store.get("222222"), NormalizedROI(*GAME_B))
        self.assertEqual(store.get(None), NormalizedROI(*CUSTOM))

    def test_config_remains_valid_json(self) -> None:
        _set(self.cfg, GAME_A, "111111")
        _set(self.cfg, GAME_B, "222222")
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertIn("111111", data["games"])
        self.assertIn("222222", data["games"])
        _reset(self.cfg, "111111")
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertNotIn("111111", data["games"])
        self.assertIn("222222", data["games"])

    def test_reset_global_falls_back(self) -> None:
        _reset(self.cfg)
        code, source, normalized = _active(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(source, rr.SOURCE_DEFAULT)
        self.assertEqual(normalized, (0.08, 0.62, 0.84, 0.32))


class HarnessNoSideEffectsTest(unittest.TestCase):
    def test_no_capture_or_renderer_startup(self) -> None:
        import inspect

        cfg = _cfg()
        _set(cfg, CUSTOM)
        _set(cfg, GAME_A, "111111")
        _active(cfg, "111111")
        _reset(cfg, "111111")
        # the harness never imports the Decky backend, so no capture producer or
        # overlay renderer can be constructed by a config-only operation
        self.assertNotIn("main", sys.modules)
        body = inspect.getsource(roi_test._apply_active_roi)
        for forbidden in ("GamescopeCapture", "CaptureProducer", "ClarifyDeckEngine", "capture_frame"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
