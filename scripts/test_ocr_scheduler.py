#!/usr/bin/env python3
"""Phase 2H tests: change-gated OCR scheduling.

Run:
    python3 scripts/test_ocr_scheduler.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import ocr_test  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.scheduler import OCRChangeGate, validate_force_interval  # noqa: E402
from ocr import OCRConfig, OCRRuntime, OCRStabilizer  # noqa: E402
from ocr.runtime import _RapidOCREngine  # noqa: E402


class FakeDetector:
    def __init__(self, changes) -> None:
        self._changes = list(changes)
        self._index = 0
        self.reset_calls = 0

    def classify_rgba(self, **kwargs):
        if not self._changes:
            changed = False
        else:
            changed = self._changes[min(self._index, len(self._changes) - 1)]
        self._index += 1
        return SimpleNamespace(
            changed=changed, score=0.0, reason="changed" if changed else "below_threshold", age_ms=0.0
        )

    def reset_state(self) -> None:
        self.reset_calls += 1
        self._index = 0


class BrokenDetector:
    def classify_rgba(self, **kwargs):
        raise RuntimeError("detector boom")

    def reset_state(self) -> None:
        pass


def _clock(values):
    iterator = iter(values)
    return lambda: next(iterator)


class GateUnitTest(unittest.TestCase):
    def test_first_frame_always_ocr(self) -> None:
        gate = OCRChangeGate(FakeDetector([]), force_interval_sec=3.0, clock=_clock([0.0]))
        decision = gate.decide(b"\x00" * 4, 1, 1, 1, 0.0)
        self.assertEqual(decision.action, "ocr")
        self.assertEqual(decision.reason, "first_frame")

    def test_changed_frame_triggers_ocr(self) -> None:
        gate = OCRChangeGate(FakeDetector([True, True]), force_interval_sec=3.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual((decision.action, decision.reason), ("ocr", "change"))

    def test_unchanged_frame_skips(self) -> None:
        gate = OCRChangeGate(FakeDetector([True, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual((decision.action, decision.reason), ("skip", "unchanged"))

    def test_forced_refresh_after_interval(self) -> None:
        gate = OCRChangeGate(FakeDetector([True, False, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5, 3.5]))
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.decide(b"", 1, 1, 2, 0.5)
        decision = gate.decide(b"", 1, 1, 3, 3.5)
        self.assertEqual((decision.action, decision.reason), ("ocr", "forced_refresh"))

    def test_forced_refresh_not_early(self) -> None:
        gate = OCRChangeGate(FakeDetector([True, False, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5, 2.9]))
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.decide(b"", 1, 1, 2, 0.5)
        decision = gate.decide(b"", 1, 1, 3, 2.9)
        self.assertEqual(decision.action, "skip")

    def test_changed_reason_wins_over_forced(self) -> None:
        gate = OCRChangeGate(FakeDetector([True, True]), force_interval_sec=1.0, clock=_clock([0.0, 5.0]))
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 5.0)
        self.assertEqual(decision.reason, "change")

    def test_detector_failure_fails_open(self) -> None:
        gate = OCRChangeGate(BrokenDetector(), force_interval_sec=3.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(decision.action, "ocr")
        self.assertEqual(decision.reason, "detector_error")
        self.assertGreaterEqual(gate.stats().change_detector_errors, 1)

    def test_geometry_change_resets_baseline(self) -> None:
        detector = FakeDetector([True, False])
        gate = OCRChangeGate(detector, force_interval_sec=3.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 10, 10, 1, 0.0)
        decision = gate.decide(b"", 20, 20, 2, 0.5)
        self.assertEqual(decision.reason, "first_frame")
        # one reset for the initial baseline, one for the geometry change
        self.assertEqual(detector.reset_calls, 2)

    def test_reset_forces_next_ocr(self) -> None:
        gate = OCRChangeGate(FakeDetector([True, False, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5, 0.6]))
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.decide(b"", 1, 1, 2, 0.5)
        gate.reset()
        decision = gate.decide(b"", 1, 1, 3, 0.6)
        self.assertEqual(decision.reason, "first_frame")

    def test_invalid_force_interval_rejected(self) -> None:
        for bad in (0.0, -1.0, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(CaptureError) as ctx:
                    validate_force_interval(bad)
                self.assertEqual(ctx.exception.code, "invalid_force_interval")

    def test_counters_and_ratio(self) -> None:
        gate = OCRChangeGate(FakeDetector([True, False, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5, 0.6]))
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.decide(b"", 1, 1, 2, 0.5)
        gate.decide(b"", 1, 1, 3, 0.6)
        stats = gate.stats()
        self.assertEqual(stats.frames_seen, 3)
        self.assertEqual(stats.ocr_calls, 1)
        self.assertEqual(stats.ocr_skipped, 2)
        self.assertEqual(stats.ocr_trigger_first, 1)
        self.assertAlmostEqual(stats.ocr_skip_ratio, round(2 / 3, 3), places=3)
        self.assertIsNotNone(stats.avg_change_check_ms)


class CountingEngine:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, image):
        self.calls += 1

        class Result:
            boxes = None
            txts = ("稳定字幕",)
            scores = (0.95,)

        return Result()


def _model_dir() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="clarifydeck-sched-")) / "ppocrv6"
    directory.mkdir(parents=True)
    for name in ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx"):
        (directory / name).write_bytes(b"x")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": 2,
                "engine": "rapidocr",
                "family": "PP-OCRv6",
                "model_type": "small",
                "engine_type": "onnxruntime",
                "files": {
                    "det": {"path": "PP-OCRv6_det_small.onnx"},
                    "rec": {"path": "PP-OCRv6_rec_small.onnx"},
                },
                "dictionary": {"mode": "embedded"},
            }
        ),
        encoding="utf-8",
    )
    return directory


def _args(**overrides) -> argparse.Namespace:
    base = {
        "live": False,
        "mock": True,
        "no_ocr": False,
        "fps": 4.0,
        "duration_sec": 1.3,
        "app_id": None,
        "json": False,
        "debug": False,
        "debug_roi_copy": None,
        "replay_roi": None,
        "ort_intra_threads": None,
        "ort_inter_threads": None,
        "opencv_threads": None,
        "det_limit_side_len": 736,
        "det_limit_type": "min",
        "change_gate": False,
        "force_ocr_interval_sec": 3.0,
        "debug_switch_roi": None,
        "debug_switch_roi_after_sec": None,
        "debug_reset_scheduler_after_sec": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class GateIntegrationTest(unittest.TestCase):
    def _run(self, *, gate, stabilizer=None):
        engine = CountingEngine()
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(engine))
        diagnostic = ocr_test.OCRDiagnostic(_args(), runtime, stabilizer=stabilizer, gate=gate)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(diagnostic.run_live())
        return code, out.getvalue(), runtime, engine, diagnostic

    def test_skip_never_calls_engine(self) -> None:
        gate = OCRChangeGate(force_interval_sec=0.6)
        code, output, runtime, engine, diagnostic = self._run(gate=gate)
        self.assertEqual(code, 0)
        stats = gate.stats()
        self.assertEqual(engine.calls, stats.ocr_calls)
        self.assertGreater(stats.ocr_skipped, 0)
        self.assertGreaterEqual(stats.ocr_trigger_forced, 1)
        self.assertEqual(runtime.engine_init_count, 1)

    def test_skip_does_not_increment_stabilizer_raw_frames(self) -> None:
        gate = OCRChangeGate(force_interval_sec=0.6)
        stabilizer = OCRStabilizer()
        code, output, runtime, engine, diagnostic = self._run(gate=gate, stabilizer=stabilizer)
        self.assertEqual(code, 0)
        self.assertEqual(stabilizer.stats().raw_frames, gate.stats().ocr_calls)
        self.assertLess(stabilizer.stats().raw_frames, gate.stats().frames_seen)

    def test_gate_off_has_no_scheduler_output(self) -> None:
        code, output, runtime, engine, diagnostic = self._run(gate=None)
        self.assertEqual(code, 0)
        self.assertNotIn("[ocr-scheduler]", output)

    def test_scheduler_stats_printed(self) -> None:
        gate = OCRChangeGate(force_interval_sec=0.6)
        code, output, runtime, engine, diagnostic = self._run(gate=gate)
        self.assertIn("[ocr-scheduler] stats=", output)
        self.assertIn("ocr_skip_ratio", output)


class ConfirmationUnitTest(unittest.TestCase):
    """Phase 2H.2: post-change confirmation scheduling."""

    def _gate(self, changes, times, force=3.0):
        return OCRChangeGate(FakeDetector(changes), force_interval_sec=force, clock=_clock(times))

    def test_change_arms_two_attempts(self) -> None:
        gate = self._gate([True, False], [0.0, 0.5])
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        self.assertTrue(gate.confirmation_pending)
        self.assertEqual(gate.confirmation_remaining, 2)
        self.assertEqual(gate.stats().confirmation_armed, 1)

    def test_confirmation_retries_once_then_exhausts(self) -> None:
        gate = self._gate([False, False, False, False], [0.0, 0.5, 1.0, 1.5], force=3.0)
        gate.decide(b"", 1, 1, 1, 0.0)  # first_frame arms budget
        gate.note_ocr_result(True)  # armed
        first = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(first.reason, "confirmation")
        self.assertEqual(first.confirmation_remaining, 1)
        gate.note_ocr_result(True)  # retry scheduled
        self.assertTrue(gate.confirmation_pending)
        self.assertEqual(gate.stats().confirmation_retried, 1)
        second = gate.decide(b"", 1, 1, 3, 1.0)
        self.assertEqual(second.reason, "confirmation")
        self.assertEqual(second.confirmation_remaining, 0)
        gate.note_ocr_result(True)  # exhausted
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(gate.stats().confirmation_exhausted, 1)
        third = gate.decide(b"", 1, 1, 4, 1.5)
        self.assertEqual(third.action, "skip")

    def test_confirmation_one_stable_clears_immediately(self) -> None:
        gate = self._gate([False, False, False], [0.0, 0.5, 1.0], force=3.0)
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        gate.decide(b"", 1, 1, 2, 0.5)  # confirmation #1
        gate.note_ocr_result(False)
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(gate.stats().confirmation_retried, 0)
        decision = gate.decide(b"", 1, 1, 3, 1.0)
        self.assertEqual(decision.action, "skip")

    def test_confirmation_outranks_forced_refresh(self) -> None:
        gate = self._gate([True, False], [0.0, 5.0], force=3.0)
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        decision = gate.decide(b"", 1, 1, 2, 5.0)
        self.assertEqual(decision.reason, "confirmation")

    def test_new_change_supersedes_confirmation(self) -> None:
        gate = self._gate([True, True], [0.0, 0.5])
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(decision.reason, "change")
        self.assertEqual(gate.stats().confirmation_superseded, 1)
        self.assertFalse(gate.confirmation_pending)

    def test_forced_refresh_after_exhaustion(self) -> None:
        gate = self._gate([False, False, False, False], [0.0, 0.5, 1.0, 4.5], force=3.0)
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        gate.decide(b"", 1, 1, 2, 0.5)
        gate.note_ocr_result(True)
        gate.decide(b"", 1, 1, 3, 1.0)
        gate.note_ocr_result(True)  # exhausted
        decision = gate.decide(b"", 1, 1, 4, 4.5)
        self.assertEqual(decision.reason, "forced_refresh")

    def test_duplicate_stable_does_not_arm(self) -> None:
        gate = self._gate([True, False], [0.0, 0.5])
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(False)
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(gate.stats().confirmation_armed, 0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(decision.action, "skip")

    def test_reset_clears_remaining(self) -> None:
        gate = self._gate([True, False], [0.0, 0.5])
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        gate.reset()
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(gate.confirmation_remaining, 0)
        # counters persist across an explicit session reset (observability)
        self.assertGreaterEqual(gate.stats().confirmation_armed, 1)

    def test_geometry_reset_clears_remaining(self) -> None:
        gate = self._gate([True, False], [0.0, 0.5])
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        decision = gate.decide(b"", 9, 9, 2, 0.5)
        # geometry change re-baselines: old retries cleared, treated as first_frame
        self.assertEqual(decision.reason, "first_frame")
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(gate.confirmation_remaining, 2)

    def test_first_frame_unchanged(self) -> None:
        gate = self._gate([False], [0.0])
        decision = gate.decide(b"", 1, 1, 1, 0.0)
        self.assertEqual(decision.reason, "first_frame")

    def test_detector_failure_fails_open(self) -> None:
        gate = OCRChangeGate(BrokenDetector(), force_interval_sec=3.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(decision.reason, "detector_error")

    def test_forced_refresh_still_available(self) -> None:
        gate = self._gate([True, False, False], [0.0, 0.5, 4.0], force=3.0)
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.decide(b"", 1, 1, 2, 0.5)  # unchanged skip
        decision = gate.decide(b"", 1, 1, 3, 4.0)
        self.assertEqual(decision.reason, "forced_refresh")


class TextEngine:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        text = self.text

        class Result:
            boxes = None
            txts = (text,)
            scores = (0.95,)

        return Result()


class ConfirmationIntegrationTest(unittest.TestCase):
    def _run(self, *, changes, text="稳定字幕", force=3.0, stabilizer=None, duration=1.3):
        engine = TextEngine(text)
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(engine))
        gate = OCRChangeGate(FakeDetector(changes), force_interval_sec=force)
        stabilizer = stabilizer or OCRStabilizer()
        diagnostic = ocr_test.OCRDiagnostic(
            _args(duration_sec=duration, debug=True), runtime, stabilizer=stabilizer, gate=gate
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(diagnostic.run_live())
        return code, out.getvalue(), runtime, engine, gate, stabilizer

    def test_confirmation_promptly_reaches_consensus(self) -> None:
        code, output, runtime, engine, gate, stabilizer = self._run(changes=[False, False, False, False, False])
        self.assertEqual(code, 0)
        stats = gate.stats()
        self.assertEqual(stats.ocr_trigger_first, 1)
        self.assertGreaterEqual(stats.ocr_trigger_confirmation, 1)
        self.assertEqual(stats.ocr_trigger_forced, 0)
        self.assertGreaterEqual(stabilizer.stats().stable_text_emits, 1)
        self.assertIn("reason=confirmation", output)

    def test_confirmation_is_a_real_observation(self) -> None:
        code, output, runtime, engine, gate, stabilizer = self._run(changes=[False, False, False, False, False])
        self.assertEqual(stabilizer.stats().raw_frames, gate.stats().ocr_calls)
        self.assertEqual(engine.calls, gate.stats().ocr_calls)
        self.assertEqual(runtime.engine_init_count, 1)

    def test_no_confirmation_when_consensus_immediate(self) -> None:
        stabilizer = OCRStabilizer(consensus_required=1, history_size=1)
        code, output, runtime, engine, gate, stabilizer = self._run(
            changes=[False, False, False], stabilizer=stabilizer
        )
        self.assertEqual(gate.stats().ocr_trigger_confirmation, 0)
        self.assertGreaterEqual(stabilizer.stats().stable_text_emits, 1)


class ForceUnchangedTest(unittest.TestCase):
    """Phase 2H H4: diagnostic force-unchanged hook."""

    def _gate(self, changes, times, force=3.0, force_unchanged=False):
        return OCRChangeGate(
            FakeDetector(changes),
            force_interval_sec=force,
            force_unchanged=force_unchanged,
            clock=_clock(times),
        )

    def test_default_off_preserves_behavior(self) -> None:
        gate = self._gate([True, True], [0.0, 0.5])
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(decision.reason, "change")

    def test_force_unchanged_suppresses_change(self) -> None:
        gate = self._gate([True, True, True], [0.0, 0.5, 1.0], force=3.0, force_unchanged=True)
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertNotEqual(decision.reason, "change")
        self.assertEqual(gate.stats().ocr_trigger_change, 0)

    def test_first_frame_still_ocrs(self) -> None:
        gate = self._gate([True], [0.0], force_unchanged=True)
        decision = gate.decide(b"", 1, 1, 1, 0.0)
        self.assertEqual((decision.action, decision.reason), ("ocr", "first_frame"))

    def test_scheduler_facing_changed_false(self) -> None:
        gate = self._gate([True, True], [0.0, 0.5], force_unchanged=True)
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertIs(decision.changed, False)

    def test_forced_refresh_still_fires(self) -> None:
        gate = self._gate([True, True], [0.0, 1.5], force=1.0, force_unchanged=True)
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 1.5)
        self.assertEqual((decision.action, decision.reason), ("ocr", "forced_refresh"))

    def test_forced_refresh_can_arm_confirmation(self) -> None:
        gate = self._gate([True, True, True], [0.0, 1.5, 2.0], force=1.0, force_unchanged=True)
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.decide(b"", 1, 1, 2, 1.5)  # forced_refresh
        gate.note_ocr_result(True)
        decision = gate.decide(b"", 1, 1, 3, 2.0)
        self.assertEqual(decision.reason, "confirmation")

    def test_override_does_not_change_detector(self) -> None:
        from capture.roi import ROI_CHANGE_THRESHOLD, ROI_GRID, ROIChangeDetector

        detector = ROIChangeDetector()
        gate = OCRChangeGate(detector, force_interval_sec=3.0, force_unchanged=True)
        self.assertEqual(gate._detector._threshold, ROI_CHANGE_THRESHOLD)
        self.assertEqual(gate._detector._grid, ROI_GRID)


class ForceUnchangedIntegrationTest(unittest.TestCase):
    def test_force_unchanged_never_triggers_change(self) -> None:
        engine = TextEngine("字幕")
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(engine))
        gate = OCRChangeGate(FakeDetector([True, True, True, True, True]), force_interval_sec=0.3, force_unchanged=True)
        stabilizer = OCRStabilizer()
        diagnostic = ocr_test.OCRDiagnostic(
            _args(duration_sec=1.4, debug=True), runtime, stabilizer=stabilizer, gate=gate
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        stats = gate.stats()
        self.assertEqual(stats.ocr_trigger_change, 0)
        self.assertEqual(stats.ocr_trigger_first, 1)
        self.assertGreaterEqual(stats.ocr_trigger_forced, 1)
        self.assertNotIn("reason=change", out.getvalue())


class DetectorErrorTest(unittest.TestCase):
    """Phase 2H H5: change detector failure fails open to OCR."""

    def _gate(self, changes, times, force=3.0, force_detector_error=False):
        return OCRChangeGate(
            FakeDetector(changes),
            force_interval_sec=force,
            force_detector_error=force_detector_error,
            clock=_clock(times),
        )

    def test_default_off(self) -> None:
        gate = self._gate([True, True], [0.0, 0.5])
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(decision.reason, "change")
        self.assertEqual(gate.stats().change_detector_errors, 0)

    def test_injected_error_fails_open(self) -> None:
        gate = self._gate([False, False], [0.0, 0.5], force_detector_error=True)
        gate.decide(b"", 1, 1, 1, 0.0)
        decision = gate.decide(b"", 1, 1, 2, 0.5)
        self.assertEqual(decision.action, "ocr")
        self.assertEqual(decision.reason, "detector_error")
        self.assertGreaterEqual(gate.stats().change_detector_errors, 1)
        self.assertGreaterEqual(gate.stats().ocr_trigger_detector_error, 1)

    def test_first_frame_still_ocrs(self) -> None:
        gate = self._gate([False], [0.0], force_detector_error=True)
        decision = gate.decide(b"", 1, 1, 1, 0.0)
        self.assertEqual((decision.action, decision.reason), ("ocr", "first_frame"))

    def test_repeated_errors_never_skip(self) -> None:
        gate = self._gate([False] * 6, [0.0, 0.1, 0.2, 0.3, 0.4, 0.5], force_detector_error=True)
        actions = []
        for index in range(6):
            actions.append(gate.decide(b"", 1, 1, index + 1, index * 0.1).action)
        self.assertTrue(all(action == "ocr" for action in actions))
        self.assertGreaterEqual(gate.stats().change_detector_errors, 5)

    def test_detector_error_clears_confirmation(self) -> None:
        gate = self._gate([False, False], [0.0, 0.5], force_detector_error=True)
        gate.decide(b"", 1, 1, 1, 0.0)
        gate.note_ocr_result(True)
        self.assertTrue(gate.confirmation_pending)
        gate.decide(b"", 1, 1, 2, 0.5)
        self.assertFalse(gate.confirmation_pending)


class DetectorErrorIntegrationTest(unittest.TestCase):
    def test_force_detector_error_fails_open_end_to_end(self) -> None:
        engine = TextEngine("字幕")
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(engine))
        gate = OCRChangeGate(FakeDetector([False] * 6), force_interval_sec=3.0, force_detector_error=True)
        stabilizer = OCRStabilizer()
        diagnostic = ocr_test.OCRDiagnostic(
            _args(duration_sec=1.3, debug=True), runtime, stabilizer=stabilizer, gate=gate
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        stats = gate.stats()
        self.assertGreaterEqual(stats.change_detector_errors, 1)
        self.assertGreaterEqual(stats.ocr_calls, 1)
        self.assertEqual(stats.ocr_skipped, 0)
        self.assertEqual(runtime.engine_init_count, 1)
        self.assertEqual(stabilizer.stats().raw_frames, stats.ocr_calls)
        self.assertEqual(diagnostic.ocr_errors, 0)
        self.assertIn("reason=detector_error", out.getvalue())


class RoiGeometryResetTest(unittest.TestCase):
    """Phase 2H H6: ROI geometry change resets detector + confirmation state."""

    def _gate(self, changes, times, force=3.0):
        return OCRChangeGate(FakeDetector(changes), force_interval_sec=force, clock=_clock(times))

    def test_geometry_change_resets_and_ocrs_immediately(self) -> None:
        detector = FakeDetector([True, False, False])
        gate = OCRChangeGate(detector, force_interval_sec=3.0, clock=_clock([0.0, 0.5, 1.0]))
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(10, 10, 100, 20))
        gate.note_ocr_result(True)
        self.assertTrue(gate.confirmation_pending)
        decision = gate.decide(b"", 100, 20, 2, 0.5, roi_key=(10, 5, 100, 20))
        self.assertTrue(decision.roi_geometry_changed)
        self.assertEqual((decision.action, decision.reason), ("ocr", "first_frame"))
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(detector.reset_calls, 2)  # initial baseline + geometry change

    def test_geometry_change_does_not_use_confirmation_or_forced(self) -> None:
        gate = self._gate([False, False, False], [0.0, 5.0, 5.1], force=3.0)
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        decision = gate.decide(b"", 100, 20, 2, 5.0, roi_key=(0, 5, 100, 20))
        self.assertEqual(decision.reason, "first_frame")

    def test_fresh_budget_after_geometry_change(self) -> None:
        gate = self._gate([False, False, False], [0.0, 0.5, 1.0])
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        gate.note_ocr_result(True)
        gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 0, 100, 20))  # confirmation -> remaining 1
        self.assertEqual(gate.confirmation_remaining, 1)
        gate.decide(b"", 100, 20, 3, 1.0, roi_key=(0, 5, 100, 20))  # geometry change re-arms
        self.assertEqual(gate.confirmation_remaining, 2)

    def test_new_baseline_then_skip(self) -> None:
        gate = self._gate([False, False, False], [0.0, 0.5, 1.0])
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 5, 100, 20))  # first_frame for new ROI
        decision = gate.decide(b"", 100, 20, 3, 1.0, roi_key=(0, 5, 100, 20))
        self.assertEqual(decision.action, "skip")
        self.assertFalse(decision.roi_geometry_changed)

    def test_same_geometry_no_spurious_reset(self) -> None:
        detector = FakeDetector([False, False, False])
        gate = OCRChangeGate(detector, force_interval_sec=3.0, clock=_clock([0.0, 0.5, 1.0]))
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        first = gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 0, 100, 20))
        second = gate.decide(b"", 100, 20, 3, 1.0, roi_key=(0, 0, 100, 20))
        self.assertFalse(first.roi_geometry_changed)
        self.assertFalse(second.roi_geometry_changed)
        self.assertEqual(detector.reset_calls, 1)

    def test_reset_preserves_detector_and_force_settings(self) -> None:
        from capture.roi import ROI_CHANGE_THRESHOLD, ROI_GRID, ROIChangeDetector

        detector = ROIChangeDetector()
        gate = OCRChangeGate(detector, force_interval_sec=2.5)
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 5, 100, 20))
        self.assertEqual(detector._threshold, ROI_CHANGE_THRESHOLD)
        self.assertEqual(detector._grid, ROI_GRID)
        self.assertEqual(gate.force_interval_sec, 2.5)


class RoiSwitchIntegrationTest(unittest.TestCase):
    def test_roi_switch_resets_and_rebaselines(self) -> None:
        from capture.roi import NormalizedROI

        engine = TextEngine("字幕")
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(engine))
        gate = OCRChangeGate(FakeDetector([False] * 8), force_interval_sec=3.0)
        stabilizer = OCRStabilizer()
        args = _args(
            duration_sec=1.6,
            debug=True,
            debug_switch_roi=NormalizedROI(0.0, 0.0, 1.0, 1.0),
            debug_switch_roi_after_sec=0.4,
        )
        diagnostic = ocr_test.OCRDiagnostic(args, runtime, stabilizer=stabilizer, gate=gate)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("debug_roi_switch", text)
        self.assertIn("roi_geometry_changed", text)
        self.assertEqual(text.count("reason=first_frame"), 2)
        self.assertEqual(runtime.engine_init_count, 1)


class SessionResetTest(unittest.TestCase):
    """Phase 2H H7: explicit scheduler/session reset boundary."""

    def test_reset_clears_all_transient_state_and_ocrs(self) -> None:
        detector = FakeDetector([True, False, False])
        gate = OCRChangeGate(detector, force_interval_sec=2.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        gate.note_ocr_result(True)
        self.assertTrue(gate.confirmation_pending)
        gate.reset()
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(gate.confirmation_remaining, 0)
        self.assertEqual(detector.reset_calls, 2)  # initial baseline + reset
        decision = gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 0, 100, 20))
        self.assertEqual((decision.action, decision.reason), ("ocr", "first_frame"))

    def test_reset_clears_force_timing(self) -> None:
        gate = OCRChangeGate(FakeDetector([False, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        gate.reset()
        self.assertIsNone(gate._last_ocr_time)
        decision = gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 0, 100, 20))
        self.assertEqual(decision.reason, "first_frame")

    def test_reset_does_not_use_confirmation_or_forced(self) -> None:
        gate = OCRChangeGate(FakeDetector([False, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5]))
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        gate.note_ocr_result(True)
        gate.reset()
        decision = gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 0, 100, 20))
        self.assertNotEqual(decision.reason, "confirmation")
        self.assertNotEqual(decision.reason, "forced_refresh")

    def test_reset_with_no_prior_frame_is_safe(self) -> None:
        gate = OCRChangeGate(FakeDetector([False]), force_interval_sec=3.0, clock=_clock([0.0]))
        gate.reset()
        decision = gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        self.assertEqual(decision.reason, "first_frame")

    def test_repeated_reset_is_idempotent(self) -> None:
        gate = OCRChangeGate(FakeDetector([False]), force_interval_sec=3.0, clock=_clock([0.0]))
        gate.reset()
        gate.reset()
        self.assertFalse(gate.confirmation_pending)
        self.assertEqual(gate.confirmation_remaining, 0)
        self.assertIsNone(gate._geometry)

    def test_unchanged_after_fresh_baseline_skips(self) -> None:
        gate = OCRChangeGate(FakeDetector([False, False, False]), force_interval_sec=3.0, clock=_clock([0.0, 0.5, 1.0]))
        gate.decide(b"", 100, 20, 1, 0.0, roi_key=(0, 0, 100, 20))
        gate.reset()
        gate.decide(b"", 100, 20, 2, 0.5, roi_key=(0, 0, 100, 20))  # first_frame after reset
        decision = gate.decide(b"", 100, 20, 3, 1.0, roi_key=(0, 0, 100, 20))
        self.assertEqual(decision.action, "skip")

    def test_reset_preserves_settings(self) -> None:
        from capture.roi import ROI_CHANGE_THRESHOLD, ROI_GRID, ROIChangeDetector

        detector = ROIChangeDetector()
        gate = OCRChangeGate(detector, force_interval_sec=2.5, max_confirmation_attempts=2)
        gate.reset()
        self.assertEqual(detector._threshold, ROI_CHANGE_THRESHOLD)
        self.assertEqual(detector._grid, ROI_GRID)
        self.assertEqual(gate.force_interval_sec, 2.5)


class SessionResetIntegrationTest(unittest.TestCase):
    def test_debug_reset_resets_and_rebaselines(self) -> None:
        engine = TextEngine("字幕")
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(engine))
        gate = OCRChangeGate(FakeDetector([False] * 8), force_interval_sec=3.0)
        stabilizer = OCRStabilizer()
        args = _args(duration_sec=1.6, debug=True, debug_reset_scheduler_after_sec=0.4)
        diagnostic = ocr_test.OCRDiagnostic(args, runtime, stabilizer=stabilizer, gate=gate)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("debug_scheduler_reset", text)
        self.assertEqual(text.count("reason=first_frame"), 2)
        self.assertEqual(gate.stats().ocr_trigger_first, 2)
        self.assertEqual(runtime.engine_init_count, 1)


class JsonlEmitIntegrationTest(unittest.TestCase):
    def test_emit_stable_jsonl_to_machine_stream(self) -> None:
        from backend.ocr_transport import OCRTransportReceiver

        engine = TextEngine("字幕")
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(engine))
        gate = OCRChangeGate(FakeDetector([False] * 6), force_interval_sec=3.0)
        stabilizer = OCRStabilizer()
        machine = io.StringIO()
        diagnostic = ocr_test.OCRDiagnostic(
            _args(duration_sec=1.3), runtime, stabilizer=stabilizer, gate=gate, machine_stream=machine
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        lines = [line for line in machine.getvalue().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        receiver = OCRTransportReceiver()
        for line in lines:
            self.assertTrue(receiver.handle_line(line))
        self.assertEqual(receiver.state().kind, "text")
        self.assertEqual(receiver.state().text, "字幕")
        self.assertEqual(receiver.state().last_event_seq, len(lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
