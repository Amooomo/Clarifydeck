#!/usr/bin/env python3
"""Production OCR worker entrypoint (Phase 2I.2).

Long-running child of the Decky backend leader. Runs the validated OCR pipeline
(capture -> ROI -> optional change gate -> PP-OCRv6 -> stabilizer) and emits
stable-text protocol v1 JSONL on **stdout**; all diagnostics go to **stderr**.

This process is the only place that imports native OCR dependencies
(rapidocr / onnxruntime / numpy / cv2 / omegaconf / antlr4); the Decky backend
never imports them.

Run:
    /usr/bin/python3 scripts/ocr_worker.py --parent-pid <pid> --fps 1 \
        --model-dir models/ppocrv6
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import ocr_test  # noqa: E402
from backend import parent_death  # noqa: E402
from capture.scheduler import OCRChangeGate, validate_force_interval  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from ocr import OCRConfig, OCRRuntime, OCRStabilizer, StabilizerConfigError, activate_plugin_ocr_runtime  # noqa: E402

LONG_RUN_SECONDS = 86400.0


def _start_parent_watchdog(expected_parent_pid) -> None:
    """Secondary defense: exit cleanly if the parent relationship changes.

    Uses ``getppid`` (parent relationship), never bare PID existence, so a zombie
    or PID-reused parent cannot keep the worker alive.
    """
    if expected_parent_pid is None or expected_parent_pid <= 1:
        return

    def watch() -> None:
        while True:
            time.sleep(0.5)
            if parent_death.parent_changed(expected_parent_pid):
                print(
                    f"[ocr-worker] parent_gone expected={expected_parent_pid} "
                    f"actual={os.getppid()}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                except Exception:
                    os._exit(0)
                return

    threading.Thread(target=watch, name="ocr-worker-parent-watchdog", daemon=True).start()


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ClarifyDeck production OCR worker")
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--duration-sec", type=float, default=LONG_RUN_SECONDS)
    parser.add_argument("--model-dir", default=str(ROOT / "models" / "ppocrv6"))
    parser.add_argument("--roi-config", default=None)
    parser.add_argument("--app-id", default=None)
    parser.add_argument("--change-gate", action="store_true")
    parser.add_argument("--force-ocr-interval-sec", type=float, default=3.0)
    parser.add_argument(
        "--diagnostic-ocr-evidence",
        action="store_true",
        help="emit bounded per-OCR-attempt evidence records to stderr (diagnostics only)",
    )
    parser.add_argument(
        "--multi-region",
        action="store_true",
        help="opt-in multi-region OCR execution (v2 region-tagged output; not production-default)",
    )
    parser.add_argument(
        "--capture-backend",
        default=None,
        choices=("screenshot", "pipewire"),
        help="capture frame source (default: CLARIFYDECK_CAPTURE_BACKEND or screenshot)",
    )
    parser.add_argument("--min-line-confidence", type=float, default=0.70)
    parser.add_argument("--consensus-required", type=int, default=2)
    parser.add_argument("--history-size", type=int, default=3)
    parser.add_argument("--stale-timeout-sec", type=float, default=2.0)
    parser.add_argument("--ort-intra-threads", type=int, default=2)
    parser.add_argument("--ort-inter-threads", type=int, default=1)
    parser.add_argument("--opencv-threads", type=int, default=1)
    parser.add_argument("--det-limit-side-len", type=int, default=256)
    parser.add_argument("--det-limit-type", default="min", choices=("min", "max"))
    return parser.parse_args(argv)


def _diagnostic_args(args: argparse.Namespace) -> argparse.Namespace:
    """Shape the worker args into what OCRDiagnostic expects."""
    return argparse.Namespace(
        live=True,
        mock=False,
        no_ocr=False,
        fps=args.fps,
        duration_sec=args.duration_sec,
        app_id=args.app_id,
        json=False,
        debug=False,
        debug_roi_copy=None,
        replay_roi=None,
        ort_intra_threads=args.ort_intra_threads,
        ort_inter_threads=args.ort_inter_threads,
        opencv_threads=args.opencv_threads,
        det_limit_side_len=args.det_limit_side_len,
        det_limit_type=args.det_limit_type,
        change_gate=args.change_gate,
        force_ocr_interval_sec=args.force_ocr_interval_sec,
        debug_switch_roi=None,
        debug_switch_roi_after_sec=None,
        debug_reset_scheduler_after_sec=None,
        stable_output=True,
        emit_stable_jsonl=True,
        diagnostic_ocr_evidence=args.diagnostic_ocr_evidence,
        multi_region=args.multi_region,
        capture_backend=args.capture_backend,
    )


def main(argv=None) -> int:
    args = _parse_args(argv)

    # Arm orphan safety EARLY, before any OCR/native initialization.
    try:
        arm_result = parent_death.setup_parent_death(args.parent_pid, signal.SIGINT)
    except parent_death.ParentDeathError as exc:
        print(
            f"[ocr-worker] parent_death_error code={exc.code} detail={exc}",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(
        f"[ocr-worker] parent_death armed={arm_result['armed']} reason={arm_result['reason']} "
        f"parent={args.parent_pid}",
        file=sys.stderr,
        flush=True,
    )

    try:
        activate_plugin_ocr_runtime(require=True)
    except Exception as exc:
        print(f"[ocr-worker] runtime_unavailable detail={exc}", file=sys.stderr, flush=True)
        return 2

    if args.roi_config:
        from capture import recognition_roi

        recognition_roi.configure(Path(args.roi_config))

    try:
        runtime = OCRRuntime(
            OCRConfig(
                model_dir=Path(args.model_dir).expanduser(),
                ort_intra_threads=args.ort_intra_threads,
                ort_inter_threads=args.ort_inter_threads,
                opencv_threads=args.opencv_threads,
                det_limit_side_len=args.det_limit_side_len,
                det_limit_type=args.det_limit_type,
            )
        )
        stabilizer = OCRStabilizer(
            min_line_confidence=args.min_line_confidence,
            consensus_required=args.consensus_required,
            history_size=args.history_size,
            stale_timeout_sec=args.stale_timeout_sec,
        )
        gate = None
        if args.change_gate:
            gate = OCRChangeGate(force_interval_sec=validate_force_interval(args.force_ocr_interval_sec))
    except (CaptureError, StabilizerConfigError) as exc:
        print(f"[ocr-worker] config_error detail={exc}", file=sys.stderr, flush=True)
        return 2

    machine_stream = sys.stdout
    diagnostic = ocr_test.OCRDiagnostic(
        _diagnostic_args(args),
        runtime,
        stabilizer=stabilizer,
        gate=gate,
        machine_stream=machine_stream,
    )
    _start_parent_watchdog(args.parent_pid)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return asyncio.run(diagnostic.run_live())
    except KeyboardInterrupt:
        print("[ocr-worker] interrupted", file=sys.stderr, flush=True)
        return 130
    except asyncio.CancelledError:
        print("[ocr-worker] interrupted", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
