#!/usr/bin/env python3
"""Bounded libpng-vs-stdlib PNG decode benchmark (Phase 2D.3).

Not a unit test; reports timings for manual inspection.

Run:
    python3 scripts/png_decode_benchmark.py
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import change_detector as cd  # noqa: E402


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


def _png_multi_filter(width: int, height: int, pixels: bytes, filters: list[int], channels: int) -> bytes:
    raw = bytearray()
    prev = None
    for y in range(height):
        row = pixels[y * width * channels : (y + 1) * width * channels]
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


def _best_ms(fn, iters: int) -> tuple[float, object]:
    best = None
    result = None
    for _ in range(iters):
        started = time.perf_counter()
        result = fn()
        elapsed = (time.perf_counter() - started) * 1000.0
        best = elapsed if best is None else min(best, elapsed)
    return best, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--iters", type=int, default=3)
    args = parser.parse_args()

    fixtures = [
        ("rgb_filter0", 3, [0]),
        ("rgb_filter_heavy", 3, [0, 1, 2, 3, 4]),
        ("rgba_filter_heavy", 4, [0, 1, 2, 3, 4]),
    ]
    for name, channels, filters in fixtures:
        pixels = bytes((i * 7 + (i // channels)) & 0xFF for i in range(args.width * args.height * channels))
        data = _png_multi_filter(args.width, args.height, pixels, filters, channels)
        structure = cd._parse_png(data)[0]
        std_ms, std = _best_ms(lambda: cd.decode_png_stdlib(data), args.iters)
        print(f"[bench] {name}: {args.width}x{args.height} channels={channels} idat_bytes={structure.idat_bytes}")
        print(f"[bench]   stdlib={std_ms:.2f} ms")
        if cd._LIBPNG.available:
            fast_ms, fast = _best_ms(lambda: cd.decode_png_fast(data, structure), args.iters)
            speedup = std_ms / fast_ms if fast_ms > 0 else float("inf")
            print(f"[bench]   libpng={fast_ms:.2f} ms speedup={speedup:.1f}x equal={fast.rgba == std.rgba}")
        else:
            print(f"[bench]   libpng unavailable ({cd._LIBPNG.reason})")
    if cd._LIBPNG.available:
        print(f"[bench] libpng_version={cd._LIBPNG.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
