#!/usr/bin/env python3
"""Phase 2C diagnostic: bounded capture producer start/run/stop.

Explicitly starts the producer, runs for a bounded duration, reports stats,
stops the producer, verifies STOPPED, and exits. Never runs unbounded by
default (max 60 s unless --allow-long).

Usage:
    python3 scripts/capture_producer_test.py --duration-sec 10 --fps 1
    python3 scripts/capture_producer_test.py --mock --duration-sec 5 --fps 1 \
        --consumer-delay-ms 2000
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import gamescope_capture  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402
from capture.producer import CaptureProducer  # noqa: E402


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


class MockCapture:
    def __init__(self) -> None:
        self._seq = 0

    def capture_frame(self, mode: str = "base_plane_only", timeout: float = 5.0, debug_copy=None) -> CaptureFrame:
        self._seq += 1
        time.sleep(0.05)
        return CaptureFrame.from_png(
            _png(1280, 800), sequence=self._seq, source_backend="mock", source_mode=mode
        )


async def run(args) -> int:
    queue = LatestFrameQueue()
    if args.mock:
        capture = MockCapture()
    else:
        capture = gamescope_capture.GamescopeCapture(logger=lambda m: print(m, file=sys.stderr))
    producer = CaptureProducer(
        capture,
        queue,
        target_fps=args.fps,
        logger=lambda m: print(m, file=sys.stderr),
    )

    consumed = {"n": 0, "seqs": []}

    async def consumer() -> None:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            consumed["n"] += 1
            consumed["seqs"].append(frame.sequence)
            if args.consumer_delay_ms:
                await asyncio.sleep(args.consumer_delay_ms / 1000.0)

    consumer_task = None
    if not args.no_consume:
        consumer_task = asyncio.create_task(consumer())

    await producer.start()
    deadline = time.monotonic() + args.duration_sec
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        status = producer.status()
        print(
            f"[capture-producer-test] state={status['state']} attempted={status['frames_attempted']} "
            f"ok={status['frames_succeeded']} failed={status['frames_failed']} "
            f"last_seq={status['last_sequence']} avg_ms={status['avg_capture_ms']}"
        )
        if status["state"] == "FAILED":
            break

    final = await producer.stop()
    if consumer_task is not None:
        consumer_task.cancel()
        try:
            await consumer_task
        except BaseException:
            pass

    print(f"[capture-producer-test] final_state={final['state']}")
    print(
        f"[capture-producer-test] frames_attempted={final['frames_attempted']} "
        f"succeeded={final['frames_succeeded']} failed={final['frames_failed']} "
        f"late_ticks={final['late_ticks']}"
    )
    print(f"[capture-producer-test] queue={final['queue']}")
    print(f"[capture-producer-test] consumed_sequences={consumed['seqs']}")
    ok = final["state"] == "STOPPED" and final["queue"]["max_pending"] <= 1
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck capture producer diagnostic")
    parser.add_argument("--duration-sec", type=float, default=10.0)
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--consumer-delay-ms", type=int, default=0)
    parser.add_argument("--no-consume", action="store_true")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--allow-long", action="store_true")
    args = parser.parse_args(argv)
    if args.duration_sec <= 0:
        print("--duration-sec must be > 0")
        return 2
    if args.duration_sec > 60 and not args.allow_long:
        print("--duration-sec clamped to 60 (use --allow-long to override)")
        args.duration_sec = 60.0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
