#!/usr/bin/env python3
"""Phase 2E.2 tests: recognition ROI config store, resolver, backend RPC, crop.

Run:
    python3 scripts/test_recognition_roi.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SETTINGS_DIR = tempfile.mkdtemp(prefix="clarifydeck-roi-settings-")
sys.modules.setdefault(
    "decky",
    types.SimpleNamespace(
        logger=logging.getLogger("clarifydeck-roi-test"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_SETTINGS_DIR=_SETTINGS_DIR,
        DECKY_PLUGIN_DIR=".",
        emit=lambda *args, **kwargs: None,
    ),
)

import main  # noqa: E402
from capture import recognition_roi as rr  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.roi import (  # noqa: E402
    DEFAULT_ROI,
    GameROIProfile,
    NormalizedROI,
    ROIProfileStore,
    SubtitleBandROI,
)

CUSTOM = NormalizedROI(0.10, 0.70, 0.80, 0.20)
GAME_A = NormalizedROI(0.12, 0.72, 0.70, 0.18)
GAME_B = NormalizedROI(0.20, 0.60, 0.60, 0.25)


def _tmp_path(name: str = "recognition_roi.json") -> Path:
    return Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / name


class ConfigStoreTest(unittest.TestCase):
    def test_missing_config_uses_defaults(self) -> None:
        store = rr.ROIConfigStore(_tmp_path())
        self.assertIsNone(store.get(None))
        self.assertIsNone(store.get("123"))
        self.assertIsNone(store.last_error)

    def test_valid_config_load(self) -> None:
        path = _tmp_path()
        path.write_text(
            json.dumps({"version": 1, "default_roi": {"x": 0.1, "y": 0.7, "width": 0.8, "height": 0.2}}),
            encoding="utf-8",
        )
        store = rr.ROIConfigStore(path)
        self.assertEqual(store.get(None), CUSTOM)

    def test_valid_per_game_override(self) -> None:
        path = _tmp_path()
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default_roi": None,
                    "games": {"123": {"roi": {"x": 0.12, "y": 0.72, "width": 0.7, "height": 0.18}}},
                }
            ),
            encoding="utf-8",
        )
        store = rr.ROIConfigStore(path)
        self.assertEqual(store.get("123"), GAME_A)

    def test_default_override_roundtrip(self) -> None:
        store = rr.ROIConfigStore(_tmp_path())
        store.set(None, CUSTOM)
        self.assertEqual(rr.ROIConfigStore(store.path).get(None), CUSTOM)

    def test_corrupt_json_safe_fallback(self) -> None:
        path = _tmp_path()
        path.write_text("{not json", encoding="utf-8")
        store = rr.ROIConfigStore(path)
        self.assertIsNone(store.get(None))
        self.assertEqual(store.last_error, "invalid_json")

    def test_invalid_roi_safe_fallback(self) -> None:
        path = _tmp_path()
        path.write_text(
            json.dumps({"version": 1, "default_roi": {"x": 5, "y": 0, "width": 0.5, "height": 0.5}}),
            encoding="utf-8",
        )
        store = rr.ROIConfigStore(path)
        self.assertIsNone(store.get(None))

    def test_atomic_save_leaves_no_temp_files(self) -> None:
        path = _tmp_path()
        store = rr.ROIConfigStore(path)
        store.set(None, CUSTOM)
        self.assertTrue(path.exists())
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_reset_removes_override(self) -> None:
        store = rr.ROIConfigStore(_tmp_path())
        store.set("123", GAME_A)
        store.reset("123")
        self.assertIsNone(store.get("123"))
        self.assertEqual(rr.ROIConfigStore(store.path).get("123"), None)

    def test_unknown_appid_falls_back(self) -> None:
        store = rr.ROIConfigStore(_tmp_path())
        store.set("123", GAME_A)
        self.assertIsNone(store.get("999"))

    def test_oversized_file_rejected(self) -> None:
        path = _tmp_path()
        path.write_bytes(b"{" + b"0" * (rr.MAX_CONFIG_BYTES + 10) + b"}")
        store = rr.ROIConfigStore(path)
        self.assertIsNone(store.get(None))
        self.assertEqual(store.last_error, "config_too_large")

    def test_unsupported_version_rejected(self) -> None:
        path = _tmp_path()
        path.write_text(json.dumps({"version": 99, "default_roi": {"x": 0.1, "y": 0.7, "width": 0.8, "height": 0.2}}), encoding="utf-8")
        store = rr.ROIConfigStore(path)
        self.assertIsNone(store.get(None))
        self.assertEqual(store.last_error, "unsupported_version:99")


class ResolverTest(unittest.TestCase):
    def _resolver(self, default=None, games=None, profiles=None) -> rr.ActiveROIResolver:
        store = rr.ROIConfigStore(_tmp_path())
        if default is not None:
            store.set(None, default)
        for app_id, roi in (games or {}).items():
            store.set(app_id, roi)
        profile_store = ROIProfileStore(overrides=profiles or [])
        return rr.ActiveROIResolver(store, profile_store=profile_store)

    def test_per_game_user_override_wins(self) -> None:
        resolver = self._resolver(
            default=CUSTOM,
            games={"123": GAME_A},
            profiles=[GameROIProfile(app_id="123", roi=GAME_B, label="preset")],
        )
        active = resolver.resolve("123")
        self.assertEqual(active.roi, GAME_A)
        self.assertEqual(active.source, rr.SOURCE_USER)

    def test_game_preset_next(self) -> None:
        resolver = self._resolver(
            default=CUSTOM,
            profiles=[GameROIProfile(app_id="123", roi=GAME_B, label="preset")],
        )
        active = resolver.resolve("123")
        self.assertEqual(active.roi, GAME_B)
        self.assertEqual(active.source, rr.SOURCE_GAME_PROFILE)

    def test_global_user_default_next(self) -> None:
        resolver = self._resolver(default=CUSTOM)
        active = resolver.resolve("123")
        self.assertEqual(active.roi, CUSTOM)
        self.assertEqual(active.source, rr.SOURCE_USER)

    def test_builtin_default_last(self) -> None:
        resolver = self._resolver()
        active = resolver.resolve(None)
        self.assertEqual(active.roi, DEFAULT_ROI)
        self.assertEqual(active.source, rr.SOURCE_DEFAULT)

    def test_returned_roi_is_immutable(self) -> None:
        resolver = self._resolver(default=CUSTOM)
        active = resolver.resolve(None)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            active.roi.x = 0.5  # type: ignore[misc]

    def test_source_correct(self) -> None:
        self.assertEqual(self._resolver().resolve("x").source, rr.SOURCE_DEFAULT)
        self.assertEqual(self._resolver(default=CUSTOM).resolve("x").source, rr.SOURCE_USER)


class CropTest(unittest.TestCase):
    def _resolver(self, roi: NormalizedROI) -> rr.ActiveROIResolver:
        store = rr.ROIConfigStore(_tmp_path())
        store.set(None, roi)
        return rr.ActiveROIResolver(store)

    def test_resolves_1280x800(self) -> None:
        resolver = self._resolver(CUSTOM)
        frame = bytes(1280 * 800 * 4)
        roi_frame = rr.extract_recognition_roi(frame, 1280, 800, resolver=resolver)
        self.assertEqual((roi_frame.roi.x, roi_frame.roi.y), (128, 560))
        self.assertEqual((roi_frame.roi.width, roi_frame.roi.height), (1024, 160))

    def test_resolves_1280x720(self) -> None:
        resolver = self._resolver(CUSTOM)
        frame = bytes(1280 * 720 * 4)
        roi_frame = rr.extract_recognition_roi(frame, 1280, 720, resolver=resolver)
        self.assertEqual((roi_frame.roi.x, roi_frame.roi.y), (128, 504))
        self.assertEqual((roi_frame.roi.width, roi_frame.roi.height), (1024, 144))

    def test_resolves_1920x1080(self) -> None:
        resolver = self._resolver(CUSTOM)
        frame = bytes(1920 * 1080 * 4)
        roi_frame = rr.extract_recognition_roi(frame, 1920, 1080, resolver=resolver)
        self.assertEqual((roi_frame.roi.x, roi_frame.roi.y), (192, 756))
        self.assertEqual((roi_frame.roi.width, roi_frame.roi.height), (1536, 216))

    def test_exact_crop_bytes(self) -> None:
        width, height = 40, 30
        rgba = bytes((i * 7 + 1) & 0xFF for i in range(width * height * 4))
        roi = NormalizedROI(0.25, 0.5, 0.5, 0.25)  # -> (10, 15, 20, 8)
        resolver = self._resolver(roi)
        roi_frame = rr.extract_recognition_roi(rgba, width, height, resolver=resolver)
        self.assertEqual((roi_frame.roi.x, roi_frame.roi.y, roi_frame.roi.width, roi_frame.roi.height), (10, 15, 20, 8))
        expected = bytearray()
        for y in range(15, 23):
            start = (y * width + 10) * 4
            expected += rgba[start : start + 20 * 4]
        self.assertEqual(roi_frame.rgba, bytes(expected))

    def test_no_source_mutation(self) -> None:
        width, height = 40, 30
        rgba = bytearray((i * 3) & 0xFF for i in range(width * height * 4))
        snapshot = bytes(rgba)
        resolver = self._resolver(CUSTOM)
        rr.extract_recognition_roi(rgba, width, height, resolver=resolver)
        self.assertEqual(bytes(rgba), snapshot)

    def test_no_png_decode(self) -> None:
        import capture.change_detector as cd
        import capture.roi as roi_mod

        original_cd, original_roi = cd.decode_png_ex, roi_mod.decode_png_ex

        def explode(*args, **kwargs):
            raise AssertionError("decode must not be called")

        cd.decode_png_ex = explode
        roi_mod.decode_png_ex = explode
        try:
            resolver = self._resolver(CUSTOM)
            rr.extract_recognition_roi(bytes(40 * 30 * 4), 40, 30, resolver=resolver)
        finally:
            cd.decode_png_ex = original_cd
            roi_mod.decode_png_ex = original_roi


class BackendRpcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = main.ClarifyDeckEngine()
        self.engine._screen_width = 1280
        self.engine._screen_height = 800
        self.path = _tmp_path()
        self.engine.configure_roi_config(self.path)

    def test_get_default(self) -> None:
        result = self.engine.roi_config_get()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], rr.SOURCE_DEFAULT)
        self.assertEqual(result["roi"], rr.roi_to_dict(DEFAULT_ROI))

    def test_set_valid(self) -> None:
        result = self.engine.roi_config_set(rr.roi_to_dict(CUSTOM))
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], rr.SOURCE_USER)
        self.assertEqual(result["roi"], rr.roi_to_dict(CUSTOM))
        self.assertEqual(self.engine.roi_config_get()["roi"], rr.roi_to_dict(CUSTOM))

    def test_reject_invalid_position(self) -> None:
        result = self.engine.roi_config_set({"x": 0.9, "y": 0.0, "width": 0.5, "height": 0.5})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_roi")

    def test_reject_invalid_size(self) -> None:
        result = self.engine.roi_config_set({"x": 0.1, "y": 0.1, "width": 0.001, "height": 0.2})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_roi")

    def test_reject_malformed(self) -> None:
        result = self.engine.roi_config_set({"x": "abc"})  # type: ignore[dict-item]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_roi")

    def test_set_and_get_per_game(self) -> None:
        self.engine.roi_config_set(rr.roi_to_dict(GAME_A), "123")
        self.engine.roi_config_set(rr.roi_to_dict(GAME_B), "456")
        self.assertEqual(self.engine.roi_config_get("123")["roi"], rr.roi_to_dict(GAME_A))
        self.assertEqual(self.engine.roi_config_get("456")["roi"], rr.roi_to_dict(GAME_B))
        self.assertEqual(self.engine.roi_config_get("999")["source"], rr.SOURCE_DEFAULT)

    def test_reset_per_game(self) -> None:
        self.engine.roi_config_set(rr.roi_to_dict(GAME_A), "123")
        result = self.engine.roi_config_reset("123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], rr.SOURCE_DEFAULT)

    def test_reset_default(self) -> None:
        self.engine.roi_config_set(rr.roi_to_dict(CUSTOM))
        result = self.engine.roi_config_reset()
        self.assertEqual(result["source"], rr.SOURCE_DEFAULT)
        self.assertEqual(result["roi"], rr.roi_to_dict(DEFAULT_ROI))

    def test_preview_does_not_persist(self) -> None:
        self.engine.roi_config_preview(rr.roi_to_dict(GAME_A))
        self.assertEqual(self.engine.roi_config_get()["source"], rr.SOURCE_DEFAULT)

    def test_backend_survives_corrupt_config(self) -> None:
        self.path.write_text("}{ corrupt", encoding="utf-8")
        engine = main.ClarifyDeckEngine()
        engine.configure_roi_config(self.path)
        result = engine.roi_config_get()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], rr.SOURCE_DEFAULT)
        self.assertEqual(result["config_error"], "invalid_json")

    def test_no_capture_or_renderer_auto_start(self) -> None:
        self.engine.roi_config_set(rr.roi_to_dict(CUSTOM))
        self.engine.roi_config_get("123")
        self.engine.roi_config_reset("123")
        self.assertIsNone(self.engine._capture_task)
        self.assertIsNone(self.engine._producer)
        self.assertIsNone(self.engine._overlay)
        self.assertIsNone(self.engine.overlay_status())
        self.assertEqual(self.engine.capture_producer_status()["state"], "STOPPED")

    def test_plugin_rpc_wiring(self) -> None:
        plugin = main.Plugin()

        async def scenario() -> None:
            result = await plugin.roi_config_set(rr.roi_to_dict(CUSTOM))
            self.assertTrue(result["ok"])
            fetched = await plugin.roi_config_get()
            self.assertEqual(fetched["roi"], rr.roi_to_dict(CUSTOM))
            reset = await plugin.roi_config_reset()
            self.assertEqual(reset["source"], rr.SOURCE_DEFAULT)

        asyncio.run(scenario())


class ConfigValidationTest(unittest.TestCase):
    def test_bounds_rules(self) -> None:
        for bad in (
            NormalizedROI(1.0, 0.0, 0.2, 0.2),
            NormalizedROI(0.0, 1.0, 0.2, 0.2),
            NormalizedROI(0.0, 0.0, 1.2, 0.2),
            NormalizedROI(0.0, 0.0, 0.2, 0.01),
        ):
            with self.subTest(roi=bad):
                with self.assertRaises(CaptureError):
                    rr.validate_user_roi(bad)

    def test_valid_roi_accepted(self) -> None:
        self.assertEqual(rr.validate_user_roi(CUSTOM), CUSTOM)

    def test_parse_roi_from_mapping(self) -> None:
        self.assertEqual(rr.parse_roi(rr.roi_to_dict(CUSTOM)), CUSTOM)

    def test_subtitle_band_remains_available(self) -> None:
        band = SubtitleBandROI(0.05, 0.35, 0.90, 0.45)
        self.assertEqual(band.as_tuple(), (0.05, 0.35, 0.90, 0.45))


if __name__ == "__main__":
    unittest.main(verbosity=2)
