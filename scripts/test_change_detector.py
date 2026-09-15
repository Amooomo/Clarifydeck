#!/usr/bin/env python3
"""Phase 2D tests: freshness, change detector, bounded state, noise, pipeline.

Run:
    python3 scripts/test_change_detector.py
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import importlib.util
import io
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import change_detector as cd  # noqa: E402
from capture.change_detector import (  # noqa: E402
    DEFAULT_GRID,
    DEFAULT_THRESHOLD,
    FrameChangeDetector,
    decode_png,
    decode_png_ex,
    decode_png_fast,
    decode_png_stdlib,
    luminance_signature,
    signature_score,
)
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402

W, H = 320, 200


def _png_from_rgb(width: int, height: int, rgb: bytearray) -> bytes:
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
        encoded_bytes=_png_from_rgb(W, H, rgb),
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


class FreshnessTest(unittest.TestCase):
    def test_age_calculation(self) -> None:
        clock = FakeClock(10.5)
        detector = FrameChangeDetector(clock=clock)
        frame = _frame(1, _solid(100), captured=10.0)
        decision = detector.classify(frame)
        self.assertAlmostEqual(decision.age_ms, 500.0, delta=0.1)

    def test_negative_clock_clamps(self) -> None:
        clock = FakeClock(9.0)
        detector = FrameChangeDetector(clock=clock)
        frame = _frame(1, _solid(100), captured=10.0)
        decision = detector.classify(frame)
        self.assertEqual(decision.age_ms, 0.0)


class DetectorTest(unittest.TestCase):
    def test_first_frame_changed(self) -> None:
        detector = FrameChangeDetector()
        decision = detector.classify(_frame(1, _solid(100)))
        self.assertTrue(decision.changed)
        self.assertEqual(decision.reason, "first_frame")

    def test_identical_unchanged(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, base))
        self.assertFalse(decision.changed)
        self.assertEqual(decision.reason, "below_threshold")
        self.assertAlmostEqual(decision.score, 0.0, places=6)

    def test_tiny_brightness_unchanged(self) -> None:
        detector = FrameChangeDetector()  # calibrated default threshold
        base = _solid(100)
        brighter = bytearray([min(255, v + 1) for v in base])
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, brighter))
        self.assertFalse(decision.changed)

    def test_large_change_detected(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(0)
        large = _with_block(base, 0, 0, W, H // 2, 255)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, large))
        self.assertTrue(decision.changed)
        self.assertGreaterEqual(decision.score, 0.01)

    def test_changed_frame_updates_baseline(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(0)
        large = _with_block(base, 0, 0, W, H // 2, 255)
        detector.classify(_frame(1, base))
        detector.classify(_frame(2, large))
        decision = detector.classify(_frame(3, large))
        self.assertFalse(decision.changed)

    def test_stale_sequence_rejected(self) -> None:
        detector = FrameChangeDetector()
        detector.classify(_frame(5, _solid(0)))
        decision = detector.classify(_frame(4, _solid(200)))
        self.assertFalse(decision.changed)
        self.assertEqual(decision.reason, "stale_sequence")
        self.assertEqual(detector.stats().stale_rejected, 1)

    def test_reset_returns_to_first_frame(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        detector.classify(_frame(1, base))
        detector.classify(_frame(2, base))
        detector.reset()
        decision = detector.classify(_frame(3, base))
        self.assertTrue(decision.changed)
        self.assertEqual(decision.reason, "first_frame")


class SmallChangeTest(unittest.TestCase):
    def test_single_pixel_noise_unchanged(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        noisy = bytearray(base)
        o = (15 * W + 15) * 3  # a sampled cell center
        noisy[o] = noisy[o + 1] = noisy[o + 2] = 255
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, noisy))
        self.assertFalse(decision.changed)

    def test_subtitle_like_block_small_signal(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        subtitle = _with_block(base, 100, 180, 10, 10, 255)  # ~one grid cell
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, subtitle))
        # full-frame downsampling gives a small, sub-threshold signal for thin
        # subtitle-like text; ROI (Phase 2E) is the intended fix.
        self.assertGreater(decision.score, 0.0)
        self.assertLess(decision.score, DEFAULT_THRESHOLD)
        self.assertFalse(decision.changed)

    def test_large_scene_change(self) -> None:
        detector = FrameChangeDetector(threshold=0.02)
        base = _solid(0)
        scene = _with_block(base, 0, 0, W, H, 255)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, scene))
        self.assertTrue(decision.changed)


class ThresholdCalibrationTest(unittest.TestCase):
    """True-device-informed bands: static <=0.001, deliberate change > threshold."""

    def test_default_threshold_calibrated(self) -> None:
        self.assertGreaterEqual(DEFAULT_THRESHOLD, 0.003)
        self.assertLessEqual(DEFAULT_THRESHOLD, 0.005)
        self.assertEqual(FrameChangeDetector().threshold, DEFAULT_THRESHOLD)

    def test_static_noise_band_unchanged(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        noisy = bytearray(base)
        o = (15 * W + 15) * 3  # a sampled cell center
        noisy[o] = noisy[o + 1] = noisy[o + 2] = 255
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, noisy))
        self.assertLessEqual(decision.score, 0.001)
        self.assertFalse(decision.changed)

    def test_moderate_change_band_detected(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        moderate = _with_block(base, 0, 0, 80, 10, 255)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, moderate))
        self.assertGreater(decision.score, DEFAULT_THRESHOLD)
        self.assertTrue(decision.changed)

    def test_large_change_band_detected(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        large = _with_block(base, 0, 0, W, H, 255)
        detector.classify(_frame(1, base))
        decision = detector.classify(_frame(2, large))
        self.assertGreaterEqual(decision.score, 0.01)
        self.assertTrue(decision.changed)

    def test_post_change_static_settles(self) -> None:
        detector = FrameChangeDetector()
        base = _solid(100)
        changed = _with_block(base, 0, 0, 80, 10, 255)
        detector.classify(_frame(1, base))
        first = detector.classify(_frame(2, changed))
        self.assertTrue(first.changed)
        second = detector.classify(_frame(3, changed))
        self.assertFalse(second.changed)


def _filter_row(row: bytes, prev: bytes, bpp: int, filter_type: int) -> bytearray:
    out = bytearray(len(row))
    for i in range(len(row)):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i] if prev else 0
        c = prev[i - bpp] if prev and i >= bpp else 0
        if filter_type == 0:
            pred = 0
        elif filter_type == 1:
            pred = a
        elif filter_type == 2:
            pred = b
        elif filter_type == 3:
            pred = (a + b) // 2
        else:
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
        out[i] = (row[i] - pred) & 0xFF
    return out


def _png_multi_filter(width: int, height: int, rgb: bytes, filters: list[int], channels: int = 3) -> bytes:
    raw = bytearray()
    prev = None
    for y in range(height):
        row = rgb[y * width * channels : (y + 1) * width * channels]
        filter_type = filters[y % len(filters)]
        raw.append(filter_type)
        raw += _filter_row(row, prev, channels, filter_type)
        prev = row

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    color = 6 if channels == 4 else 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


class PngStructureTest(unittest.TestCase):
    def test_ihdr_parsed(self) -> None:
        data = _png_from_rgb(40, 30, _solid(100))
        structure, _idat = cd._parse_png(data)
        self.assertEqual((structure.width, structure.height), (40, 30))
        self.assertEqual(structure.bit_depth, 8)
        self.assertEqual(structure.color_type, 2)
        self.assertEqual(structure.interlace, 0)
        self.assertEqual(structure.bpp, 3)

    def test_filter_histogram(self) -> None:
        rgb = bytes([50, 60, 70]) * (W * H)
        data = _png_multi_filter(W, H, rgb, [0, 1, 2, 3, 4])
        result = decode_png_ex(data, collect_filters=True)
        histogram = result.structure.filter_histogram
        self.assertEqual(histogram.get(0), 40)
        self.assertEqual(histogram.get(1), 40)
        self.assertEqual(histogram.get(2), 40)
        self.assertEqual(histogram.get(3), 40)
        self.assertEqual(histogram.get(4), 40)


class FastDecoderTest(unittest.TestCase):
    def _assert_equivalent(self, data: bytes) -> None:
        if not cd._LIBPNG.available:
            self.skipTest("libpng simplified API not available")
        std = decode_png_stdlib(data)
        fast = decode_png_fast(data, cd._parse_png(data)[0])
        self.assertEqual((fast.width, fast.height), (std.width, std.height))
        self.assertEqual(fast.rgba, std.rgba)

    def test_stdlib_reference_multi_filter(self) -> None:
        rgb = bytes([10, 20, 30]) * (W * H)
        data = _png_multi_filter(W, H, rgb, [0, 1, 2, 3, 4])
        result = decode_png_stdlib(data)
        expected = bytearray()
        for i in range(W * H):
            expected += rgb[i * 3 : i * 3 + 3] + b"\xff"
        self.assertEqual(result.rgba, bytes(expected))

    def test_fast_matches_stdlib_rgb_filters(self) -> None:
        for filter_type in (0, 1, 2, 3, 4):
            with self.subTest(filter=filter_type):
                data = _png_multi_filter(W, H, bytes([9, 8, 7]) * (W * H), [filter_type])
                self._assert_equivalent(data)

    def test_fast_matches_stdlib_rgba_filters(self) -> None:
        for filter_type in (0, 1, 2, 3, 4):
            with self.subTest(filter=filter_type):
                rgba = bytes([9, 8, 7, 200]) * (W * H)
                data = _png_multi_filter(W, H, rgba, [filter_type], channels=4)
                self._assert_equivalent(data)

    def test_fast_matches_stdlib_mixed_filters(self) -> None:
        self._assert_equivalent(_png_multi_filter(W, H, bytes([9, 8, 7]) * (W * H), [0, 1, 2, 3, 4]))

    def test_malformed_fails_safely(self) -> None:
        bad_zlib = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
            + struct.pack(">I", 7)
            + b"IDAT"
            + b"notzlib"
            + struct.pack(">I", 0)
        )
        truncated_idat = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
            + struct.pack(">I", 100)
            + b"IDAT"
            + b"\x00" * 10
        )
        fixtures = {
            "bad_signature": b"\x89PNG\r\n\x1a\nnot a png",
            "truncated_ihdr": b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + b"\x00" * 5,
            "truncated_idat": truncated_idat,
            "bad_zlib": bad_zlib,
        }
        for name, data in fixtures.items():
            with self.subTest(fixture=name):
                with self.assertRaises(CaptureError):
                    decode_png_stdlib(data)
                if cd._LIBPNG.available:
                    with self.assertRaises(CaptureError):
                        decode_png_fast(data)

    def test_corrupt_dimensions_rejected(self) -> None:
        data = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">IIBBBBB", 0, 8, 8, 2, 0, 0, 0)
            + struct.pack(">I", 0)
            + b"IDAT"
            + struct.pack(">I", 0)
        )
        with self.assertRaises(CaptureError):
            cd._parse_png(data)

    def test_eligibility_gate(self) -> None:
        self.assertEqual(cd._native_ineligible_reason(cd._parse_png(_png_from_rgb(8, 8, _solid(1)))[0]), None)
        self.assertEqual(
            cd._native_ineligible_reason(replace_structure(_png_from_rgb(8, 8, _solid(1)), bit_depth=16)),
            "unsupported_bit_depth",
        )
        self.assertEqual(
            cd._native_ineligible_reason(replace_structure(_png_from_rgb(8, 8, _solid(1)), color_type=0)),
            "unsupported_color_type",
        )
        self.assertEqual(
            cd._native_ineligible_reason(replace_structure(_png_from_rgb(8, 8, _solid(1)), interlace=1)),
            "interlaced_png",
        )

    def test_ineligible_skips_native(self) -> None:
        data = _png_ihdr(8, 8, 16, 2, 0)
        calls = []
        original = cd.decode_png_fast

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        cd.decode_png_fast = spy
        try:
            with self.assertRaises(CaptureError):
                cd.decode_png_ex(data, backend="libpng")
            self.assertEqual(calls, [])
        finally:
            cd.decode_png_fast = original

    def test_native_begin_failure_falls_back(self) -> None:
        self._assert_native_failure_falls_back("native_begin_failed")

    def test_native_finish_failure_falls_back(self) -> None:
        self._assert_native_failure_falls_back("native_finish_failed")

    def _assert_native_failure_falls_back(self, code: str) -> None:
        original = cd.decode_png_fast
        saved_lib, saved_reason = cd._LIBPNG.lib, cd._LIBPNG.reason

        def boom(*args, **kwargs):
            raise CaptureError(code, "boom")

        cd.decode_png_fast = boom
        cd._LIBPNG.lib = object()
        cd._LIBPNG.reason = None
        try:
            result = cd.decode_png_ex(_png_from_rgb(8, 8, _solid(1)), backend="libpng")
            self.assertEqual(result.decoder_backend, "stdlib")
            self.assertEqual(result.decoder_fallback_reason, code)
        finally:
            cd.decode_png_fast = original
            cd._LIBPNG.lib, cd._LIBPNG.reason = saved_lib, saved_reason

    def test_missing_library_falls_back(self) -> None:
        saved_lib, saved_reason = cd._LIBPNG.lib, cd._LIBPNG.reason
        try:
            cd._LIBPNG.lib = None
            cd._LIBPNG.reason = "test_unavailable"
            result = decode_png_ex(_png_from_rgb(8, 8, _solid(1)))
            self.assertEqual(result.decoder_backend, "stdlib")
            self.assertEqual(result.decoder_fallback_reason, "test_unavailable")
        finally:
            cd._LIBPNG.lib, cd._LIBPNG.reason = saved_lib, saved_reason

    def test_symbol_missing_falls_back(self) -> None:
        saved_lib, saved_reason = cd._LIBPNG.lib, cd._LIBPNG.reason
        try:
            cd._LIBPNG.lib = None
            cd._LIBPNG.reason = "libpng_symbol_missing"
            result = cd.decode_png_ex(_png_from_rgb(8, 8, _solid(1)), backend="libpng")
            self.assertEqual(result.decoder_backend, "stdlib")
            self.assertEqual(result.decoder_fallback_reason, "libpng_symbol_missing")
        finally:
            cd._LIBPNG.lib, cd._LIBPNG.reason = saved_lib, saved_reason

    def test_fast_raises_when_library_missing(self) -> None:
        saved_lib, saved_reason = cd._LIBPNG.lib, cd._LIBPNG.reason
        try:
            cd._LIBPNG.lib = None
            cd._LIBPNG.reason = "libpng_unavailable"
            with self.assertRaises(CaptureError) as ctx:
                decode_png_fast(_png_from_rgb(8, 8, _solid(1)))
            self.assertEqual(ctx.exception.code, "libpng_unavailable")
        finally:
            cd._LIBPNG.lib, cd._LIBPNG.reason = saved_lib, saved_reason

    def test_library_loaded_once(self) -> None:
        first = cd._LIBPNG.lib
        decode_png_ex(_png_from_rgb(8, 8, _solid(1)))
        decode_png_ex(_png_from_rgb(8, 8, _solid(2)))
        self.assertIs(cd._LIBPNG.lib, first)

    def test_libpng_version_diagnostic(self) -> None:
        self.assertTrue(cd._LIBPNG.version is None or isinstance(cd._LIBPNG.version, str))


class PngAbiTest(unittest.TestCase):
    def test_field_offsets(self) -> None:
        self.assertEqual(cd._PngImage.opaque.offset, 0)
        self.assertEqual(cd._PngImage.version.offset, ctypes.sizeof(ctypes.c_void_p))

    def test_struct_size(self) -> None:
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(cd._PngImage), 104)

    def test_constants(self) -> None:
        self.assertEqual(cd.PNG_IMAGE_VERSION, 1)
        self.assertEqual(cd.PNG_FORMAT_RGB, 2)
        self.assertEqual(cd.PNG_FORMAT_RGBA, 3)


def _png_ihdr(width: int, height: int, bit_depth: int, color_type: int, interlace: int) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace))
        + chunk(b"IDAT", b"")
        + chunk(b"IEND", b"")
    )


def replace_structure(data: bytes, **changes):
    structure = cd._parse_png(data)[0]
    for key, value in changes.items():
        setattr(structure, key, value)
    return structure


class BoundedStateTest(unittest.TestCase):
    def test_no_frame_history_retained(self) -> None:
        detector = FrameChangeDetector()
        tw, th = 64, 40
        for seq in range(1, 1001):
            data = _png_from_rgb(tw, th, bytearray([seq % 256, seq % 256, seq % 256] * (tw * th)))
            frame = CaptureFrame(
                width=tw,
                height=th,
                format="png",
                encoded_bytes=data,
                captured_monotonic=0.0,
                captured_wall_time=None,
                source_backend="mock",
                source_mode="base_plane_only",
                sequence=seq,
            )
            detector.classify(frame)
        self.assertEqual(detector.baseline_size, DEFAULT_GRID[0] * DEFAULT_GRID[1])
        for value in vars(detector).values():
            if isinstance(value, list):
                self.assertFalse(any(isinstance(item, CaptureFrame) for item in value))
            self.assertNotIsInstance(value, CaptureFrame)

    def test_decode_and_signature_helpers(self) -> None:
        data = _png_from_rgb(W, H, _solid(128))
        width, height, rgba = decode_png(data)
        self.assertEqual((width, height), (W, H))
        sig = luminance_signature(rgba, width, height)
        self.assertEqual(len(sig), DEFAULT_GRID[0] * DEFAULT_GRID[1])
        self.assertEqual(signature_score(sig, sig), 0.0)


class PipelineTest(unittest.TestCase):
    def test_queue_integration_and_slow_detector(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            detector = FrameChangeDetector()
            seen: list[int] = []
            done = asyncio.Event()

            async def consumer() -> None:
                while True:
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        if done.is_set() and queue.pending == 0:
                            return
                        continue
                    seen.append(detector.classify(frame).sequence)

            task = asyncio.create_task(consumer())
            for seq in range(1, 21):
                await queue.put_latest(_frame(seq, _solid(seq % 256)))
            done.set()
            await task
            self.assertLessEqual(queue.stats().max_pending, 1)
            self.assertEqual(seen, sorted(seen))

        asyncio.run(run())

    def test_shutdown_clears_queue_and_resets_detector(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            detector = FrameChangeDetector()
            await queue.put_latest(_frame(1, _solid(10)))
            detector.classify(_frame(1, _solid(10)))
            self.assertEqual(queue.pending, 1)
            self.assertEqual(detector.baseline_size, DEFAULT_GRID[0] * DEFAULT_GRID[1])
            # Phase 2D pipeline shutdown boundary
            queue.clear()
            detector.reset()
            self.assertEqual(queue.pending, 0)
            self.assertEqual(detector.baseline_size, 0)

        asyncio.run(run())

    def test_stale_input_cannot_replace_baseline(self) -> None:
        detector = FrameChangeDetector()
        detector.classify(_frame(5, _solid(0)))
        baseline_before = detector.baseline_size
        detector.classify(_frame(1, _solid(255)))  # stale
        self.assertEqual(detector.baseline_size, baseline_before)


class NoAutoStartTest(unittest.TestCase):
    def test_main_does_not_start_detector(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("async def _main(self)")
        end = source.index("async def _unload(self)")
        boot_body = source[start:end]
        self.assertNotIn("ChangeDetector", boot_body)
        self.assertNotIn("change_detector", boot_body)


class ConsumerWaitingTest(unittest.TestCase):
    def test_empty_queue_blocks(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            task = asyncio.create_task(queue.get())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())  # blocked, not spinning
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

    def test_frame_arrival_wakes_consumer(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            task = asyncio.create_task(queue.get())
            await asyncio.sleep(0)
            await queue.put_latest(_frame(7, _solid(1)))
            frame = await task
            self.assertEqual(frame.sequence, 7)

        asyncio.run(run())

    def test_stop_wakes_waiting_consumer(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            stop = asyncio.Event()
            received: list[int] = []

            async def consumer() -> None:
                while True:
                    if stop.is_set() and queue.pending == 0:
                        return
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue
                    received.append(frame.sequence)

            task = asyncio.create_task(consumer())
            await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)
            self.assertEqual(received, [])

        asyncio.run(run())


class DiagnosticAttributionTest(unittest.TestCase):
    def test_diagnostic_emits_cpu_and_loop_metrics(self) -> None:
        path = ROOT / "scripts" / "change_detection_test.py"
        spec = importlib.util.spec_from_file_location("change_detection_test_cli", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.main(["--mock-static", "--duration-sec", "1.0", "--fps", "4"])
        output = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[cpu]", output)
        self.assertIn("[loop]", output)
        self.assertIn("process_cpu_total_sec=", output)
        self.assertIn("iterations=", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
