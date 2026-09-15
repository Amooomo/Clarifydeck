#!/usr/bin/env python3
"""Phase 2E tests: ROI model, resolution, crop, preprocessing, ROI detector.

Run:
    python3 scripts/test_roi.py
"""

from __future__ import annotations

import dataclasses
import math
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import roi as R  # noqa: E402
from capture.change_detector import (  # noqa: E402
    DEFAULT_GRID,
    DEFAULT_THRESHOLD,
    FrameChangeDetector,
    decode_png_ex,
)
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402

W, H = 320, 200


def _png_from_rgb(rgb: bytearray) -> bytes:
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        raw += rgb[y * W * 3 : (y + 1) * W * 3]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _solid(value: int) -> bytearray:
    return bytearray([value, value, value] * (W * H))


def _with_block(base: bytearray, x0: int, y0: int, bw: int, bh: int, value: int) -> bytearray:
    out = bytearray(base)
    for y in range(y0, min(H, y0 + bh)):
        for x in range(x0, min(W, x0 + bw)):
            o = (y * W + x) * 3
            out[o] = out[o + 1] = out[o + 2] = value
    return out


def _frame(seq: int, rgb: bytearray, captured: float = 0.0) -> CaptureFrame:
    return CaptureFrame(
        width=W,
        height=H,
        format="png",
        encoded_bytes=_png_from_rgb(rgb),
        captured_monotonic=captured,
        captured_wall_time=None,
        source_backend="mock",
        source_mode="base_plane_only",
        sequence=seq,
    )


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ROIMathTest(unittest.TestCase):
    def test_normalized_resolves_correctly(self) -> None:
        roi = R.resolve_roi(R.NormalizedROI(0.25, 0.5, 0.5, 0.25), 400, 200)
        self.assertEqual(roi.as_tuple(), (100, 100, 200, 50))

    def test_default_roi_resolves_1280x800(self) -> None:
        roi = R.resolve_roi(R.DEFAULT_ROI, 1280, 800)
        self.assertEqual(roi.as_tuple(), (102, 496, 1075, 256))

    def test_default_roi_inside_frame(self) -> None:
        roi = R.resolve_roi(R.DEFAULT_ROI, 1280, 800)
        self.assertLessEqual(roi.x + roi.width, 1280)
        self.assertLessEqual(roi.y + roi.height, 800)

    def test_clamp_left_top(self) -> None:
        roi = R.resolve_roi(R.NormalizedROI(-0.25, -0.25, 0.5, 0.5), 100, 100)
        self.assertEqual((roi.x, roi.y), (0, 0))
        self.assertEqual((roi.width, roi.height), (50, 50))

    def test_clamp_right_bottom(self) -> None:
        roi = R.resolve_roi(R.NormalizedROI(0.9, 0.9, 0.5, 0.5), 100, 100)
        self.assertEqual(roi.as_tuple(), (90, 90, 10, 10))

    def test_reject_zero_size(self) -> None:
        with self.assertRaises(CaptureError):
            R.NormalizedROI(0.1, 0.1, 0.0, 0.2)
        with self.assertRaises(CaptureError):
            R.NormalizedROI(0.1, 0.1, 0.2, -0.1)

    def test_reject_nan_inf(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(CaptureError):
                    R.NormalizedROI(bad, 0.1, 0.2, 0.2)

    def test_reject_fully_outside(self) -> None:
        with self.assertRaises(CaptureError):
            R.resolve_roi(R.NormalizedROI(2.0, 0.0, 0.1, 0.1), 100, 100)
        with self.assertRaises(CaptureError):
            R.resolve_roi(R.NormalizedROI(0.0, -3.0, 0.1, 0.1), 100, 100)

    def test_deterministic_rounding(self) -> None:
        self.assertEqual(R._round_half_up(0.5), 1)
        self.assertEqual(R._round_half_up(1.5), 2)
        self.assertEqual(R._round_half_up(2.5), 3)
        roi = R.NormalizedROI(0.25, 0.25, 0.5, 0.5)
        self.assertEqual(R.resolve_roi(roi, 100, 100), R.resolve_roi(roi, 100, 100))

    def test_invalid_frame_size(self) -> None:
        with self.assertRaises(CaptureError):
            R.resolve_roi(R.DEFAULT_ROI, 0, 100)


class ROICropTest(unittest.TestCase):
    def _rgba(self) -> bytes:
        return bytes((i * 13 + 7) & 0xFF for i in range(W * H * 4))

    def test_1x1_crop(self) -> None:
        rgba = self._rgba()
        roi = R.PixelROI(3, 4, 1, 1)
        cw, ch, out = R.crop_rgba(rgba, W, H, roi)
        self.assertEqual((cw, ch), (1, 1))
        o = (4 * W + 3) * 4
        self.assertEqual(out, rgba[o : o + 4])

    def test_full_frame_crop(self) -> None:
        rgba = self._rgba()
        cw, ch, out = R.crop_rgba(rgba, W, H, R.PixelROI(0, 0, W, H))
        self.assertEqual((cw, ch), (W, H))
        self.assertEqual(out, rgba)

    def test_middle_crop_exact_bytes(self) -> None:
        rgba = self._rgba()
        roi = R.PixelROI(10, 20, 30, 40)
        cw, ch, out = R.crop_rgba(rgba, W, H, roi)
        self.assertEqual((cw, ch), (30, 40))
        expected = bytearray()
        for y in range(20, 60):
            start = (y * W + 10) * 4
            expected += rgba[start : start + 30 * 4]
        self.assertEqual(out, bytes(expected))

    def test_first_last_row_boundaries(self) -> None:
        rgba = self._rgba()
        _, _, first = R.crop_rgba(rgba, W, H, R.PixelROI(0, 0, W, 1))
        self.assertEqual(first, rgba[: W * 4])
        _, _, last = R.crop_rgba(rgba, W, H, R.PixelROI(0, H - 1, W, 1))
        self.assertEqual(last, rgba[(H - 1) * W * 4 :])

    def test_invalid_buffer_length_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.crop_rgba(b"\x00" * 10, W, H, R.PixelROI(0, 0, 1, 1))

    def test_out_of_range_rejected(self) -> None:
        rgba = self._rgba()
        with self.assertRaises(CaptureError):
            R.crop_rgba(rgba, W, H, R.PixelROI(W - 1, 0, 5, 1))
        with self.assertRaises(CaptureError):
            R.crop_rgba(rgba, W, H, R.PixelROI(0, H - 1, 1, 5))
        with self.assertRaises(CaptureError):
            R.crop_rgba(rgba, W, H, R.PixelROI(-1, 0, 1, 1))


class PreprocessTest(unittest.TestCase):
    def test_scale_1_identity(self) -> None:
        rgba = bytes(4 * 3 * 4)
        w, h, out = R.preprocess_roi(rgba, 4, 3, 1)
        self.assertEqual((w, h), (4, 3))
        self.assertEqual(out, rgba)

    def test_scale_2_3_deferred(self) -> None:
        rgba = bytes(4 * 3 * 4)
        for scale in (2, 3):
            with self.subTest(scale=scale):
                with self.assertRaises(CaptureError) as ctx:
                    R.preprocess_roi(rgba, 4, 3, scale)
                self.assertEqual(ctx.exception.code, "scale_unavailable")

    def test_invalid_scale_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.preprocess_roi(bytes(8), 2, 1, 4)
        with self.assertRaises(CaptureError):
            R.preprocess_roi(bytes(8), 2, 1, 0)

    def test_scale_supported(self) -> None:
        self.assertTrue(R.scale_supported(1))
        self.assertFalse(R.scale_supported(2))


class ROIProfileTest(unittest.TestCase):
    def test_default_profile(self) -> None:
        profile = R.resolve_profile(None)
        self.assertIsNone(profile.app_id)
        self.assertEqual(profile.roi, R.DEFAULT_ROI)

    def test_unknown_app_id_falls_back(self) -> None:
        self.assertEqual(R.resolve_profile("999999").roi, R.DEFAULT_ROI)

    def test_exact_app_id_override(self) -> None:
        custom = R.GameROIProfile(
            app_id="12345", roi=R.NormalizedROI(0.1, 0.7, 0.8, 0.25), scale=1, label="custom"
        )
        store = R.ROIProfileStore(overrides=[custom])
        self.assertEqual(store.resolve("12345").label, "custom")
        self.assertEqual(store.resolve("12345").scale, 1)
        self.assertEqual(store.resolve("other").label, "default")

    def test_profile_scale_validation(self) -> None:
        with self.assertRaises(CaptureError):
            R.GameROIProfile(app_id="1", roi=R.DEFAULT_ROI, scale=5)

    def test_default_store_has_no_overrides(self) -> None:
        self.assertEqual(R.DEFAULT_PROFILE_STORE.overrides, {})


class ROIDecodeCropTest(unittest.TestCase):
    def test_crop_from_decoded_frame(self) -> None:
        decoded = decode_png_ex(_png_from_rgb(_solid(120)))
        roi = R.resolve_roi(R.DEFAULT_ROI, W, H)
        cw, ch, out = R.crop_rgba(decoded.rgba, decoded.width, decoded.height, roi)
        self.assertEqual((cw, ch), (roi.width, roi.height))
        self.assertEqual(len(out), roi.width * roi.height * 4)


class ROIChangeTest(unittest.TestCase):
    def test_first_roi_frame_changed(self) -> None:
        detector = R.ROIChangeDetector()
        decision = detector.classify(_frame(1, _solid(100)))
        self.assertTrue(decision.changed)
        self.assertEqual(decision.reason, "first_frame")

    def test_identical_roi_unchanged(self) -> None:
        detector = R.ROIChangeDetector()
        base = _solid(100)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, base))
        self.assertFalse(decision.changed)
        self.assertEqual(decision.reason, "below_threshold")
        self.assertAlmostEqual(decision.score, 0.0, places=6)

    def test_tiny_noise_unchanged(self) -> None:
        detector = R.ROIChangeDetector()
        base = _solid(100)
        noisy = _with_block(base, 140, 150, 3, 3, 101)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, noisy))
        self.assertFalse(decision.changed)

    def test_subtitle_like_change_detected(self) -> None:
        base = _solid(100)
        subtitle = _with_block(base, 140, 178, 25, 10, 255)

        full = FrameChangeDetector()
        full.classify(_frame(1, base))
        full_decision = full.classify(_frame(2, subtitle))
        self.assertLess(full_decision.score, DEFAULT_THRESHOLD)
        self.assertFalse(full_decision.changed)

        detector = R.ROIChangeDetector()
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, subtitle))
        self.assertGreater(decision.score, R.ROI_CHANGE_THRESHOLD)
        self.assertTrue(decision.changed)
        self.assertEqual(decision.reason, "changed")

    def test_large_roi_change_detected(self) -> None:
        detector = R.ROIChangeDetector()
        base = _solid(0)
        large = _with_block(base, 40, 130, 240, 50, 255)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, large))
        self.assertTrue(decision.changed)
        self.assertGreater(decision.score, 0.1)

    def test_stale_sequence_rejected(self) -> None:
        detector = R.ROIChangeDetector()
        detector.classify(_frame(5, _solid(0)))
        decision = detector.classify(_frame(4, _solid(200)))
        self.assertFalse(decision.changed)
        self.assertEqual(decision.reason, "stale_sequence")
        self.assertEqual(detector.stats().stale_rejected, 1)

    def test_baseline_updates_only_when_changed(self) -> None:
        detector = R.ROIChangeDetector()
        base = _solid(0)
        large = _with_block(base, 40, 130, 240, 50, 255)
        detector.classify(_frame(1, base))
        detector.classify(_frame(2, large))
        decision = detector.classify(_frame(3, large))
        self.assertFalse(decision.changed)

    def test_reset_returns_to_first_frame(self) -> None:
        detector = R.ROIChangeDetector()
        base = _solid(100)
        detector.classify(_frame(1, base))
        detector.classify(_frame(2, base))
        detector.reset()
        decision = detector.classify(_frame(3, base))
        self.assertTrue(decision.changed)
        self.assertEqual(decision.reason, "first_frame")

    def test_stats_populated(self) -> None:
        detector = R.ROIChangeDetector()
        detector.classify(_frame(1, _solid(100)))
        stats = detector.stats()
        self.assertEqual(stats.decoder_backend, "libpng" if R.decode_png_ex(_png_from_rgb(_solid(1))).decoder_backend == "libpng" else "stdlib")
        self.assertEqual(stats.roi_x, 26)
        self.assertEqual(stats.roi_y, 124)
        self.assertEqual(stats.roi_width, 269)
        self.assertEqual(stats.roi_height, 64)
        self.assertEqual(stats.roi_bytes, 269 * 64 * 4)
        self.assertIsNotNone(stats.crop_ms)
        self.assertIsNotNone(stats.roi_signature_ms)

    def test_bounded_state_after_1000_frames(self) -> None:
        detector = R.ROIChangeDetector()
        base = _solid(100)
        variant = _with_block(base, 140, 178, 25, 10, 255)
        for i in range(1000):
            detector.classify(_frame(i + 1, variant if i % 2 else base))
        self.assertEqual(detector.baseline_size, R.ROI_GRID[0] * R.ROI_GRID[1])
        for field in dataclasses.fields(detector.stats()):
            self.assertNotIsInstance(getattr(detector.stats(), field.name), (list, dict))

    def test_age_calculation(self) -> None:
        clock = FakeClock(10.5)
        detector = R.ROIChangeDetector(clock=clock)
        decision = detector.classify(_frame(1, _solid(100), captured=10.0))
        self.assertAlmostEqual(decision.age_ms, 500.0, delta=0.1)


class DebugExportTest(unittest.TestCase):
    def test_encode_roundtrip(self) -> None:
        rgba = bytes((i * 7 + 3) & 0xFF for i in range(16 * 8 * 4))
        png = R.encode_rgba_png(rgba, 16, 8)
        decoded = decode_png_ex(png)
        self.assertEqual((decoded.width, decoded.height), (16, 8))
        self.assertEqual(decoded.rgba, rgba)

    def test_invalid_buffer_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.encode_rgba_png(b"\x00" * 3, 4, 4)


class FrozenDefaultsRegressionTest(unittest.TestCase):
    def test_full_frame_defaults_unchanged(self) -> None:
        self.assertEqual(DEFAULT_THRESHOLD, 0.005)
        self.assertEqual(DEFAULT_GRID, (32, 20))
        self.assertNotEqual(R.ROI_GRID, DEFAULT_GRID)
        self.assertNotEqual(R.ROI_CHANGE_THRESHOLD, DEFAULT_THRESHOLD)

    def test_roi_threshold_band_calibrated(self) -> None:
        self.assertGreater(R.ROI_CHANGE_THRESHOLD, 0.003)
        self.assertLessEqual(R.ROI_CHANGE_THRESHOLD, 0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
