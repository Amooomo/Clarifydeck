#!/usr/bin/env python3
"""Phase 2F OCR diagnostic (text only; never wired into plugin boot).

Modes:
    --probe          runtime probe + single engine init (Gate 0/1)
    --image PATH     OCR one PNG file (Gate 2)
    --live           capture -> active user ROI -> OCR at low cadence (Gate 3+)

Pipeline (live):
    CaptureFrame -> libpng decode once -> ActiveROIResolver -> crop_rgba -> OCR

Change detection is intentionally NOT required: OCR runs on every consumed frame.

Run:
    python3 scripts/ocr_test.py --probe --model-dir models/ppocrv6
    python3 scripts/ocr_test.py --image shot.png --model-dir models/ppocrv6 --debug
    python3 scripts/ocr_test.py --live --duration-sec 15 --fps 1 \
        --roi-config "$CFG" --model-dir models/ppocrv6 --debug
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import gamescope_capture, recognition_roi, roi as ocr_roi_mod  # noqa: E402
from capture.change_detector import decode_png_ex  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402
from capture.producer import CaptureProducer  # noqa: E402
from ocr import (  # noqa: E402
    BundleError,
    OCRConfig,
    OCRError,
    OCRRuntime,
    activate_plugin_ocr_runtime,
    predict_det_geometry,
    probe_report_lines,
    probe_runtime,
)


class OCRState(str, Enum):
    STOPPED = "STOPPED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


def process_frame(frame: CaptureFrame, *, runtime: OCRRuntime, app_id=None, resolver=None):
    """Decode once, resolve+crop the active ROI once, then OCR the ROI bytes."""
    roi_frame, result, _timings = process_frame_timed(
        frame, runtime=runtime, app_id=app_id, resolver=resolver
    )
    return roi_frame, result


def process_frame_timed(frame: CaptureFrame, *, runtime: OCRRuntime, app_id=None, resolver=None):
    """Same as process_frame but returns per-stage monotonic timings."""
    timings: dict = {}
    decode_started = time.perf_counter()
    decoded = decode_png_ex(frame.encoded_bytes)
    decode_done = time.perf_counter()
    roi_frame = recognition_roi.extract_recognition_roi(
        decoded.rgba, decoded.width, decoded.height, app_id, resolver=resolver
    )
    crop_done = time.perf_counter()
    result = runtime.recognize_rgba(roi_frame.rgba, roi_frame.width, roi_frame.height, sequence=frame.sequence)
    ocr_done = time.perf_counter()
    timings["decode_ms"] = round((decode_done - decode_started) * 1000.0, 3)
    timings["roi_crop_ms"] = round((crop_done - decode_done) * 1000.0, 3)
    timings["ocr_wall_ms"] = round((ocr_done - crop_done) * 1000.0, 3)
    timings.update(runtime.last_timings)
    timings["total_frame_pipeline_ms"] = round(
        timings["decode_ms"] + timings["roi_crop_ms"] + timings["ocr_wall_ms"], 3
    )
    return roi_frame, result, timings


def _rss_kb() -> Optional[int]:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def _thread_count() -> Optional[int]:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def _oversubscription_lines(args, runtime) -> list:
    lines = [f"[ocr-threads] cpu_count={os.cpu_count()}"]
    lines.append(f"[ocr-threads] ort_intra={getattr(runtime, 'ort_intra_threads', None)}")
    lines.append(f"[ocr-threads] ort_inter={getattr(runtime, 'ort_inter_threads', None)}")
    lines.append(f"[ocr-threads] opencv={getattr(runtime, 'opencv_threads', None)}")
    lines.append(f"[ocr-threads] process_threads={_thread_count()}")
    return lines


class TimingStats:
    """Bounded per-stage timing aggregation (no unbounded history)."""

    def __init__(self, limit: int = 200) -> None:
        self.limit = limit
        self.samples: list = []
        self.first_ms: Optional[float] = None
        self.total_wall_ms = 0.0
        self.total_cpu_ms = 0.0
        self.stages: dict = {}

    def add(self, wall_ms: Optional[float], cpu_ms: Optional[float] = None, stages: Optional[dict] = None) -> None:
        if wall_ms is None:
            return
        if self.first_ms is None:
            self.first_ms = wall_ms
        if len(self.samples) < self.limit:
            self.samples.append(wall_ms)
        self.total_wall_ms += wall_ms
        if cpu_ms:
            self.total_cpu_ms += cpu_ms
        for key, value in (stages or {}).items():
            if isinstance(value, (int, float)):
                self.stages.setdefault(key, []).append(float(value))

    def _percentile(self, values: list, pct: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
        return round(ordered[index], 3)

    def summary(self) -> dict:
        if not self.samples:
            return {
                "count": 0,
                "first_ms": None,
                "avg_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "max_ms": None,
                "avg_cpu_pct": None,
                "stages_avg_ms": {},
            }
        steady = self.samples[1:] if len(self.samples) > 1 else self.samples
        avg = sum(steady) / len(steady)
        return {
            "count": len(self.samples),
            "first_ms": self.first_ms,
            "avg_ms": round(avg, 3),
            "p50_ms": self._percentile(steady, 50),
            "p95_ms": self._percentile(steady, 95),
            "max_ms": round(max(self.samples), 3),
            "avg_cpu_pct": round(self.total_cpu_ms / self.total_wall_ms * 100.0, 1) if self.total_wall_ms else None,
            "stages_avg_ms": {
                key: round(sum(values) / len(values), 3) for key, values in self.stages.items() if values
            },
        }


class OCRDiagnostic:
    MAX_CONSECUTIVE_ERRORS = 3
    RECENT_LIMIT = 12

    def __init__(self, args, runtime: OCRRuntime, resolver=None) -> None:
        self.args = args
        self.runtime = runtime
        self.resolver = resolver
        self.state = OCRState.STOPPED
        self.frames_received = 0
        self.frames_ocr = 0
        self.ocr_errors = 0
        self.consecutive_errors = 0
        self.roi_width = None
        self.roi_height = None
        self.last_ocr_ms = None
        self.ocr_total_ms = 0.0
        self.max_ocr_ms = 0.0
        self.last_line_count = 0
        self.last_text_chars = 0
        self.last_timings: dict = {}
        self.recent: list = []
        self.correlations: list = []
        self.saved_roi = False
        self.det_input_logged = False

    def _correlate(self, result, timings: dict) -> None:
        """Bounded recent table: sequence / ocr_wall_ms / det_ms / capture timing."""
        self.correlations.append(
            {
                "sequence": result.sequence,
                "ocr_wall_ms": timings.get("ocr_wall_ms"),
                "det_ms": timings.get("det_ms"),
                "rec_ms": timings.get("rec_ms"),
                "capture_wait_ms": timings.get("capture_wait_ms"),
                "capture_age_ms": timings.get("capture_age_ms"),
            }
        )
        if len(self.correlations) > self.RECENT_LIMIT:
            self.correlations.pop(0)

    def _save_debug_roi(self, sequence, roi_frame) -> None:
        """Write exactly one live ROI PNG from the same bytes passed to OCR."""
        target = Path(self.args.debug_roi_copy).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(ocr_roi_mod.encode_rgba_png(roi_frame.rgba, roi_frame.width, roi_frame.height))
        except (OSError, OCRError) as exc:
            print(f"[ocr-debug] roi_copy_failed path={target} detail={exc}", flush=True)
            self.saved_roi = True
            return
        self.saved_roi = True
        print(
            f"[ocr-debug] saved_roi seq={sequence} path={target} size={roi_frame.width}x{roi_frame.height}",
            flush=True,
        )

    def _record(self, result, timings: Optional[dict] = None) -> None:
        self.frames_ocr += 1
        self.last_ocr_ms = result.elapsed_ms
        self.ocr_total_ms += result.elapsed_ms
        self.max_ocr_ms = max(self.max_ocr_ms, result.elapsed_ms)
        self.last_line_count = result.line_count
        self.last_text_chars = len(result.text)
        self.last_timings = timings or {}
        self.recent.append(result)
        if len(self.recent) > self.RECENT_LIMIT:
            self.recent.pop(0)

    def _emit(self, result) -> None:
        if self.args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
            return
        print(
            f"[ocr] seq={result.sequence} roi={result.roi_width}x{result.roi_height} "
            f"lines={result.line_count} elapsed_ms={result.elapsed_ms}",
            flush=True,
        )
        if self.args.debug:
            for index, line in enumerate(result.lines, start=1):
                conf = "None" if line.confidence is None else f"{line.confidence:.3f}"
                print(f'[ocr] #{index} conf={conf} text="{line.text}"', flush=True)

    async def run_live(self) -> int:
        self._queue = LatestFrameQueue()
        if self.args.live:
            capture = gamescope_capture.GamescopeCapture(logger=lambda m: print(m, file=sys.stderr))
        else:
            capture = _MockCapture(self.args)
        self._producer = CaptureProducer(capture, self._queue, target_fps=self.args.fps)
        self._stop_event = asyncio.Event()
        self._proc_start = time.process_time()
        self._wall_start = time.monotonic()
        self._no_ocr = getattr(self.args, "no_ocr", False)
        self._stats = TimingStats()
        self._capture_stats = TimingStats()
        self._rss_start = _rss_kb()
        self._rss_peak = self._rss_start
        self._shutdown_done = False
        self._interrupted = False
        self._consumer_task = None

        for line in _oversubscription_lines(self.args, self.runtime):
            print(line, flush=True)
        self.state = OCRState.RUNNING
        self._consumer_task = asyncio.create_task(self._consume())
        try:
            await self._producer.start()
            await asyncio.sleep(self.args.duration_sec)
            return self._finalize()
        except asyncio.CancelledError:
            self._interrupted = True
            raise
        except Exception as exc:
            print(f"[ocr] unexpected_error error={type(exc).__name__} detail={exc}", flush=True)
            self.state = OCRState.FAILED
            return 1
        finally:
            await self._shutdown_live("interrupt" if self._interrupted else "complete")

    async def _consume(self) -> None:
        queue = self._queue
        stop = self._stop_event
        while True:
            if stop.is_set() and queue.pending == 0:
                return
            wait_started = time.monotonic()
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            wait_ms = round((time.monotonic() - wait_started) * 1000.0, 3)
            self.frames_received += 1
            if self._no_ocr:
                # capture + decode + crop only; the OCR engine is never touched
                try:
                    decoded = decode_png_ex(frame.encoded_bytes)
                    roi = recognition_roi.extract_recognition_roi(
                        decoded.rgba, decoded.width, decoded.height, self.args.app_id, resolver=self.resolver
                    )
                except (OCRError, CaptureError):
                    self.ocr_errors += 1
                    continue
                self.roi_width = roi.width
                self.roi_height = roi.height
                self._capture_stats.add(
                    wait_ms + decoded.decode_total_ms, stages={"decode_ms": decoded.decode_total_ms}
                )
                current = _rss_kb()
                if current:
                    self._rss_peak = max(self._rss_peak or 0, current)
                continue
            try:
                roi_frame, result, timings = await asyncio.to_thread(
                    process_frame_timed,
                    frame,
                    runtime=self.runtime,
                    app_id=self.args.app_id,
                    resolver=self.resolver,
                )
            except (OCRError, CaptureError) as exc:
                code = getattr(exc, "code", "ocr_error")
                self.ocr_errors += 1
                self.consecutive_errors += 1
                print(f"[ocr] seq={frame.sequence} error={code} detail={exc}", flush=True)
                if self.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self.state = OCRState.FAILED
                    return
                continue
            self.consecutive_errors = 0
            self.roi_width = roi_frame.width
            self.roi_height = roi_frame.height
            timings["capture_wait_ms"] = wait_ms
            timings["capture_age_ms"] = round(
                max(0.0, (time.monotonic() - frame.captured_monotonic) * 1000.0), 3
            )
            if not self.det_input_logged:
                print(_det_input_line(roi_frame.width, roi_frame.height, self.args), flush=True)
                self.det_input_logged = True
            self._record(result, timings)
            self._emit(result)
            self._correlate(result, timings)
            self._stats.add(
                result.elapsed_ms,
                cpu_ms=timings.get("ocr_process_cpu_ms"),
                stages={
                    "decode_ms": timings.get("decode_ms"),
                    "roi_crop_ms": timings.get("roi_crop_ms"),
                    "rgba_to_array_ms": timings.get("rgba_to_array_ms"),
                    "ocr_call_ms": timings.get("ocr_call_ms"),
                    "result_normalize_ms": timings.get("result_normalize_ms"),
                    "det_ms": timings.get("det_ms"),
                    "rec_ms": timings.get("rec_ms"),
                },
            )
            if getattr(self.args, "debug_roi_copy", None) and not self.saved_roi:
                self._save_debug_roi(frame.sequence, roi_frame)
            current = _rss_kb()
            if current:
                self._rss_peak = max(self._rss_peak or 0, current)

    async def _shutdown_live(self, reason: str) -> None:
        """Single idempotent shutdown path (normal completion, Ctrl-C, failure)."""
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        print(f"[ocr] shutdown reason={reason}", flush=True)
        self.state = OCRState.STOPPING
        print(f"[ocr] state={self.state.value}", flush=True)

        producer = getattr(self, "_producer", None)
        if producer is not None:
            try:
                await producer.stop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[ocr] producer_stop_failed detail={exc}", flush=True)

        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()

        consumer = getattr(self, "_consumer_task", None)
        if consumer is not None and not consumer.done():
            consumer.cancel()
        if consumer is not None:
            try:
                await consumer
            except asyncio.CancelledError:
                if not consumer.cancelled():
                    raise
            except Exception as exc:
                print(f"[ocr] consumer_failed detail={exc}", flush=True)

        queue = getattr(self, "_queue", None)
        if queue is not None:
            queue.clear()
        if not getattr(self, "_no_ocr", False):
            try:
                self.runtime.close()
            except Exception:
                pass

        self.state = OCRState.STOPPED
        print(f"[ocr] state={self.state.value}", flush=True)
        self._print_summary()

    def _print_summary(self) -> None:
        try:
            queue_stats = self._queue.stats()
            summary = self._stats.summary()
            wall_total = time.monotonic() - self._wall_start
            proc_cpu = time.process_time() - self._proc_start
            print(
                f"[ocr] state={self.state.value} frames_received={self.frames_received} "
                f"frames_ocr={self.frames_ocr} ocr_errors={self.ocr_errors} "
                f"engine_init_count={self.runtime.engine_init_count} engine_init_ms={self.runtime.engine_init_ms}"
            )
            print(
                f"[ocr] roi={self.roi_width}x{self.roi_height} backend={self.runtime.backend} "
                f"no_ocr={self._no_ocr}"
            )
            print(
                f"[ocr] first_ocr_ms={summary['first_ms']} steady_avg_ocr_ms={summary['avg_ms']} "
                f"p50_ms={summary['p50_ms']} p95_ms={summary['p95_ms']} max_ocr_ms={summary['max_ms']}"
            )
            print(f"[ocr] ocr_effective_cpu_pct={summary['avg_cpu_pct']}")
            print(f"[ocr] stages_avg_ms={summary['stages_avg_ms']}")
            print(
                f"[cpu] wall_total_sec={wall_total:.2f} process_cpu_total_sec={proc_cpu:.2f} "
                f"avg_process_cpu_pct={(proc_cpu / wall_total * 100.0) if wall_total else 0.0:.1f}"
            )
            print(f"[rss] start_kb={self._rss_start} peak_kb={self._rss_peak} end_kb={_rss_kb()}")
            if self._no_ocr:
                capture_summary = self._capture_stats.summary()
                print(
                    f"[capture] frames={self.frames_received} "
                    f"avg_wait_plus_decode_ms={capture_summary['avg_ms']} "
                    f"stages_avg_ms={capture_summary['stages_avg_ms']}"
                )
            if self.args.debug and self.correlations:
                print(f"[ocr-correlation] {self.correlations}")
            print(f"[ocr] queue={queue_stats.__dict__}")
        except Exception as exc:
            print(f"[ocr] summary_failed detail={exc}", flush=True)

    def _finalize(self) -> int:
        if self.state == OCRState.FAILED:
            print("FAIL", flush=True)
            return 1
        queue_stats = self._queue.stats()
        if self._no_ocr:
            ok = queue_stats.max_pending <= 1 and self.ocr_errors == 0 and self.runtime.engine_init_count == 0
            print("PASS" if ok else "FAIL", flush=True)
            return 0 if ok else 1
        ok = (
            self.frames_ocr >= 1
            and queue_stats.max_pending <= 1
            and self.runtime.engine_init_count == 1
            and self.ocr_errors == 0
        )
        print("PASS" if ok else "FAIL", flush=True)
        return 0 if ok else 1


class _MockCapture:
    """Deterministic offline frames for pipeline tests (no Wayland)."""

    def __init__(self, args) -> None:
        self._args = args
        self._seq = 0

    def capture_frame(self, mode: str = "base_plane_only", timeout: float = 5.0, debug_copy=None) -> CaptureFrame:
        import struct
        import zlib

        self._seq += 1
        width, height = 64, 32
        raw = bytearray()
        for y in range(height):
            raw.append(0)
            raw += bytes([200, 200, 200]) * width

        def chunk(tag: bytes, payload: bytes) -> bytes:
            body = tag + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )
        return CaptureFrame.from_png(png, sequence=self._seq, source_backend="mock", source_mode=mode)


def _det_input_line(width: int, height: int, args) -> str:
    geometry = predict_det_geometry(
        width, height, getattr(args, "det_limit_side_len", 736), getattr(args, "det_limit_type", "min")
    )
    return (
        f"[ocr-det-input] source={geometry['source']} limit_type={geometry['limit_type']} "
        f"limit_side_len={geometry['limit_side_len']} ratio={geometry['ratio']} "
        f"predicted_resized={geometry['predicted_resized']}"
    )


def _build_runtime(args) -> OCRRuntime:
    config = OCRConfig(
        model_dir=Path(args.model_dir).expanduser(),
        color_order=args.color_order,
        min_confidence=args.min_confidence,
        engine_params=json.loads(args.engine_params) if args.engine_params else None,
        debug=args.debug,
        ort_intra_threads=args.ort_intra_threads,
        ort_inter_threads=args.ort_inter_threads,
        opencv_threads=args.opencv_threads,
        det_limit_side_len=args.det_limit_side_len,
        det_limit_type=args.det_limit_type,
    )
    return OCRRuntime(config)


def _run_repeat(args, runtime: OCRRuntime) -> int:
    """Static repeated OCR: decode/crop once, initialize once, OCR N times."""
    path = Path(args.image).expanduser()
    if not path.is_file():
        print(f"[ocr] image_missing path={path}")
        return 1
    try:
        decoded = decode_png_ex(path.read_bytes())
    except CaptureError as exc:
        print(f"[ocr] decode_failed error={exc.code} detail={exc}")
        return 1
    try:
        runtime.initialize()
    except OCRError as exc:
        print(f"[ocr] init_failed error={exc.code} detail={exc}")
        return 1
    for line in _oversubscription_lines(args, runtime):
        print(line)
    print(f"[ocr] input={path} size={decoded.width}x{decoded.height} repeat={args.repeat}")
    print(_det_input_line(decoded.width, decoded.height, args))
    print(
        f"[ocr] engine_init_count={runtime.engine_init_count} engine_init_ms={runtime.engine_init_ms} "
        f"backend={runtime.backend}"
    )
    stats = TimingStats()
    last_result = None
    for index in range(args.repeat):
        try:
            last_result = runtime.recognize_rgba(decoded.rgba, decoded.width, decoded.height, sequence=index)
        except OCRError as exc:
            print(f"[ocr] run_failed error={exc.code} detail={exc}")
            return 1
        timings = runtime.last_timings
        stats.add(
            last_result.elapsed_ms,
            cpu_ms=timings.get("ocr_process_cpu_ms"),
            stages={
                "rgba_to_array_ms": timings.get("rgba_to_array_ms"),
                "ocr_call_ms": timings.get("ocr_call_ms"),
                "result_normalize_ms": timings.get("result_normalize_ms"),
                "det_ms": timings.get("det_ms"),
                "rec_ms": timings.get("rec_ms"),
            },
        )
        if args.debug:
            print(
                f"[ocr] call={index} ocr_wall_ms={timings.get('ocr_wall_ms')} "
                f"ocr_call_ms={timings.get('ocr_call_ms')} det_ms={timings.get('det_ms')} "
                f"rec_ms={timings.get('rec_ms')} cpu_pct={timings.get('ocr_effective_cpu_pct')}"
            )
    summary = stats.summary()
    print(
        f"[ocr] first_ocr_ms={summary['first_ms']} steady_avg_ocr_ms={summary['avg_ms']} "
        f"p50_ms={summary['p50_ms']} p95_ms={summary['p95_ms']} max_ocr_ms={summary['max_ms']}"
    )
    print(f"[ocr] ocr_effective_cpu_pct={summary['avg_cpu_pct']}")
    print(f"[ocr] stages_avg_ms={summary['stages_avg_ms']}")
    if last_result is not None and args.debug:
        for index, line in enumerate(last_result.lines, start=1):
            conf = "None" if line.confidence is None else f"{line.confidence:.3f}"
            print(f'[ocr] #{index} conf={conf} text="{line.text}"')
    runtime.close()
    return 0


def _run_probe(args, runtime: OCRRuntime, activated=None) -> int:
    info = probe_runtime(Path(args.model_dir).expanduser())
    info["runtime_site_packages"] = str(activated) if activated else None
    info["runtime_activated"] = activated is not None
    for line in probe_report_lines(info):
        print(line)
    if not args.no_init:
        try:
            runtime.initialize()
        except OCRError as exc:
            print(f"[ocr] init_failed error={exc.code} detail={exc}")
            return 1
        print(
            f"[ocr] engine_init_count={runtime.engine_init_count} engine_init_ms={runtime.engine_init_ms} "
            f"backend={runtime.backend}"
        )
        paths = runtime.model_paths
        print(f"[ocr] det_model={paths.get('det')}")
        print(f"[ocr] rec_model={paths.get('rec')}")
        print(f"[ocr] classifier={paths.get('classifier')} model={paths.get('cls')}")
    return 0 if info.get("compatible") else 1


def _run_image(args, runtime: OCRRuntime) -> int:
    path = Path(args.image).expanduser()
    if not path.is_file():
        print(f"[ocr] image_missing path={path}")
        return 1
    try:
        decoded = decode_png_ex(path.read_bytes())
    except CaptureError as exc:
        print(f"[ocr] decode_failed error={exc.code} detail={exc}")
        return 1
    try:
        result = runtime.recognize_rgba(decoded.rgba, decoded.width, decoded.height, sequence=None)
    except OCRError as exc:
        print(f"[ocr] init_or_ocr_failed error={exc.code} detail={exc}")
        return 1
    print(f"[ocr] input={path} size={decoded.width}x{decoded.height} backend={result.backend}")
    print(f"[ocr] engine_init_count={runtime.engine_init_count} engine_init_ms={runtime.engine_init_ms}")
    print(f"[ocr] lines={result.line_count} elapsed_ms={result.elapsed_ms} det_ms={result.det_ms} rec_ms={result.rec_ms}")
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        for index, line in enumerate(result.lines, start=1):
            conf = "None" if line.confidence is None else f"{line.confidence:.3f}"
            print(f'[ocr] #{index} conf={conf} text="{line.text}"')
    return 0 if result.line_count >= 1 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck OCR diagnostic")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--no-init", action="store_true", help="probe only, skip engine init")
    parser.add_argument("--image", default=None)
    parser.add_argument("--repeat", type=int, default=None, help="static repeated OCR of --image")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--no-ocr", action="store_true", help="capture/decode/crop only; never init OCR")
    parser.add_argument("--replay-roi", default=None, help="alias for --image (static replay)")
    parser.add_argument("--debug-roi-copy", default=None, help="save one live active-ROI PNG")
    parser.add_argument("--mock", action="store_true", help="offline deterministic frames")
    parser.add_argument("--duration-sec", type=float, default=15.0)
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--app-id", default=None)
    parser.add_argument("--roi-config", default=None)
    parser.add_argument("--model-dir", default=str(ROOT / "models" / "ppocrv6"))
    parser.add_argument("--plugin-root", default=None, help="override plugin root for the runtime bundle")
    parser.add_argument("--require-bundle", action="store_true", help="fail if the plugin-local bundle is absent")
    parser.add_argument("--color-order", default="bgr", choices=("rgb", "bgr"))
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--engine-params", default=None, help="JSON override for RapidOCR params")
    parser.add_argument("--ort-intra-threads", type=int, default=None, help="ORT intra_op_num_threads (-1 auto)")
    parser.add_argument("--ort-inter-threads", type=int, default=None, help="ORT inter_op_num_threads (-1 auto)")
    parser.add_argument("--opencv-threads", type=int, default=None, help="cv2.setNumThreads value")
    parser.add_argument("--det-limit-side-len", type=int, default=736, help="Det.limit_side_len")
    parser.add_argument("--det-limit-type", default="min", choices=("min", "max"), help="Det.limit_type")
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-long", action="store_true")
    args = parser.parse_args(argv)

    if args.roi_config:
        recognition_roi.configure(Path(args.roi_config))
    if args.replay_roi and not args.image:
        args.image = args.replay_roi
    if args.duration_sec <= 0:
        print("--duration-sec must be > 0")
        return 2
    if args.duration_sec > 60 and not args.allow_long:
        print("--duration-sec clamped to 60 (use --allow-long to override)")
        args.duration_sec = 60.0
    if args.max_frames is not None:
        args.duration_sec = min(args.duration_sec, args.max_frames / max(args.fps, 0.2))

    plugin_root = Path(args.plugin_root).expanduser() if args.plugin_root else None
    try:
        activated = activate_plugin_ocr_runtime(plugin_root, require=args.require_bundle)
    except BundleError as exc:
        print(f"[ocr] bundle_error={exc.code} detail={exc}")
        return 1

    try:
        runtime = _build_runtime(args)
    except OCRError as exc:
        print(f"[ocr] config_error error={exc.code} detail={exc}")
        return 2
    if args.probe:
        return _run_probe(args, runtime, activated)
    if args.repeat is not None:
        if not args.image:
            print("--repeat requires --image")
            return 2
        if args.repeat < 1:
            print("--repeat must be >= 1")
            return 2
        return _run_repeat(args, runtime)
    if args.image:
        return _run_image(args, runtime)
    if args.live or args.mock:
        try:
            return asyncio.run(OCRDiagnostic(args, runtime).run_live())
        except KeyboardInterrupt:
            print("[ocr] interrupted", flush=True)
            return 130
        except asyncio.CancelledError:
            print("[ocr] interrupted", flush=True)
            return 130
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
