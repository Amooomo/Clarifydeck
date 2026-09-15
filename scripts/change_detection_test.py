#!/usr/bin/env python3
"""Phase 2D diagnostic: capture -> queue -> change detector.

Modes:
    --mock-static    identical frames -> first changed, rest unchanged
    --mock-changing  alternating frames -> repeated changes
    --live           real gamescope base_plane_only frames

Bounded; exits automatically; clears the queue and resets the detector on stop.

Usage:
    python3 scripts/change_detection_test.py --mock-static --duration-sec 5 --fps 2
    python3 scripts/change_detection_test.py --live --duration-sec 15 --fps 1 --threshold 0.02
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
from capture.change_detector import DEFAULT_THRESHOLD, FrameChangeDetector  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402
from capture.producer import CaptureProducer  # noqa: E402


def _png(width: int, height: int, variant: int = 0) -> bytes:
    row_black = bytes([0, 0, 0]) * width
    row_white = bytes([255, 255, 255]) * width
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        if variant == 0:
            raw += row_black
        elif variant == 1 and height - 30 <= y < height - 20:
            raw += row_black[: width // 2 * 3] + row_white[width // 2 * 3 :]
        elif variant == 2 and y >= height // 2:
            raw += row_white
        else:
            raw += row_black

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
    def __init__(self, mode: str) -> None:
        self._seq = 0
        self._mode = mode

    def capture_frame(self, mode: str = "base_plane_only", timeout: float = 5.0, debug_copy=None) -> CaptureFrame:
        self._seq += 1
        if self._mode == "static":
            variant = 0
        elif self._mode == "subtitle":
            variant = 1 if self._seq > 1 else 0
        else:
            variant = 2 if self._seq % 2 == 0 else 0
        return CaptureFrame.from_png(
            _png(320, 200, variant),
            sequence=self._seq,
            source_backend="mock",
            source_mode=mode,
        )


async def run(args) -> int:
    queue = LatestFrameQueue()
    if args.live:
        capture = gamescope_capture.GamescopeCapture(logger=lambda m: print(m, file=sys.stderr))
    elif args.mock_changing:
        capture = MockCapture("changing")
    elif args.mock_subtitle:
        capture = MockCapture("subtitle")
    else:
        capture = MockCapture("static")

    producer = CaptureProducer(capture, queue, target_fps=args.fps)
    detector = FrameChangeDetector(threshold=args.threshold, collect_filters=args.debug)
    stop = asyncio.Event()
    decisions = []

    proc_start = time.process_time()
    wall_start = time.monotonic()
    loop = {"iterations": 0, "frames_received": 0, "empty_polls": 0, "wait_wakeups": 0}
    timing = {"detector_wall": 0.0, "detector_cpu": 0.0, "wait_wall": 0.0, "wait_cpu": 0.0}

    async def consumer() -> None:
        while True:
            if stop.is_set() and queue.pending == 0:
                return
            loop["iterations"] += 1
            w0 = time.monotonic()
            c0 = time.process_time()
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                loop["empty_polls"] += 1
                timing["wait_wall"] += time.monotonic() - w0
                timing["wait_cpu"] += time.process_time() - c0
                continue
            timing["wait_wall"] += time.monotonic() - w0
            timing["wait_cpu"] += time.process_time() - c0
            loop["wait_wakeups"] += 1
            loop["frames_received"] += 1
            d0 = time.monotonic()
            dc0 = time.process_time()
            decision = detector.classify(frame)
            timing["detector_wall"] += time.monotonic() - d0
            timing["detector_cpu"] += time.process_time() - dc0
            decisions.append(decision)
            if args.debug:
                print(
                    f"[change-detector] seq={decision.sequence} changed={decision.changed} "
                    f"score={decision.score:.4f} reason={decision.reason} age_ms={decision.age_ms:.1f}"
                )

    consumer_task = asyncio.create_task(consumer())
    await producer.start()
    await asyncio.sleep(args.duration_sec)
    await producer.stop()
    stop.set()
    await consumer_task

    wall_total = time.monotonic() - wall_start
    proc_cpu = time.process_time() - proc_start
    stats = detector.stats()
    queue_stats = queue.stats()
    queue.clear()
    detector.reset()

    print(
        f"[change-detector] seen={stats.frames_seen} changed={stats.changed} "
        f"unchanged={stats.unchanged} stale={stats.stale_rejected} "
        f"decode_errors={stats.decode_errors} threshold={stats.threshold}"
    )
    print(
        f"[change-detector] last_score={stats.last_score} "
        f"decode_ms={stats.decode_ms} signature_ms={stats.signature_ms} "
        f"compare_ms={stats.compare_ms} total_ms={stats.total_ms}"
    )
    print(
        f"[png] {stats.png_width}x{stats.png_height} bit_depth={stats.png_bit_depth} "
        f"color_type={stats.png_color_type} interlace={stats.png_interlace} "
        f"decoder_backend={stats.decoder_backend} fallback_reason={stats.decoder_fallback_reason} "
        f"libpng_version={stats.libpng_version}"
    )
    print(f"[png] filters={stats.png_filters}")
    print(
        f"[decode] parse_chunks_ms={stats.parse_chunks_ms} zlib_ms={stats.zlib_ms} "
        f"unfilter_ms={stats.unfilter_ms} pixel_expand_ms={stats.pixel_expand_ms} "
        f"decode_ms={stats.decode_ms} avg_decode_ms={stats.avg_decode_ms}"
    )
    print(
        f"[cpu] wall_total_sec={wall_total:.2f} process_cpu_total_sec={proc_cpu:.2f} "
        f"avg_process_cpu_pct={(proc_cpu / wall_total * 100.0) if wall_total else 0.0:.1f}"
    )
    print(
        f"[cpu] detector_wall_ms={timing['detector_wall']*1000:.1f} "
        f"detector_cpu_ms={timing['detector_cpu']*1000:.1f} "
        f"wait_wall_ms={timing['wait_wall']*1000:.1f} wait_cpu_ms={timing['wait_cpu']*1000:.1f}"
    )
    print(
        f"[loop] iterations={loop['iterations']} frames_received={loop['frames_received']} "
        f"empty_polls={loop['empty_polls']} wait_wakeups={loop['wait_wakeups']}"
    )
    tail = [(d.sequence, d.changed, round(d.score, 4), d.reason) for d in decisions[-12:]]
    print(f"[change-detector] recent={tail}")
    print(f"[change-detector] queue={queue_stats.__dict__}")

    ok = queue_stats.max_pending <= 1 and stats.decode_errors == 0 and stats.frames_seen >= 1
    if args.mock_static:
        ok = ok and stats.unchanged >= 1
    if args.mock_changing:
        ok = ok and stats.changed >= 2
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck change-detection diagnostic")
    parser.add_argument("--duration-sec", type=float, default=5.0)
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--mock-static", action="store_true")
    parser.add_argument("--mock-changing", action="store_true")
    parser.add_argument("--mock-subtitle", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--allow-long", action="store_true")
    args = parser.parse_args(argv)
    if args.duration_sec <= 0:
        print("--duration-sec must be > 0")
        return 2
    if args.duration_sec > 60 and not args.allow_long:
        print("--duration-sec clamped to 60 (use --allow-long to override)")
        args.duration_sec = 60.0
    if not (args.live or args.mock_static or args.mock_changing or args.mock_subtitle):
        args.mock_static = True
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
