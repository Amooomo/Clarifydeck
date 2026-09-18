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
import contextlib
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
from capture.backend import resolve_capture_backend  # noqa: E402
from capture.change_detector import decode_frame_ex, decode_png_ex  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame, DecodedFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402
from capture.producer import CaptureProducer  # noqa: E402
from capture.scheduler import OCRChangeGate, validate_force_interval  # noqa: E402
from ocr import (  # noqa: E402
    BundleError,
    OCRConfig,
    OCRError,
    OCRRuntime,
    OCRStabilizer,
    StabilizerConfigError,
    activate_plugin_ocr_runtime,
    normalize_line,
    predict_det_geometry,
    probe_report_lines,
    probe_runtime,
)
from ocr.multi_region import MultiRegionOCRCoordinator  # noqa: E402
from ocr.transport import encode_envelope, encode_region_stable_text_event, envelope_from_event  # noqa: E402

# Bounded cap for opt-in OCR evidence diagnostics (never unbounded logs).
MAX_EVIDENCE_LINES = 20


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
    if isinstance(frame, DecodedFrame):
        decoded = decode_frame_ex(frame)
    else:
        decoded = decode_png_ex(frame.encoded_bytes)
    decode_done = time.perf_counter()
    roi_frame = recognition_roi.extract_recognition_roi(
        decoded.rgba, decoded.width, decoded.height, app_id, resolver=resolver
    )
    crop_done = time.perf_counter()
    result = runtime.recognize_rgba(roi_frame.rgba, roi_frame.width, roi_frame.height, sequence=frame.sequence)
    ocr_done = time.perf_counter()
    timings["decode_ms"] = round((decode_done - decode_started) * 1000.0, 3)
    conversion_ms = getattr(frame, "conversion_ms", None)
    if conversion_ms is not None:
        timings["frame_conversion_ms"] = round(float(conversion_ms), 3)
    timings["roi_crop_ms"] = round((crop_done - decode_done) * 1000.0, 3)
    timings["ocr_wall_ms"] = round((ocr_done - crop_done) * 1000.0, 3)
    timings.update(runtime.last_timings)
    timings["total_frame_pipeline_ms"] = round(
        timings["decode_ms"] + timings["roi_crop_ms"] + timings["ocr_wall_ms"], 3
    )
    return roi_frame, result, timings


class _StaticResolver:
    """Diagnostic-only resolver with a mutable ROI (for H6 ROI-switch testing)."""

    def __init__(self, roi) -> None:
        self.roi = roi

    def resolve(self, app_id=None):
        return recognition_roi.RecognitionROI(self.roi, "debug")


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

    def __init__(
        self,
        args,
        runtime: OCRRuntime,
        resolver=None,
        stabilizer=None,
        gate=None,
        machine_stream=None,
        multi_region_regions=None,
    ) -> None:
        self.args = args
        self.runtime = runtime
        self.resolver = resolver
        self.stabilizer = stabilizer
        self.gate = gate
        self.machine_stream = machine_stream
        self._multi_region_enabled = bool(getattr(args, "multi_region", False))
        self._multi_region_regions = tuple(multi_region_regions) if multi_region_regions is not None else None
        self._multi_region_primary_id: Optional[str] = None
        self._multi_region_coordinator: Optional[MultiRegionOCRCoordinator] = None
        self._stable_event_seq = 0
        self._last_ocr_trigger: Optional[str] = None
        self._debug_resolver = None
        self._debug_switch_roi = None
        self._debug_switch_after = None
        self._roi_switched = False
        self._debug_reset_after = None
        self._scheduler_reset = False
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

    def _stabilize(self, result) -> None:
        """Feed one OCR frame into the stabilizer and print stable diagnostics."""
        try:
            events = self.stabilizer.observe(result.lines, result.sequence, time.monotonic())
        except Exception as exc:
            print(f"[ocr-stable] error detail={exc}", flush=True)
            return
        candidate = self.stabilizer.last_candidate
        candidate_text = candidate.text if candidate is not None else ""
        print(f'[ocr-stable] candidate="{candidate_text}"', flush=True)
        print(
            f"[ocr-stable] consensus={self.stabilizer.last_consensus}/{self.stabilizer.consensus_required}",
            flush=True,
        )
        for event in events:
            if event.kind == "text":
                conf = "None" if event.confidence is None else f"{event.confidence:.3f}"
                print(
                    f'[ocr-stable] emit=text seq={event.source_seq} conf={conf} text="{event.text}"',
                    flush=True,
                )
            else:
                print("[ocr-stable] emit=clear", flush=True)
        self._emit_stable_events(events)
        self._emit_ocr_evidence(result)
        self._emit_fast_accept_single(self.stabilizer)

    def _emit_fast_accept_single(self, stabilizer) -> None:
        """Phase 2N.5A diagnostics for the single-region stabilizer path."""
        if stabilizer is None:
            return
        record = stabilizer.take_fast_accept_record()
        if record is not None:
            print(
                "[fast-accept] region={region} frame={frame} conf={conf} "
                "previous_stable={prev} threshold={threshold}".format(
                    region=record.get("region_id"),
                    frame=record.get("frame_seq"),
                    conf=record.get("confidence"),
                    prev=record.get("previous_stable"),
                    threshold=record.get("threshold"),
                ),
                flush=True,
            )
        confirm = stabilizer.take_fast_accept_confirm()
        if confirm is not None:
            print(
                "[fast-accept-confirm] region={region} match={match} "
                "next_conf={conf} elapsed_ms={elapsed}".format(
                    region=confirm.get("region_id"),
                    match=1 if confirm.get("match") else 0,
                    conf=confirm.get("next_confidence"),
                    elapsed=confirm.get("elapsed_ms"),
                ),
                flush=True,
            )

    def _diagnostic_enabled(self) -> bool:
        return bool(getattr(self.args, "diagnostic_ocr_evidence", False))

    def _emit_ocr_evidence(self, result) -> None:
        """Bounded opt-in evidence for one real OCR attempt (stderr only).

        Records exactly what OCR produced and what candidate reached the
        stabilizer, so true text vs. false positives can be compared on device.
        Never writes to stdout (machine JSONL) and never serializes images.
        """
        if not self._diagnostic_enabled():
            return
        stabilizer = self.stabilizer
        candidate = stabilizer.last_observed_candidate if stabilizer is not None else None
        min_conf = stabilizer.min_line_confidence if stabilizer is not None else 0.0
        lines_out = []
        usable = 0
        for line in result.lines:
            confidence = getattr(line, "confidence", None)
            box = getattr(line, "box", None)
            lines_out.append(
                {
                    "text": getattr(line, "text", ""),
                    "confidence": confidence,
                    "box": [list(point) for point in box] if box else None,
                }
            )
            if (
                confidence is not None
                and confidence >= min_conf
                and normalize_line(getattr(line, "text", ""))
            ):
                usable += 1
        truncated = len(lines_out) > MAX_EVIDENCE_LINES
        record = {
            "frame_seq": result.sequence,
            "timestamp_monotonic": time.monotonic(),
            "roi_pixel_size": [result.roi_width, result.roi_height],
            "change_gate_enabled": self.gate is not None,
            "ocr_trigger_reason": self._last_ocr_trigger,
            "real_ocr_attempt": True,
            "raw_line_count": len(lines_out),
            "usable_line_count": usable,
            "lines": lines_out[:MAX_EVIDENCE_LINES],
            "lines_truncated": truncated,
            "candidate_text": candidate.text if candidate is not None else None,
            "candidate_confidence": candidate.confidence if candidate is not None else None,
            "candidate_source_seq": candidate.source_seq if candidate is not None else None,
            "no_usable_text": candidate is None,
        }
        try:
            print(f"[ocr-evidence] {json.dumps(record, ensure_ascii=False)}", file=sys.stderr, flush=True)
        except Exception as exc:  # diagnostics must never break the OCR loop
            print(f"[ocr-evidence] emit_error detail={exc}", file=sys.stderr, flush=True)

    def _emit_ocr_schedule(self, reason: str) -> None:
        """Bounded opt-in skipped-frame record (distinct from OCR evidence)."""
        if not self._diagnostic_enabled():
            return
        record = {
            "frame_seq": None,
            "change_gate_enabled": True,
            "real_ocr_attempt": False,
            "reason": reason,
        }
        try:
            print(f"[ocr-schedule] {json.dumps(record, ensure_ascii=False)}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[ocr-schedule] emit_error detail={exc}", file=sys.stderr, flush=True)

    def _write_machine_line(self, line: str) -> None:
        if self.machine_stream is None:
            return
        try:
            self.machine_stream.write(line + "\n")
            self.machine_stream.flush()
        except Exception as exc:  # must never break the OCR loop
            print(f"[ocr-stable] emit_error detail={exc}", flush=True)

    def _emit_stable_events(self, events) -> None:
        """Write stabilizer events to the machine JSONL stream (transport).

        Shared by real OCR observations and skipped-frame ticks: a clear emitted
        by ``tick`` after a real no-text observation must reach transport too.
        """
        if self.machine_stream is None:
            return
        for event in events:
            self._stable_event_seq += 1
            self._write_machine_line(encode_envelope(envelope_from_event(self._stable_event_seq, event)))

    # -- Phase 2L.4 opt-in multi-region execution --------------------------

    def _resolve_effective_regions(self):
        """Authoritative 2L.1 region resolution (no second resolver here)."""
        from capture import recognition_regions, recognition_roi

        store = recognition_regions.RegionConfigStore(recognition_roi.get_store().path)
        resolver = recognition_regions.RegionResolver(store, legacy_resolver=recognition_roi.get_resolver())
        return resolver.resolve_effective_regions(getattr(self.args, "app_id", None)).regions

    def _setup_multi_region(self) -> bool:
        if self._multi_region_regions is None:
            self._multi_region_regions = tuple(self._resolve_effective_regions())
        primary = next((region for region in self._multi_region_regions if region.enabled), None)
        self._multi_region_primary_id = primary.region_id if primary is not None else None
        # When the change gate is requested, give every region its own gate (never
        # share the single-region gate instance across regions).
        gate_factory = None
        if self.gate is not None:
            force = validate_force_interval(getattr(self.args, "force_ocr_interval_sec", 3.0))
            gate_factory = lambda: OCRChangeGate(force_interval_sec=force)
        self._multi_region_coordinator = MultiRegionOCRCoordinator(self.runtime, gate_factory=gate_factory)
        return True

    def _process_multi_region(self, frame, wait_ms: float) -> None:
        """One decode -> many region crops -> v2 events (+ primary v1 projection)."""
        region_events = self._multi_region_coordinator.process_frame(frame, self._multi_region_regions)
        self.frames_ocr += 1
        for region_event in region_events:
            self._stable_event_seq += 1
            self._write_machine_line(
                encode_region_stable_text_event(self._stable_event_seq, region_event)
            )
            if region_event.region_id == self._multi_region_primary_id:
                self._stable_event_seq += 1
                self._write_machine_line(
                    encode_envelope(envelope_from_event(self._stable_event_seq, region_event.event))
                )
        # Phase 2N.3: one concise latency line per changed Stable Text (stderr only,
        # never the machine JSONL stream). No text content is logged.
        for sample in self._multi_region_coordinator.drain_latency():
            line = (
                "[latency] region={region} frame={frame} "
                "capture_age_at_ocr_start_ms={age} decode_ms={decode} roi_ms={roi} "
                "ocr_ms={ocr} stabilizer_accept_ms={stab} worker_total_ms={total}".format(
                    region=sample.get("region_id"),
                    frame=sample.get("frame_seq"),
                    age=sample.get("capture_age_at_ocr_start_ms"),
                    decode=sample.get("decode_ms"),
                    roi=sample.get("roi_ms"),
                    ocr=sample.get("ocr_ms"),
                    stab=sample.get("stabilizer_accept_ms"),
                    total=sample.get("worker_total_ms"),
                )
            )
            if sample.get("frame_conversion_ms") is not None:
                # PipeWire-only optional field; the legacy fields keep their meaning.
                line += " frame_conversion_ms={conv}".format(conv=sample.get("frame_conversion_ms"))
            print(line, flush=True)
        # Phase 2N.4: one concise first-candidate reliability line per accepted
        # transition (stderr only; no text content, digests/lengths only).
        for record in self._multi_region_coordinator.drain_audit():
            print(
                "[stabilizer-audit] region={region} frame={frame} "
                "first_conf={conf} candidate_count={count} first_matches_final={matches} "
                "distinct_candidates={distinct} first_to_accept_ms={first_to} saving_ms={saving}".format(
                    region=record.get("region_id"),
                    frame=record.get("frame_seq"),
                    conf=record.get("first_candidate_confidence"),
                    count=record.get("candidate_count_until_accept"),
                    matches=1 if record.get("first_matches_final") else 0,
                    distinct=record.get("intermediate_distinct_candidate_count"),
                    first_to=record.get("first_candidate_to_accept_ms"),
                    saving=record.get("theoretical_fast_accept_saving_ms"),
                ),
                flush=True,
            )

        # Phase 2N.5A: one concise fast-accept line per high-confidence replacement
        # and one shadow-confirmation line per resolved fast accept (stderr only;
        # no text content is logged).
        for record in self._multi_region_coordinator.drain_fast_accept():
            print(
                "[fast-accept] region={region} frame={frame} conf={conf} "
                "previous_stable={prev} threshold={threshold}".format(
                    region=record.get("region_id"),
                    frame=record.get("frame_seq"),
                    conf=record.get("confidence"),
                    prev=record.get("previous_stable"),
                    threshold=record.get("threshold"),
                ),
                flush=True,
            )
        for record in self._multi_region_coordinator.drain_fast_accept_confirm():
            print(
                "[fast-accept-confirm] region={region} match={match} "
                "next_conf={conf} elapsed_ms={elapsed}".format(
                    region=record.get("region_id"),
                    match=1 if record.get("match") else 0,
                    conf=record.get("next_confidence"),
                    elapsed=record.get("elapsed_ms"),
                ),
                flush=True,
            )

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
            capture = resolve_capture_backend(
                getattr(self.args, "capture_backend", None),
                logger=lambda m: print(m, file=sys.stderr),
            )
        else:
            capture = _MockCapture(self.args)
        self._capture_backend = capture
        self._producer = CaptureProducer(
            capture, self._queue, target_fps=self.args.fps, logger=lambda m: print(m, flush=True)
        )
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
        if self.stabilizer is not None:
            self.stabilizer.reset()
        if self.gate is not None:
            self.gate.reset()
        self._stable_event_seq = 0
        if self._multi_region_enabled:
            self._setup_multi_region()
        if getattr(self.args, "debug_switch_roi", None) is not None:
            base_roi = recognition_roi.get_resolver().resolve(self.args.app_id).roi
            self._debug_resolver = _StaticResolver(base_roi)
            self._debug_switch_roi = self.args.debug_switch_roi
            self._debug_switch_after = getattr(self.args, "debug_switch_roi_after_sec", None) or 5.0
            self._roi_switched = False
        self._debug_reset_after = getattr(self.args, "debug_reset_scheduler_after_sec", None)
        self._scheduler_reset = False

        for line in _oversubscription_lines(self.args, self.runtime):
            print(line, flush=True)
        self.state = OCRState.RUNNING
        self._consumer_task = asyncio.create_task(self._consume())
        try:
            if hasattr(capture, "start"):
                # PipeWire: connect the persistent stream before the producer pulls.
                await asyncio.to_thread(capture.start)
            await self._producer.start()
            await asyncio.sleep(self.args.duration_sec)
            return self._finalize()
        except asyncio.CancelledError:
            self._interrupted = True
            raise
        except CaptureError as exc:
            # Phase 2N.6.1: narrow startup/failure diagnostic (no transport change).
            print(
                f"[capture-backend] error backend={getattr(capture, 'name', 'unknown')} "
                f"code={exc.code} detail={exc}",
                flush=True,
            )
            self.state = OCRState.FAILED
            return 1
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
                    if isinstance(frame, DecodedFrame):
                        decoded = decode_frame_ex(frame)
                    else:
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
                await self._process_and_record(frame, wait_ms)
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

    def _effective_resolver(self):
        return self._debug_resolver if self._debug_resolver is not None else self.resolver

    async def _process_and_record(self, frame, wait_ms: float) -> None:
        if self._multi_region_enabled:
            # One decode -> many region crops; OCR runtime runs in a worker thread.
            await asyncio.to_thread(self._process_multi_region, frame, wait_ms)
            return
        if self.gate is not None and self._debug_reset_after is not None and not self._scheduler_reset:
            if time.monotonic() - self._wall_start >= self._debug_reset_after:
                before = self.gate.stats().ocr_trigger_first
                self.gate.reset()
                self._scheduler_reset = True
                print(
                    f"[ocr-scheduler] debug_scheduler_reset reset=1 "
                    f"ocr_trigger_first_before={before}",
                    flush=True,
                )
        if self._debug_resolver is not None and not self._roi_switched:
            if time.monotonic() - self._wall_start >= (self._debug_switch_after or 0.0):
                self._debug_resolver.roi = self._debug_switch_roi
                self._roi_switched = True
                print(
                    f"[ocr-scheduler] debug_roi_switch roi={self._debug_switch_roi.as_tuple()}",
                    flush=True,
                )
        if self.gate is not None:
            roi_frame, result, decision, timings = await asyncio.to_thread(self._gated_frame, frame)
            if getattr(self.args, "debug", False):
                if decision.roi_geometry_changed:
                    print(
                        f"[ocr-scheduler] roi_geometry_changed new={decision.roi_geometry} reset=1",
                        flush=True,
                    )
                suffix = (
                    f" remaining={decision.confirmation_remaining}"
                    if decision.reason == "confirmation"
                    else ""
                )
                print(
                    f"[ocr-scheduler] seq={frame.sequence} changed={decision.changed} "
                    f"action={decision.action} reason={decision.reason}{suffix}",
                    flush=True,
                )
            if decision.action == "skip":
                # Skipped OCR is NOT an empty OCR result; advance time only.
                # A clear can still legitimately fire here when an earlier real
                # no-text observation set the stale clock; forward it to transport.
                self._emit_ocr_schedule(decision.reason)
                if self.stabilizer is not None:
                    events = self.stabilizer.tick(time.monotonic())
                    if events:
                        for event in events:
                            if event.kind == "clear":
                                print("[ocr-stable] emit=clear", flush=True)
                        self._emit_stable_events(events)
                return
            self._last_ocr_trigger = decision.reason
            self.roi_width = roi_frame.width
            self.roi_height = roi_frame.height
        else:
            roi_frame, result, timings = await asyncio.to_thread(
                process_frame_timed,
                frame,
                runtime=self.runtime,
                app_id=self.args.app_id,
                resolver=self.resolver,
            )
            self._last_ocr_trigger = "ungated"
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
        if self.stabilizer is not None:
            self._stabilize(result)
        if self.gate is not None:
            needs = self.stabilizer.needs_confirmation if self.stabilizer is not None else False
            self.gate.note_ocr_result(needs)
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

    def _gated_frame(self, frame):
        """Decode + crop once, run the change gate, and OCR only when required."""
        decode_started = time.perf_counter()
        if isinstance(frame, DecodedFrame):
            decoded = decode_frame_ex(frame)
        else:
            decoded = decode_png_ex(frame.encoded_bytes)
        decode_done = time.perf_counter()
        roi_frame = recognition_roi.extract_recognition_roi(
            decoded.rgba,
            decoded.width,
            decoded.height,
            self.args.app_id,
            resolver=self._effective_resolver(),
        )
        crop_done = time.perf_counter()
        decision = self.gate.decide(
            roi_frame.rgba,
            roi_frame.width,
            roi_frame.height,
            frame.sequence,
            frame.captured_monotonic,
            roi_key=roi_frame.roi.as_tuple(),
        )
        timings = {
            "decode_ms": round((decode_done - decode_started) * 1000.0, 3),
            "roi_crop_ms": round((crop_done - decode_done) * 1000.0, 3),
            "change_check_ms": decision.change_check_ms,
            "ocr_wall_ms": 0.0,
        }
        result = None
        if decision.action == "ocr":
            result = self.runtime.recognize_rgba(
                roi_frame.rgba, roi_frame.width, roi_frame.height, sequence=frame.sequence
            )
            ocr_done = time.perf_counter()
            timings["ocr_wall_ms"] = round((ocr_done - crop_done) * 1000.0, 3)
            timings.update(self.runtime.last_timings)
        timings["total_frame_pipeline_ms"] = round(
            timings["decode_ms"] + timings["roi_crop_ms"] + timings["ocr_wall_ms"] + decision.change_check_ms, 3
        )
        return roi_frame, result, decision, timings

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

        # Stop the capture backend after the producer (no more pulls) so the
        # PipeWire pipeline transitions to NULL before the worker exits.
        backend = getattr(self, "_capture_backend", None)
        if backend is not None and hasattr(backend, "stop"):
            try:
                await asyncio.to_thread(backend.stop)
            except Exception as exc:
                print(f"[ocr] capture_backend_stop_failed detail={exc}", flush=True)

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
            if self.stabilizer is not None:
                print(f"[ocr-stable] stats={self.stabilizer.stats().__dict__}")
            if self._multi_region_enabled and self._multi_region_coordinator is not None:
                print(
                    f"[stabilizer-audit-summary] {self._multi_region_coordinator.audit_summary()}"
                )
                print(
                    f"[fast-accept-summary] {self._multi_region_coordinator.fast_accept_summary()}"
                )
            elif self.stabilizer is not None:
                print(f"[stabilizer-audit-summary] {self.stabilizer.audit_summary()}")
                print(f"[fast-accept-summary] {self.stabilizer.fast_accept_summary()}")
            if self.gate is not None:
                gate_stats = self.gate.stats()
                payload = dict(gate_stats.__dict__)
                payload["ocr_skip_ratio"] = gate_stats.ocr_skip_ratio
                payload["avg_change_check_ms"] = gate_stats.avg_change_check_ms
                print(f"[ocr-scheduler] stats={payload}")
            producer = getattr(self, "_producer", None)
            if producer is not None:
                status = producer.status()
                print(
                    f"[capture-producer] state={status.get('state')} "
                    f"frames_attempted={status.get('frames_attempted')} "
                    f"frames_succeeded={status.get('frames_succeeded')} "
                    f"frames_failed={status.get('frames_failed')} "
                    f"capture_cancellations={status.get('capture_cancellations')} "
                    f"shutdown_discarded_frames={status.get('shutdown_discarded_frames')} "
                    f"last_error_category={status.get('last_error_category')} "
                    f"last_error={status.get('last_error')}"
                )
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


def _parse_roi(text: str):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--debug-switch-roi must be x,y,w,h")
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--debug-switch-roi values must be numbers") from exc
    return ocr_roi_mod.NormalizedROI(x, y, w, h)


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
    parser.add_argument("--stable-output", action="store_true", help="enable OCR output stabilization")
    parser.add_argument(
        "--emit-stable-jsonl",
        action="store_true",
        help="emit stable events as JSONL on stdout (diagnostics go to stderr)",
    )
    parser.add_argument("--min-line-confidence", type=float, default=0.70)
    parser.add_argument("--consensus-required", type=int, default=2)
    parser.add_argument("--history-size", type=int, default=3)
    parser.add_argument("--stale-timeout-sec", type=float, default=2.0)
    parser.add_argument(
        "--diagnostic-ocr-evidence",
        action="store_true",
        help="emit bounded per-OCR-attempt evidence records to stderr (diagnostics only)",
    )
    parser.add_argument(
        "--multi-region",
        action="store_true",
        help="opt-in multi-region OCR execution (v2 region-tagged output)",
    )
    parser.add_argument(
        "--capture-backend",
        default=None,
        choices=("screenshot", "pipewire"),
        help="capture frame source (default: pipewire; screenshot is explicit diagnostic only)",
    )
    parser.add_argument("--change-gate", action="store_true", help="skip OCR when the ROI is unchanged")
    parser.add_argument("--force-ocr-interval-sec", type=float, default=3.0)
    parser.add_argument(
        "--debug-force-unchanged",
        action="store_true",
        help="diagnostic-only: force the scheduler-facing change result to unchanged",
    )
    parser.add_argument(
        "--debug-force-change-detector-error",
        action="store_true",
        help="diagnostic-only: inject a change-detector exception (fail-open path)",
    )
    parser.add_argument(
        "--debug-switch-roi-after-sec",
        type=float,
        default=None,
        help="diagnostic-only: switch the diagnostic ROI after N seconds",
    )
    parser.add_argument(
        "--debug-switch-roi",
        type=_parse_roi,
        default=None,
        help="diagnostic-only: ROI to switch to (x,y,w,h normalized)",
    )
    parser.add_argument(
        "--debug-reset-scheduler-after-sec",
        type=float,
        default=None,
        help="diagnostic-only: call the production scheduler reset after N seconds",
    )
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
    stabilizer = None
    if args.stable_output:
        try:
            stabilizer = OCRStabilizer(
                min_line_confidence=args.min_line_confidence,
                consensus_required=args.consensus_required,
                history_size=args.history_size,
                stale_timeout_sec=args.stale_timeout_sec,
            )
        except StabilizerConfigError as exc:
            print(f"[ocr] config_error error={exc.code} detail={exc}")
            return 2
    gate = None
    if args.change_gate:
        try:
            gate = OCRChangeGate(
                force_interval_sec=validate_force_interval(args.force_ocr_interval_sec),
                force_unchanged=bool(getattr(args, "debug_force_unchanged", False)),
                force_detector_error=bool(getattr(args, "debug_force_change_detector_error", False)),
            )
        except CaptureError as exc:
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
        emit_jsonl = bool(getattr(args, "emit_stable_jsonl", False))
        machine_stream = sys.stdout if emit_jsonl else None
        diagnostic = OCRDiagnostic(
            args, runtime, stabilizer=stabilizer, gate=gate, machine_stream=machine_stream
        )
        try:
            if emit_jsonl:
                # machine JSONL stays on stdout; all diagnostics move to stderr
                with contextlib.redirect_stdout(sys.stderr):
                    return asyncio.run(diagnostic.run_live())
            return asyncio.run(diagnostic.run_live())
        except KeyboardInterrupt:
            print("[ocr] interrupted", file=sys.stderr, flush=True)
            return 130
        except asyncio.CancelledError:
            print("[ocr] interrupted", file=sys.stderr, flush=True)
            return 130
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
