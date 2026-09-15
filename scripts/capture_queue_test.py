#!/usr/bin/env python3
"""Phase 2B diagnostic: bounded LatestFrameQueue producer/consumer.

Proves the mailbox semantics on a real Steam Deck (or with mock frames):

    producer faster than consumer
    -> queue never exceeds 1 pending frame
    -> stale pending frames are replaced
    -> consumer sequences increase
    -> bounded run, exits automatically

Usage:
    python3 scripts/capture_queue_test.py --mock --frames 5 \
        --interval-ms 200 --consumer-delay-ms 700
    python3 scripts/capture_queue_test.py --frames 5 \
        --interval-ms 500 --consumer-delay-ms 1200 \
        --debug-copy /home/deck/clarifydeck-debug/queue.png
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import gamescope_capture  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
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


async def run(args) -> int:
    queue = LatestFrameQueue()
    capture = gamescope_capture.GamescopeCapture(logger=lambda m: print(m, file=sys.stderr))
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    produced = {"n": 0, "errors": 0}
    consumed = {"n": 0, "last_seq": 0, "seqs": []}

    async def producer() -> None:
        for index in range(args.frames):
            try:
                if args.mock:
                    frame = CaptureFrame.from_png(
                        _png(1280, 800),
                        sequence=index + 1,
                        source_backend="mock",
                        source_mode="base_plane_only",
                    )
                else:
                    frame = await loop.run_in_executor(
                        None,
                        capture.capture_frame,
                        "base_plane_only",
                        5.0,
                        Path(args.debug_copy) if args.debug_copy else None,
                    )
                await queue.put_latest(frame)
                produced["n"] += 1
            except Exception as exc:  # capture failure must not enqueue
                produced["errors"] += 1
                queue.note_capture_error()
                print(f"[capture-queue] capture error: {exc}", file=sys.stderr)
            await asyncio.sleep(args.interval_ms / 1000.0)
        stop.set()

    async def consumer() -> None:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                if stop.is_set():
                    return
                continue
            consumed["n"] += 1
            consumed["last_seq"] = frame.sequence
            consumed["seqs"].append(frame.sequence)
            if args.consumer_delay_ms:
                await asyncio.sleep(args.consumer_delay_ms / 1000.0)
            if stop.is_set() and queue.pending == 0:
                return

    await asyncio.gather(producer(), consumer())

    stats = queue.stats()
    print(
        f"[capture-queue] produced={stats.produced} replaced={stats.replaced} "
        f"consumed={stats.consumed} pending={stats.pending} max_pending={stats.max_pending} "
        f"errors={stats.capture_errors}"
    )
    print(f"[capture-queue] consumed_sequences={consumed['seqs']}")
    print(f"[capture-queue] last_consumed_sequence={consumed['last_seq']}")

    ok = stats.max_pending <= 1 and produced["errors"] == 0
    if not args.mock:
        ok = ok and produced["n"] >= 1
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck latest-frame queue diagnostic")
    parser.add_argument("--frames", type=int, default=5)
    parser.add_argument("--interval-ms", type=int, default=200)
    parser.add_argument("--consumer-delay-ms", type=int, default=700)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--debug-copy", default=None)
    args = parser.parse_args(argv)
    if args.frames <= 0:
        print("--frames must be > 0")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
