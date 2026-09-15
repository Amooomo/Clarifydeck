#!/usr/bin/env python3
"""Phase 2E.1 diagnostic: capture -> queue -> broad ROI -> subtitle band -> detector.

Pipeline per frame (one PNG decode, one broad crop, one band crop):

    decoded RGBA -> broad ROI crop -> subtitle band crop -> ROIChangeDetector

Modes:
    --mock-static    identical frames -> band mostly unchanged
    --mock-subtitle  text-like block toggles inside the band
    --mock-moving    block moves inside the broad ROI but OUTSIDE the band
    --live           real gamescope base_plane_only frames

Bounded; exits automatically; optional debug PNGs are produced from in-memory
band/broad RGBA (never from the transient Gamescope screenshot path).

Usage:
    python3 scripts/roi_test.py --mock-moving --duration-sec 4 --fps 2 --debug
    python3 scripts/roi_test.py --live --duration-sec 20 --fps 1 \
        --band 0.05,0.35,0.90,0.45 --debug-copy ~/clarifydeck-debug/band.png --debug
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

from capture import gamescope_capture, recognition_roi, roi as roi_mod  # noqa: E402
from capture.change_detector import FrameChangeDetector  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402
from capture.producer import CaptureProducer  # noqa: E402

MOCK_W, MOCK_H = 320, 200


def _png_rgb(rgb: bytearray, width: int, height: int) -> bytes:
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


def _solid(width: int, height: int, value: int) -> bytearray:
    return bytearray([value, value, value] * (width * height))


def _with_block(base: bytearray, width: int, height: int, x0: int, y0: int, bw: int, bh: int, value: int) -> bytearray:
    out = bytearray(base)
    for y in range(y0, min(height, y0 + bh)):
        for x in range(x0, min(width, x0 + bw)):
            o = (y * width + x) * 3
            out[o] = out[o + 1] = out[o + 2] = value
    return out


class MockCapture:
    def __init__(self, mode: str, broad: roi_mod.NormalizedROI, band: roi_mod.SubtitleBandROI) -> None:
        self._seq = 0
        self._mode = mode
        self._broad = roi_mod.resolve_roi(broad, MOCK_W, MOCK_H)
        self._band = roi_mod.resolve_subtitle_band(band, self._broad.width, self._broad.height)

    def _variant(self, seq: int) -> bytearray:
        base = _solid(MOCK_W, MOCK_H, 100)
        b = self._broad
        band = self._band
        if self._mode == "static":
            return base
        if self._mode == "subtitle":
            if (seq // 2) % 2 == 0:
                return base
            # small text-like block fully inside the band
            x = b.x + band.x + 80
            y = b.y + band.y + max(0, band.height // 2 - 5)
            return _with_block(base, MOCK_W, MOCK_H, x, y, 25, 10, 255)
        # moving: animate a block inside the broad ROI but above the band
        offset = (seq * 11) % max(1, b.width - 40)
        y = b.y + max(0, min(2, band.y - 12))
        return _with_block(base, MOCK_W, MOCK_H, b.x + offset, y, 40, 12, 255)

    def capture_frame(self, mode: str = "base_plane_only", timeout: float = 5.0, debug_copy=None) -> CaptureFrame:
        self._seq += 1
        return CaptureFrame.from_png(
            _png_rgb(self._variant(self._seq), MOCK_W, MOCK_H),
            sequence=self._seq,
            source_backend="mock",
            source_mode=mode,
        )


def process_frame(
    frame: CaptureFrame,
    *,
    broad_detector: roi_mod.ROIChangeDetector,
    band_detector: roi_mod.ROIChangeDetector,
    broad: roi_mod.NormalizedROI,
    band: roi_mod.SubtitleBandROI,
    full_detector: FrameChangeDetector | None = None,
):
    """Decode once, crop broad once, crop band once, classify both detectors."""
    decoded = roi_mod.decode_png_ex(frame.encoded_bytes)
    broad_roi = roi_mod.resolve_roi(broad, decoded.width, decoded.height)
    broad_w, broad_h, broad_rgba = roi_mod.crop_rgba(decoded.rgba, decoded.width, decoded.height, broad_roi)
    band_roi = roi_mod.resolve_subtitle_band(band, broad_w, broad_h)

    broad_decision = broad_detector.classify_rgba(
        sequence=frame.sequence,
        captured_monotonic=frame.captured_monotonic,
        width=broad_w,
        height=broad_h,
        rgba=broad_rgba,
        pixel_roi=roi_mod.PixelROI(0, 0, broad_w, broad_h),
        report_roi=broad_roi,
        decoder_backend=decoded.decoder_backend,
        decoder_fallback_reason=decoded.decoder_fallback_reason,
    )
    band_frame = roi_mod.make_subtitle_band_frame(
        sequence=frame.sequence,
        captured_monotonic=frame.captured_monotonic,
        broad_rgba=broad_rgba,
        broad_width=broad_w,
        broad_height=broad_h,
        broad_roi=broad_roi,
        band_roi=band_roi,
        decoder_backend=decoded.decoder_backend,
        decoder_fallback_reason=decoded.decoder_fallback_reason,
    )
    band_decision = band_detector.classify_subtitle_band(band_frame)
    full_score = full_detector.classify(frame).score if full_detector is not None else None
    return broad_decision, band_decision, band_frame, broad_rgba, broad_roi, band_roi, full_score


async def run(args) -> int:
    queue = LatestFrameQueue()
    if args.live:
        capture = gamescope_capture.GamescopeCapture(logger=lambda m: print(m, file=sys.stderr))
    elif args.mock_moving:
        capture = MockCapture("moving", args.roi, args.band)
    elif args.mock_subtitle:
        capture = MockCapture("subtitle", args.roi, args.band)
    else:
        capture = MockCapture("static", args.roi, args.band)

    producer = CaptureProducer(capture, queue, target_fps=args.fps)
    broad_detector = roi_mod.ROIChangeDetector(roi=args.roi, scale=args.effective_scale)
    band_detector = roi_mod.ROIChangeDetector(roi=args.roi, scale=args.effective_scale)
    full_detector = FrameChangeDetector() if args.full else None

    stop = asyncio.Event()
    broad_decisions = []
    band_decisions = []
    full_scores = []
    latest = {"band": None, "broad": None}

    proc_start = time.process_time()
    wall_start = time.monotonic()
    loop = {"iterations": 0, "frames_received": 0, "empty_polls": 0, "wait_wakeups": 0}
    timing = {"pipeline_wall": 0.0, "wait_wall": 0.0}

    async def consumer() -> None:
        while True:
            if stop.is_set() and queue.pending == 0:
                return
            loop["iterations"] += 1
            w0 = time.monotonic()
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                loop["empty_polls"] += 1
                timing["wait_wall"] += time.monotonic() - w0
                continue
            timing["wait_wall"] += time.monotonic() - w0
            loop["wait_wakeups"] += 1
            loop["frames_received"] += 1
            d0 = time.monotonic()
            broad_decision, band_decision, band_frame, broad_rgba, broad_roi, band_roi, full_score = process_frame(
                frame,
                broad_detector=broad_detector,
                band_detector=band_detector,
                broad=args.roi,
                band=args.band,
                full_detector=full_detector,
            )
            timing["pipeline_wall"] += time.monotonic() - d0
            broad_decisions.append(broad_decision)
            band_decisions.append(band_decision)
            if full_score is not None:
                full_scores.append(full_score)
            latest["band"] = (band_frame.width, band_frame.height, band_frame.rgba)
            latest["broad"] = (broad_roi.width, broad_roi.height, broad_rgba)
            if args.debug:
                print(
                    f"[broad] seq={broad_decision.sequence} score={broad_decision.score:.5f} "
                    f"changed={broad_decision.changed} reason={broad_decision.reason}"
                )
                print(
                    f"[band]  seq={band_decision.sequence} score={band_decision.score:.5f} "
                    f"changed={band_decision.changed} reason={band_decision.reason}"
                )

    consumer_task = asyncio.create_task(consumer())
    await producer.start()
    await asyncio.sleep(args.duration_sec)
    await producer.stop()
    stop.set()
    await consumer_task

    wall_total = time.monotonic() - wall_start
    proc_cpu = time.process_time() - proc_start
    broad_stats = broad_detector.stats()
    band_stats = band_detector.stats()
    queue_stats = queue.stats()
    queue.clear()
    broad_detector.reset()
    band_detector.reset()
    if full_detector is not None:
        full_detector.reset()

    print(
        f"[broad] x={broad_stats.roi_x} y={broad_stats.roi_y} w={broad_stats.roi_width} "
        f"h={broad_stats.roi_height} bytes={broad_stats.roi_bytes} threshold={broad_stats.threshold}"
    )
    print(
        f"[band] x={band_stats.roi_x} y={band_stats.roi_y} w={band_stats.roi_width} "
        f"h={band_stats.roi_height} bytes={band_stats.roi_bytes} threshold={band_stats.threshold} "
        f"grid={band_stats.grid}"
    )
    print(
        f"[broad] seen={broad_stats.frames_seen} changed={broad_stats.changed} "
        f"unchanged={broad_stats.unchanged} stale={broad_stats.stale_rejected} "
        f"max_score={max((d.score for d in broad_decisions), default=0.0):.5f}"
    )
    print(
        f"[band] seen={band_stats.frames_seen} changed={band_stats.changed} "
        f"unchanged={band_stats.unchanged} stale={band_stats.stale_rejected} "
        f"max_score={max((d.score for d in band_decisions), default=0.0):.5f} "
        f"decode_errors={band_stats.decode_errors} crop_errors={band_stats.crop_errors}"
    )
    print(
        f"[band] crop_ms={band_stats.crop_ms} preprocess_ms={band_stats.preprocess_ms} "
        f"signature_ms={band_stats.roi_signature_ms} compare_ms={band_stats.roi_compare_ms} "
        f"total_ms={band_stats.total_ms} decoder_backend={band_stats.decoder_backend}"
    )
    if full_scores:
        print(f"[full] max_score={max(full_scores):.5f} last_score={full_scores[-1]:.5f} samples={len(full_scores)}")
    print(
        f"[cpu] wall_total_sec={wall_total:.2f} process_cpu_total_sec={proc_cpu:.2f} "
        f"avg_process_cpu_pct={(proc_cpu / wall_total * 100.0) if wall_total else 0.0:.1f}"
    )
    print(
        f"[cpu] pipeline_wall_ms={timing['pipeline_wall']*1000:.1f} wait_wall_ms={timing['wait_wall']*1000:.1f}"
    )
    print(
        f"[loop] iterations={loop['iterations']} frames_received={loop['frames_received']} "
        f"empty_polls={loop['empty_polls']} wait_wakeups={loop['wait_wakeups']}"
    )
    print(f"[band] recent={[(d.sequence, d.changed, round(d.score, 5), d.reason) for d in band_decisions[-12:]]}")
    print(f"[roi] queue={queue_stats.__dict__}")

    if args.debug_copy and latest["band"] is not None:
        target = Path(args.debug_copy).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        w, h, rgba = latest["band"]
        target.write_bytes(roi_mod.encode_rgba_png(rgba, w, h))
        print(f"[debug] wrote band {target} {w}x{h}")
    if args.debug_copy_broad and latest["broad"] is not None:
        target = Path(args.debug_copy_broad).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        w, h, rgba = latest["broad"]
        target.write_bytes(roi_mod.encode_rgba_png(rgba, w, h))
        print(f"[debug] wrote broad {target} {w}x{h}")

    ok = (
        queue_stats.max_pending <= 1
        and band_stats.decode_errors == 0
        and band_stats.crop_errors == 0
        and band_stats.frames_seen >= 1
    )
    if args.mock_static:
        ok = ok and band_stats.unchanged >= 1
    if args.mock_subtitle:
        ok = ok and band_stats.changed >= 2
    if args.mock_moving:
        ok = ok and broad_stats.changed >= 2 and band_stats.unchanged >= 1
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def _parse_roi(text: str) -> roi_mod.NormalizedROI:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--roi must be x,y,w,h")
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--roi values must be numbers") from exc
    try:
        return roi_mod.NormalizedROI(x, y, w, h)
    except CaptureError as exc:
        raise argparse.ArgumentTypeError(f"invalid --roi: {exc}") from exc


def _parse_band(text: str) -> roi_mod.SubtitleBandROI:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--band must be x,y,w,h")
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--band values must be numbers") from exc
    try:
        return roi_mod.SubtitleBandROI(x, y, w, h)
    except CaptureError as exc:
        raise argparse.ArgumentTypeError(f"invalid --band: {exc}") from exc


def _apply_active_roi(args) -> int:
    """Set/reset a persisted recognition ROI. Config-only: no capture/renderer."""
    store = recognition_roi.get_store()
    app_id = args.app_id
    if args.reset_active_roi:
        try:
            store.reset(app_id)
        except OSError as exc:
            print(f"[active-roi-reset] ok=false app_id={app_id} error=config_write_failed detail={exc}")
            return 1
        print(f"[active-roi-reset] ok=true app_id={app_id}")
        active = recognition_roi.get_resolver().resolve(app_id)
        roi = active.roi
        print(
            f"[active-roi] source={active.source} "
            f"normalized=({roi.x},{roi.y},{roi.width},{roi.height})"
        )
        return 0

    try:
        validated = recognition_roi.validate_user_roi(args.set_active_roi)
    except CaptureError as exc:
        print(f"[active-roi-set] ok=false app_id={app_id} error={exc.code} detail={exc}")
        return 1
    try:
        store.set(app_id, validated)
    except CaptureError as exc:
        print(f"[active-roi-set] ok=false app_id={app_id} error={exc.code} detail={exc}")
        return 1
    except OSError as exc:
        print(f"[active-roi-set] ok=false app_id={app_id} error=config_write_failed detail={exc}")
        return 1
    print(f"[active-roi-set] ok=true app_id={app_id}")
    print(f"[active-roi-set] normalized=({validated.x},{validated.y},{validated.width},{validated.height})")
    return 0


def _print_active_roi(args) -> int:
    resolver = recognition_roi.get_resolver()
    active = resolver.resolve(args.app_id)
    pixel = roi_mod.resolve_roi(active.roi, args.frame_width, args.frame_height)
    rgba = bytes(args.frame_width * args.frame_height * 4)
    started = time.perf_counter()
    width, height, crop = roi_mod.crop_rgba(rgba, args.frame_width, args.frame_height, pixel)
    crop_ms = (time.perf_counter() - started) * 1000.0
    roi = active.roi
    print(f"[active-roi] source={active.source} app_id={args.app_id}")
    print(
        f"[active-roi] normalized=({roi.x},{roi.y},{roi.width},{roi.height}) "
        f"pixel=({pixel.x},{pixel.y},{pixel.width},{pixel.height}) "
        f"frame={args.frame_width}x{args.frame_height} bytes={len(crop)} crop_ms={crop_ms:.3f}"
    )
    print(f"[active-roi] config={recognition_roi.get_store().path} error={recognition_roi.get_store().last_error}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck ROI / subtitle-band diagnostic")
    parser.add_argument("--duration-sec", type=float, default=5.0)
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--roi", type=_parse_roi, default=roi_mod.DEFAULT_ROI)
    parser.add_argument("--band", type=_parse_band, default=roi_mod.DEFAULT_SUBTITLE_BAND)
    parser.add_argument("--scale", type=int, default=1, choices=roi_mod.ALLOWED_SCALES)
    parser.add_argument("--debug-copy", default=None, help="write the band ROI PNG")
    parser.add_argument("--debug-copy-broad", default=None, help="write the broad ROI PNG")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--mock-static", action="store_true")
    parser.add_argument("--mock-subtitle", action="store_true")
    parser.add_argument("--mock-moving", action="store_true")
    parser.add_argument("--full", action="store_true", help="also run the full-frame detector (extra decode)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--allow-long", action="store_true")
    parser.add_argument("--app-id", default=None, help="Steam app id for per-game ROI resolution")
    parser.add_argument("--roi-config", default=None, help="path to recognition_roi.json")
    parser.add_argument("--active-roi", action="store_true", help="print the resolved recognition ROI and exit")
    parser.add_argument("--use-active-roi", action="store_true", help="crop the persisted recognition ROI")
    parser.add_argument("--set-active-roi", type=_parse_roi, default=None, help="persist a recognition ROI x,y,w,h")
    parser.add_argument("--reset-active-roi", action="store_true", help="remove the user override (global or --app-id)")
    parser.add_argument("--frame-width", type=int, default=1280)
    parser.add_argument("--frame-height", type=int, default=800)
    args = parser.parse_args(argv)
    if args.roi_config:
        recognition_roi.configure(Path(args.roi_config))
    if args.set_active_roi is not None or args.reset_active_roi:
        return _apply_active_roi(args)
    if args.active_roi:
        return _print_active_roi(args)
    if args.duration_sec <= 0:
        print("--duration-sec must be > 0")
        return 2
    if args.duration_sec > 60 and not args.allow_long:
        print("--duration-sec clamped to 60 (use --allow-long to override)")
        args.duration_sec = 60.0
    if not (args.live or args.mock_static or args.mock_subtitle or args.mock_moving):
        args.mock_static = True
    args.effective_scale = args.scale
    if args.scale != 1:
        print(f"[roi] scale={args.scale} deferred (no lightweight native scaler); using scale=1")
        args.effective_scale = 1
    if args.use_active_roi:
        active = recognition_roi.get_resolver().resolve(args.app_id)
        args.roi = roi_mod.NormalizedROI(0.0, 0.0, 1.0, 1.0)
        args.band = roi_mod.SubtitleBandROI(
            x=active.roi.x, y=active.roi.y, width=active.roi.width, height=active.roi.height
        )
        print(
            f"[active-roi] source={active.source} "
            f"normalized=({active.roi.x},{active.roi.y},{active.roi.width},{active.roi.height})"
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
