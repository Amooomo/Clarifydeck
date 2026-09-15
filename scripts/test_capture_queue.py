#!/usr/bin/env python3
"""Phase 2B tests: CaptureFrame, LatestFrameQueue, source lifetime, concurrency.

Run:
    python3 scripts/test_capture_queue.py
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import gamescope_capture  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame, validate_image_bytes  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402


def _png(width: int, height: int) -> bytes:
    raw = bytearray()
    for _ in range(height):
        raw.append(0)
        raw += bytes([0, 0, 0]) * width

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _frame(seq: int, width: int = 1280, height: int = 800) -> CaptureFrame:
    return CaptureFrame.from_png(
        _png(width, height), sequence=seq, source_backend="mock", source_mode="base_plane_only"
    )


class CaptureFrameTest(unittest.TestCase):
    def test_valid_frame_creation(self) -> None:
        frame = _frame(1)
        self.assertEqual((frame.width, frame.height), (1280, 800))
        self.assertEqual(frame.format, "png")
        self.assertGreater(len(frame.encoded_bytes), 0)
        self.assertEqual(frame.sequence, 1)
        self.assertEqual(frame.source_backend, "mock")

    def test_empty_bytes_rejected(self) -> None:
        with self.assertRaises(CaptureError) as ctx:
            CaptureFrame.from_png(b"", sequence=1, source_backend="mock", source_mode="base_plane_only")
        self.assertEqual(ctx.exception.code, "invalid_frame")

    def test_metadata_derived_from_bytes(self) -> None:
        frame = CaptureFrame.from_png(
            _png(640, 480), sequence=3, source_backend="mock", source_mode="base_plane_only"
        )
        self.assertEqual((frame.width, frame.height), (640, 480))

    def test_sequence_must_be_positive(self) -> None:
        with self.assertRaises(CaptureError):
            CaptureFrame.from_png(
                _png(8, 8), sequence=0, source_backend="mock", source_mode="base_plane_only"
            )


class LatestFrameQueueTest(unittest.TestCase):
    def test_empty_put_get(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            frame = await queue.get()
            self.assertEqual(frame.sequence, 1)
            self.assertEqual(queue.stats().consumed, 1)

        asyncio.run(run())

    def test_replace_pending_frame(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            await queue.put_latest(_frame(2))
            frame = await queue.get()
            self.assertEqual(frame.sequence, 2)
            stats = queue.stats()
            self.assertEqual(stats.replaced, 1)
            self.assertEqual(stats.produced, 2)
            self.assertEqual(stats.max_pending, 1)

        asyncio.run(run())

    def test_many_updates_keep_only_latest(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            for seq in range(1, 101):
                await queue.put_latest(_frame(seq))
            self.assertEqual(queue.pending, 1)
            frame = await queue.get()
            self.assertEqual(frame.sequence, 100)
            self.assertEqual(queue.stats().replaced, 99)

        asyncio.run(run())

    def test_consumer_owned_frame_not_mutated(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            owned = await queue.get()
            await queue.put_latest(_frame(2))
            self.assertEqual(owned.sequence, 1)
            self.assertEqual(queue.pending, 1)
            nxt = await queue.get()
            self.assertEqual(nxt.sequence, 2)

        asyncio.run(run())

    def test_clear(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            queue.clear()
            self.assertEqual(queue.pending, 0)
            self.assertEqual(queue.stats().cleared, 1)

        asyncio.run(run())

    def test_failed_capture_leaves_pending_untouched(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(20))
            queue.note_capture_error()
            self.assertEqual(queue.pending, 1)
            frame = await queue.get()
            self.assertEqual(frame.sequence, 20)
            self.assertEqual(queue.stats().capture_errors, 1)

        asyncio.run(run())

    def test_producer_does_not_block_on_full_queue(self) -> None:
        async def run() -> None:
            import time

            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            started = time.monotonic()
            for seq in range(2, 12):
                await queue.put_latest(_frame(seq))
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(queue.pending, 1)

        asyncio.run(run())


class FakeClient:
    def __init__(self, version: int = 6) -> None:
        self.control_version = version

    def connect(self) -> None:
        return

    def take_screenshot(self, path: str, type_id: int, timeout: float) -> str:
        Path(path).write_bytes(_png(1280, 800))
        return path

    def close(self) -> None:
        return


class SourceLifetimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["CLARIFYDECK_CAPTURE_SOURCE_DIR"] = self._tmp.name
        self._orig = gamescope_capture.GamescopeCapture._client
        gamescope_capture.GamescopeCapture._client = lambda self: FakeClient()

    def tearDown(self) -> None:
        gamescope_capture.GamescopeCapture._client = self._orig
        self._tmp.cleanup()

    def test_frame_survives_source_deletion(self) -> None:
        capture = gamescope_capture.GamescopeCapture(display="gamescope-0")
        frame = capture.capture_frame()
        self.assertIsNotNone(frame.source_path)
        self.assertFalse(Path(frame.source_path).exists())
        info = validate_image_bytes(frame.encoded_bytes)
        self.assertEqual((info["width"], info["height"]), (1280, 800))

    def test_safe_unlink_tolerates_missing(self) -> None:
        missing = Path(self._tmp.name) / "does-not-exist.png"
        gamescope_capture._safe_unlink(missing)  # must not raise

    def test_durable_debug_copy(self) -> None:
        capture = gamescope_capture.GamescopeCapture(display="gamescope-0")
        debug = Path(self._tmp.name) / "durable.png"
        frame = capture.capture_frame(debug_copy=debug)
        self.assertTrue(debug.exists())
        self.assertEqual(debug.read_bytes(), frame.encoded_bytes)


class ConcurrencyTest(unittest.TestCase):
    def test_producer_faster_than_consumer(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            produced = {"n": 0}

            async def producer() -> None:
                for seq in range(1, 21):
                    await queue.put_latest(_frame(seq))
                    produced["n"] += 1
                    await asyncio.sleep(0.001)

            seen: list[int] = []

            async def consumer() -> None:
                while produced["n"] < 20 or queue.pending:
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue
                    seen.append(frame.sequence)
                    await asyncio.sleep(0.02)

            await asyncio.gather(producer(), consumer())
            stats = queue.stats()
            self.assertLessEqual(stats.max_pending, 1)
            self.assertGreater(stats.replaced, 0)
            self.assertEqual(seen, sorted(seen))
            self.assertEqual(seen[-1], 20)

        asyncio.run(run())

    def test_clear_on_shutdown(self) -> None:
        async def run() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            queue.clear()
            self.assertEqual(queue.pending, 0)

        asyncio.run(run())


class NoStartupProducerTest(unittest.TestCase):
    def test_main_does_not_start_capture_producer(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("async def _main(self)")
        end = source.index("async def _unload(self)")
        boot_body = source[start:end]
        self.assertNotIn("capture_frame", boot_body)
        self.assertNotIn("LatestFrameQueue", boot_body)
        self.assertNotIn("capture_loop", boot_body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
