#!/usr/bin/env python3
"""Phase 2C tests: CaptureProducer lifecycle, cadence, failures, shutdown.

Deterministic: a fake clock/sleep and an inline capture runner are injected so
no real time or Wayland is needed.

Run:
    python3 scripts/test_capture_producer.py
"""

from __future__ import annotations

import asyncio
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402
from capture.producer import CaptureProducer, ProducerState  # noqa: E402


def _png(width: int = 8, height: int = 8) -> bytes:
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


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.t += delay
        await asyncio.sleep(0)


class FakeCapture:
    def __init__(self, ft: FakeTime, capture_ms: float = 0.0, fail_all: bool = False, fail_every: int = 0) -> None:
        self.ft = ft
        self.capture_ms = capture_ms
        self.fail_all = fail_all
        self.fail_every = fail_every
        self.calls = 0
        self.seq = 0

    def capture_frame(self, mode: str = "base_plane_only", timeout: float = 5.0, debug_copy=None) -> CaptureFrame:
        self.calls += 1
        self.ft.t += self.capture_ms / 1000.0
        if self.fail_all or (self.fail_every and self.calls % self.fail_every == 0):
            raise CaptureError("capture_failed")
        self.seq += 1
        return CaptureFrame.from_png(
            _png(), sequence=self.seq, source_backend="mock", source_mode=mode
        )


async def _inline_runner(fn, *args):
    return fn(*args)


def make_producer(ft: FakeTime, capture: FakeCapture, queue: LatestFrameQueue, **kwargs) -> CaptureProducer:
    return CaptureProducer(
        capture,
        queue,
        clock=ft.clock,
        sleep=ft.sleep,
        runner=_inline_runner,
        **kwargs,
    )


async def _advance(ft: FakeTime, until_ms: float, step_ms: float = 25.0) -> None:
    """Advance the fake clock deterministically, independent of the producer."""
    while ft.t < until_ms:
        ft.t = min(until_ms, ft.t + step_ms / 1000.0)
        await asyncio.sleep(0)


async def _turns(count: int) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


class StateMachineTest(unittest.TestCase):
    def test_initial_state_stopped(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            producer = make_producer(ft, FakeCapture(ft), LatestFrameQueue())
            self.assertEqual(producer.status()["state"], "STOPPED")
            self.assertIsNone(producer._task)

        asyncio.run(run())

    def test_start_then_stop(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            producer = make_producer(ft, FakeCapture(ft), LatestFrameQueue())
            status = await producer.start()
            self.assertEqual(status["state"], "RUNNING")
            stopped = await producer.stop()
            self.assertEqual(stopped["state"], "STOPPED")
            self.assertIsNone(producer._task)

        asyncio.run(run())

    def test_start_idempotency(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            producer = make_producer(ft, FakeCapture(ft), LatestFrameQueue())
            await producer.start()
            first_task = producer._task
            for _ in range(10):
                result = await producer.start()
            self.assertIs(producer._task, first_task)
            self.assertEqual(result["detail"], "already_running")
            await producer.stop()

        asyncio.run(run())

    def test_stop_idempotency(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            producer = make_producer(ft, FakeCapture(ft), LatestFrameQueue())
            result = await producer.stop()
            self.assertEqual(result["detail"], "already_stopped")
            self.assertEqual(result["state"], "STOPPED")

        asyncio.run(run())


class CadenceTest(unittest.TestCase):
    def test_deadline_scheduling(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            capture = FakeCapture(ft, capture_ms=400)
            producer = make_producer(ft, capture, LatestFrameQueue(), target_fps=1.0)
            await producer.start()
            await _turns(12)
            await producer.stop()
            # 1000 ms period - 400 ms capture => ~600 ms sleep
            self.assertTrue(ft.sleeps)
            self.assertLess(abs(ft.sleeps[0] - 0.6), 0.02)

        asyncio.run(run())

    def test_slow_capture_no_overlap(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            capture = FakeCapture(ft, capture_ms=1300)
            producer = make_producer(ft, capture, LatestFrameQueue(), target_fps=1.0)
            await producer.start()
            await _turns(12)
            status = producer.status()
            await producer.stop()
            self.assertGreaterEqual(status["late_ticks"], 1)
            # strictly sequential: one capture call per attempt, no overlap
            self.assertEqual(status["frames_attempted"], capture.calls)
            self.assertGreaterEqual(status["frames_succeeded"], 1)

        asyncio.run(run())


class QueueIntegrationTest(unittest.TestCase):
    def test_no_consumer_keeps_one_pending(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            queue = LatestFrameQueue()
            producer = make_producer(ft, FakeCapture(ft), queue, target_fps=2.0)
            await producer.start()
            await _advance(ft, 3000)
            await producer.stop()
            stats = queue.stats()
            self.assertEqual(stats.pending, 1)
            self.assertGreater(stats.replaced, 0)
            self.assertEqual(stats.max_pending, 1)

        asyncio.run(run())

    def test_slow_consumer(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            queue = LatestFrameQueue()
            producer = make_producer(ft, FakeCapture(ft), queue, target_fps=2.0)
            seen: list[int] = []

            async def consumer() -> None:
                while True:
                    frame = await queue.get()
                    seen.append(frame.sequence)
                    await ft.sleep(1.0)  # slower than the 0.5 s producer period

            await producer.start()
            consumer_task = asyncio.create_task(consumer())
            await _advance(ft, 3000)
            await producer.stop()
            consumer_task.cancel()
            try:
                await consumer_task
            except BaseException:
                pass
            self.assertEqual(seen, sorted(seen))
            self.assertGreater(queue.stats().replaced, 0)

        asyncio.run(run())

    def test_failed_capture_preserves_pending(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            queue = LatestFrameQueue()
            capture = FakeCapture(ft, fail_all=True)
            producer = make_producer(ft, capture, queue, max_consecutive_failures=2)
            # seed a valid pending frame directly
            seed = CaptureFrame.from_png(_png(), sequence=99, source_backend="mock", source_mode="base_plane_only")
            await queue.put_latest(seed)
            await producer.start()
            await _advance(ft, 3000)
            # producer hits the failure threshold and FAILED; pending seed untouched
            self.assertEqual(queue.stats().pending, 1)
            frame = await queue.get()
            self.assertEqual(frame.sequence, 99)
            await producer.stop()

        asyncio.run(run())


class FailureTest(unittest.TestCase):
    def test_single_failure_keeps_running(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            capture = FakeCapture(ft, fail_every=3)
            producer = make_producer(ft, capture, LatestFrameQueue(), max_consecutive_failures=5)
            await producer.start()
            await _advance(ft, 2500)
            status = producer.status()
            self.assertEqual(status["state"], "RUNNING")
            self.assertGreaterEqual(status["frames_failed"], 1)
            await producer.stop()

        asyncio.run(run())

    def test_consecutive_failure_threshold_fails(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            capture = FakeCapture(ft, fail_all=True)
            producer = make_producer(ft, capture, LatestFrameQueue(), max_consecutive_failures=3)
            await producer.start()
            await _advance(ft, 3000)
            status = producer.status()
            self.assertEqual(status["state"], "FAILED")
            self.assertEqual(status["frames_failed"], 3)
            calls_after_fail = capture.calls
            await _advance(ft, 3000)
            self.assertEqual(capture.calls, calls_after_fail)  # no auto restart
            await producer.stop()

        asyncio.run(run())

    def test_explicit_recovery_after_failure(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            capture = FakeCapture(ft, fail_all=True)
            producer = make_producer(ft, capture, LatestFrameQueue(), max_consecutive_failures=2)
            await producer.start()
            await _advance(ft, 2000)
            self.assertEqual(producer.status()["state"], "FAILED")
            await producer.reset()
            capture.fail_all = False
            await producer.start()
            await _advance(ft, 2000)
            self.assertEqual(producer.status()["state"], "RUNNING")
            await producer.stop()
            self.assertIsNone(producer._task)

        asyncio.run(run())


class ShutdownTest(unittest.TestCase):
    def test_stop_during_sleep(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            producer = make_producer(ft, FakeCapture(ft), LatestFrameQueue(), target_fps=1.0)
            await producer.start()
            await _advance(ft, 300)
            stopped = await producer.stop()
            self.assertEqual(stopped["state"], "STOPPED")

        asyncio.run(run())

    def test_rapid_toggle(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            producer = make_producer(ft, FakeCapture(ft), LatestFrameQueue())
            for _ in range(10):
                await producer.start()
                await producer.stop()
            self.assertEqual(producer.status()["state"], "STOPPED")
            self.assertIsNone(producer._task)

        asyncio.run(run())

    def test_backend_shutdown_clears_queue(self) -> None:
        async def run() -> None:
            ft = FakeTime()
            queue = LatestFrameQueue()
            producer = make_producer(ft, FakeCapture(ft), queue)
            await producer.start()
            await _advance(ft, 1500)
            await producer.stop()
            queue.clear()
            self.assertEqual(queue.pending, 0)

        asyncio.run(run())


class NoAutoStartTest(unittest.TestCase):
    def test_main_does_not_start_producer(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("async def _main(self)")
        end = source.index("async def _unload(self)")
        boot_body = source[start:end]
        self.assertNotIn("capture_producer", boot_body)
        self.assertNotIn("CaptureProducer", boot_body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
