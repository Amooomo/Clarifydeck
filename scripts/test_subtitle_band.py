#!/usr/bin/env python3
"""Phase 2E.1 tests: subtitle-band model, resolution, crop, isolation, pipeline.

Run:
    python3 scripts/test_subtitle_band.py
"""

from __future__ import annotations

import asyncio
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import roi_test  # noqa: E402
from capture import roi as R  # noqa: E402
from capture.change_detector import DEFAULT_THRESHOLD, FrameChangeDetector  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402

W, H = 320, 200
BROAD = R.resolve_roi(R.DEFAULT_ROI, W, H)  # (26,124,269,64)
BAND = R.resolve_subtitle_band(R.DEFAULT_SUBTITLE_BAND, BROAD.width, BROAD.height)  # (13,22,242,29)


def _png_from_rgb(rgb: bytearray, width: int = W, height: int = H) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgb[y * width * 3 : (y + 1) * width * 3]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _solid(value: int = 100, width: int = W, height: int = H) -> bytearray:
    return bytearray([value, value, value] * (width * height))


def _with_block(base: bytearray, x0: int, y0: int, bw: int, bh: int, value: int, width: int = W, height: int = H) -> bytearray:
    out = bytearray(base)
    for y in range(y0, min(height, y0 + bh)):
        for x in range(x0, min(width, x0 + bw)):
            o = (y * width + x) * 3
            out[o] = out[o + 1] = out[o + 2] = value
    return out


def _frame(seq: int, rgb: bytearray, width: int = W, height: int = H, captured: float = 0.0) -> CaptureFrame:
    return CaptureFrame(
        width=width,
        height=height,
        format="png",
        encoded_bytes=_png_from_rgb(rgb, width, height),
        captured_monotonic=captured,
        captured_wall_time=None,
        source_backend="mock",
        source_mode="base_plane_only",
        sequence=seq,
    )


def _outside_band_block(base: bytearray, seq: int = 0) -> bytearray:
    # inside broad (y 124..188) but above the band (band starts at frame y 146)
    return _with_block(base, 60, 128, 40, 12, 255)


def _inside_band_block(base: bytearray) -> bytearray:
    return _with_block(base, 120, 152, 25, 10, 255)


def _pair(frames):
    broad_det = R.ROIChangeDetector(roi=R.DEFAULT_ROI)
    band_det = R.ROIChangeDetector(roi=R.DEFAULT_ROI)
    results = []
    for frame in frames:
        broad_decision, band_decision, *_ = roi_test.process_frame(
            frame,
            broad_detector=broad_det,
            band_detector=band_det,
            broad=R.DEFAULT_ROI,
            band=R.DEFAULT_SUBTITLE_BAND,
        )
        results.append((broad_decision, band_decision))
    return results


class BandModelTest(unittest.TestCase):
    def test_valid_band(self) -> None:
        band = R.SubtitleBandROI(0.05, 0.35, 0.90, 0.45)
        self.assertEqual(band.as_tuple(), (0.05, 0.35, 0.90, 0.45))

    def test_nan_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.SubtitleBandROI(float("nan"), 0.0, 0.5, 0.5)

    def test_inf_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.SubtitleBandROI(0.0, float("inf"), 0.5, 0.5)

    def test_zero_size_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.SubtitleBandROI(0.0, 0.0, 0.0, 0.5)

    def test_negative_size_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.SubtitleBandROI(0.0, 0.0, 0.5, -0.5)

    def test_normalized_to_pixel_mapping(self) -> None:
        band = R.resolve_subtitle_band(R.SubtitleBandROI(0.25, 0.5, 0.5, 0.25), 400, 200)
        self.assertEqual(band.as_tuple(), (100, 100, 200, 50))

    def test_default_band_mapping(self) -> None:
        self.assertEqual(BAND.as_tuple(), (13, 22, 242, 29))

    def test_edge_clamp_safe(self) -> None:
        band = R.resolve_subtitle_band(R.SubtitleBandROI(0.9, 0.9, 0.5, 0.5), 100, 100)
        self.assertEqual(band.as_tuple(), (90, 90, 10, 10))
        self.assertLessEqual(band.x + band.width, 100)
        self.assertLessEqual(band.y + band.height, 100)

    def test_fully_outside_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.resolve_subtitle_band(R.SubtitleBandROI(2.0, 0.0, 0.1, 0.1), 269, 64)

    def test_invalid_broad_size_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            R.resolve_subtitle_band(R.DEFAULT_SUBTITLE_BAND, 0, 64)


class BandCropTest(unittest.TestCase):
    def _rgba(self, width: int = W, height: int = H) -> bytes:
        return bytes((i * 13 + 7) & 0xFF for i in range(width * height * 4))

    def test_exact_band_crop_bytes(self) -> None:
        rgba = self._rgba()
        broad_w, broad_h, broad_rgba = R.crop_rgba(rgba, W, H, BROAD)
        bw, bh, band_rgba = R.crop_rgba(broad_rgba, broad_w, broad_h, BAND)
        self.assertEqual((bw, bh), (BAND.width, BAND.height))
        expected = bytearray()
        for y in range(BAND.y, BAND.y + BAND.height):
            start = (y * broad_w + BAND.x) * 4
            expected += broad_rgba[start : start + BAND.width * 4]
        self.assertEqual(band_rgba, bytes(expected))

    def test_broad_to_band_mapping(self) -> None:
        frame = _frame(1, _solid())
        band_frame, broad_rgba = R.extract_subtitle_band(
            roi_test.roi_mod.decode_png_ex(frame.encoded_bytes).rgba,
            W,
            H,
            R.DEFAULT_ROI,
            R.DEFAULT_SUBTITLE_BAND,
            sequence=1,
            captured_monotonic=0.0,
        )
        self.assertEqual(band_frame.broad_roi.as_tuple(), BROAD.as_tuple())
        self.assertEqual(band_frame.band_roi.as_tuple(), BAND.as_tuple())
        self.assertEqual((band_frame.width, band_frame.height), (BAND.width, BAND.height))
        self.assertEqual(len(broad_rgba), BROAD.width * BROAD.height * 4)

    def test_no_source_mutation(self) -> None:
        rgba = bytearray(self._rgba())
        snapshot = bytes(rgba)
        R.crop_rgba(rgba, W, H, BROAD)
        self.assertEqual(bytes(rgba), snapshot)

    def test_odd_dimensions(self) -> None:
        width, height = 321, 201
        rgba = self._rgba(width, height)
        broad = R.resolve_roi(R.DEFAULT_ROI, width, height)
        bw, bh, broad_rgba = R.crop_rgba(rgba, width, height, broad)
        band = R.resolve_subtitle_band(R.DEFAULT_SUBTITLE_BAND, bw, bh)
        cw, ch, band_rgba = R.crop_rgba(broad_rgba, bw, bh, band)
        self.assertEqual((cw, ch), (band.width, band.height))
        self.assertEqual(len(band_rgba), band.width * band.height * 4)
        self.assertLessEqual(band.x + band.width, bw)
        self.assertLessEqual(band.y + band.height, bh)

    def test_edge_touching_band(self) -> None:
        rgba = self._rgba()
        broad_w, broad_h, broad_rgba = R.crop_rgba(rgba, W, H, BROAD)
        full = R.resolve_subtitle_band(R.SubtitleBandROI(0.0, 0.0, 1.0, 1.0), broad_w, broad_h)
        self.assertEqual(full.as_tuple(), (0, 0, broad_w, broad_h))
        cw, ch, out = R.crop_rgba(broad_rgba, broad_w, broad_h, full)
        self.assertEqual((cw, ch), (broad_w, broad_h))
        self.assertEqual(out, broad_rgba)


class BandProfileTest(unittest.TestCase):
    def test_default_profile_has_band(self) -> None:
        self.assertEqual(R.DEFAULT_PROFILE.subtitle_band, R.DEFAULT_SUBTITLE_BAND)
        self.assertIsInstance(R.DEFAULT_PROFILE.subtitle_band, R.SubtitleBandROI)

    def test_per_game_band_override(self) -> None:
        custom_band = R.SubtitleBandROI(0.1, 0.4, 0.8, 0.3)
        profile = R.GameROIProfile(
            app_id="12345",
            roi=R.NormalizedROI(0.1, 0.7, 0.8, 0.25),
            subtitle_band=custom_band,
            scale=1,
            label="custom",
        )
        store = R.ROIProfileStore(overrides=[profile])
        resolved = store.resolve("12345")
        self.assertEqual(resolved.subtitle_band, custom_band)
        self.assertEqual(resolved.roi, profile.roi)
        self.assertEqual(resolved.scale, 1)

    def test_unknown_appid_default_band(self) -> None:
        self.assertEqual(R.resolve_profile("999999").subtitle_band, R.DEFAULT_SUBTITLE_BAND)

    def test_malformed_band_fails_safely(self) -> None:
        with self.assertRaises(CaptureError):
            R.GameROIProfile(app_id="1", roi=R.DEFAULT_ROI, subtitle_band="not-a-band")


class BandDetectorTest(unittest.TestCase):
    def _band_detector(self) -> R.ROIChangeDetector:
        return R.ROIChangeDetector(roi=R.DEFAULT_ROI)

    def _classify(self, detector, seq, rgb, captured=0.0):
        frame = _frame(seq, rgb, captured=captured)
        decoded = roi_test.roi_mod.decode_png_ex(frame.encoded_bytes)
        band_frame, _ = R.extract_subtitle_band(
            decoded.rgba,
            W,
            H,
            R.DEFAULT_ROI,
            R.DEFAULT_SUBTITLE_BAND,
            sequence=seq,
            captured_monotonic=captured,
        )
        return detector.classify_subtitle_band(band_frame)

    def test_first_band_frame_changed(self) -> None:
        decision = self._classify(self._band_detector(), 1, _solid())
        self.assertTrue(decision.changed)
        self.assertEqual(decision.reason, "first_frame")

    def test_identical_band_unchanged(self) -> None:
        detector = self._band_detector()
        base = _solid()
        self._classify(detector, 1, base)
        decision = self._classify(detector, 2, base)
        self.assertFalse(decision.changed)
        self.assertAlmostEqual(decision.score, 0.0, places=6)

    def test_stale_band_rejected(self) -> None:
        detector = self._band_detector()
        self._classify(detector, 5, _solid())
        decision = self._classify(detector, 4, _solid(200))
        self.assertFalse(decision.changed)
        self.assertEqual(decision.reason, "stale_sequence")
        self.assertEqual(detector.stats().stale_rejected, 1)

    def test_reset_band_first_frame(self) -> None:
        detector = self._band_detector()
        self._classify(detector, 1, _solid())
        self._classify(detector, 2, _solid())
        detector.reset()
        decision = self._classify(detector, 3, _solid())
        self.assertTrue(decision.changed)
        self.assertEqual(decision.reason, "first_frame")

    def test_tiny_noise_unchanged(self) -> None:
        detector = self._band_detector()
        base = _solid()
        noisy = _with_block(base, 120, 155, 3, 3, 101)
        self._classify(detector, 1, base)
        decision = self._classify(detector, 2, noisy)
        self.assertFalse(decision.changed)


class BandIsolationTest(unittest.TestCase):
    def test_large_change_outside_band_ignored(self) -> None:
        base = _solid()
        moved = _outside_band_block(base)
        broad_decision, band_decision = _pair([_frame(1, base), _frame(2, moved)])[1]
        self.assertTrue(broad_decision.changed)
        self.assertFalse(band_decision.changed)
        self.assertEqual(band_decision.score, 0.0)

    def test_same_change_detected_by_broad(self) -> None:
        base = _solid()
        moved = _outside_band_block(base)
        broad_decision, _ = _pair([_frame(1, base), _frame(2, moved)])[1]
        self.assertTrue(broad_decision.changed)
        self.assertGreater(broad_decision.score, R.ROI_CHANGE_THRESHOLD)

    def test_subtitle_like_change_inside_band_detected(self) -> None:
        base = _solid()
        subtitle = _inside_band_block(base)
        broad_decision, band_decision = _pair([_frame(1, base), _frame(2, subtitle)])[1]
        self.assertTrue(band_decision.changed)
        self.assertGreater(band_decision.score, R.ROI_CHANGE_THRESHOLD)

        full = FrameChangeDetector()
        full.classify(_frame(1, base))
        self.assertLess(full.classify(_frame(2, subtitle)).score, DEFAULT_THRESHOLD)

    def test_mixed_outside_motion_and_subtitle_detected(self) -> None:
        base = _solid()
        mixed = _inside_band_block(_outside_band_block(base))
        broad_decision, band_decision = _pair([_frame(1, base), _frame(2, mixed)])[1]
        self.assertTrue(broad_decision.changed)
        self.assertTrue(band_decision.changed)

    def test_subtitle_disappears_detected(self) -> None:
        base = _solid()
        subtitle = _inside_band_block(base)
        _, band_present = _pair([_frame(1, base), _frame(2, subtitle)])[1]
        _, band_absent = _pair([_frame(1, subtitle), _frame(2, base)])[1]
        self.assertTrue(band_present.changed)
        self.assertTrue(band_absent.changed)

    def test_subtitle_reappears_detected(self) -> None:
        base = _solid()
        subtitle = _inside_band_block(base)
        results = _pair([_frame(1, base), _frame(2, subtitle), _frame(3, base), _frame(4, subtitle)])
        band_changes = [band.changed for _, band in results]
        self.assertEqual(band_changes, [True, True, True, True])


class BandPipelineTest(unittest.TestCase):
    def _run(self, frame):
        broad_det = R.ROIChangeDetector(roi=R.DEFAULT_ROI)
        band_det = R.ROIChangeDetector(roi=R.DEFAULT_ROI)
        return roi_test.process_frame(
            frame,
            broad_detector=broad_det,
            band_detector=band_det,
            broad=R.DEFAULT_ROI,
            band=R.DEFAULT_SUBTITLE_BAND,
        )

    def test_png_decoded_once(self) -> None:
        calls = []
        original = R.decode_png_ex

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        R.decode_png_ex = spy
        try:
            self._run(_frame(1, _solid()))
            self.assertEqual(len(calls), 1)
        finally:
            R.decode_png_ex = original

    def test_broad_crop_once_and_band_from_broad(self) -> None:
        crops = []
        original = R.crop_rgba

        def spy(rgba, width, height, roi):
            crops.append((width, height, roi.as_tuple()))
            return original(rgba, width, height, roi)

        R.crop_rgba = spy
        try:
            self._run(_frame(1, _solid()))
        finally:
            R.crop_rgba = original
        self.assertEqual(crops[0][:2], (W, H))  # broad crop from the full frame
        self.assertEqual(crops[1][:2], (BROAD.width, BROAD.height))  # band cropped from broad
        self.assertEqual(sum(1 for c in crops if c[:2] == (W, H)), 1)

    def test_band_derived_from_broad_not_frame(self) -> None:
        crops = []
        original = R.crop_rgba

        def spy(rgba, width, height, roi):
            crops.append((width, height, roi.as_tuple()))
            return original(rgba, width, height, roi)

        R.crop_rgba = spy
        try:
            self._run(_frame(1, _solid()))
        finally:
            R.crop_rgba = original
        self.assertEqual(crops[0][:2], (W, H))
        self.assertEqual(crops[1][:2], (BROAD.width, BROAD.height))
        self.assertNotIn((W, H, BAND.as_tuple()), crops)

    def test_detectors_reset(self) -> None:
        broad_det = R.ROIChangeDetector(roi=R.DEFAULT_ROI)
        band_det = R.ROIChangeDetector(roi=R.DEFAULT_ROI)
        for seq in (1, 2, 3):
            roi_test.process_frame(
                _frame(seq, _solid()),
                broad_detector=broad_det,
                band_detector=band_det,
                broad=R.DEFAULT_ROI,
                band=R.DEFAULT_SUBTITLE_BAND,
            )
        broad_det.reset()
        band_det.reset()
        self.assertEqual(broad_det.baseline_size, 0)
        self.assertEqual(band_det.baseline_size, 0)
        self.assertEqual(broad_det.stats().frames_seen, 0)
        self.assertEqual(band_det.stats().frames_seen, 0)

    def test_queue_bounded_and_cleared(self) -> None:
        async def scenario() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1, _solid()))
            await queue.put_latest(_frame(2, _solid()))
            self.assertEqual(queue.stats().max_pending, 1)
            self.assertEqual(queue.pending, 1)
            queue.clear()
            self.assertEqual(queue.pending, 0)

        asyncio.run(scenario())

    def test_no_auto_start(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("roichangedetector", source)
        self.assertNotIn("subtitle_band", source)
        self.assertNotIn("subtitleband", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
