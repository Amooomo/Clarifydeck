#!/usr/bin/env python3
"""Phase 2K.2.1 tests: stable clear emission from real no-text OCR evidence.

Covers stabilizer no-text semantics and the worker/pipeline adapter that must
forward a clear emitted during a skipped (change-gated) frame to transport.

Run:
    python3 scripts/test_ocr_clear_emission.py
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import ocr_test  # noqa: E402
from ocr.stabilizer import OCRStabilizer  # noqa: E402


class Line:
    def __init__(self, text, confidence=0.9):
        self.text = text
        self.confidence = confidence


class SkipGate:
    """Deterministic scheduler double that always skips OCR."""

    def decide(self, *args, **kwargs):
        return SimpleNamespace(
            action="skip",
            reason="unchanged",
            changed=False,
            change_check_ms=0.0,
            confirmation_remaining=0,
            roi_geometry_changed=False,
            roi_geometry=None,
        )


def _args(**overrides):
    base = dict(app_id=None, debug=False, debug_roi_copy=None, json=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _kinds(events):
    return [event.kind for event in events]


def _stable(stale=2.0):
    stabilizer = OCRStabilizer(stale_timeout_sec=stale)
    stabilizer.observe([Line("A")], 1, 0.0)
    stabilizer.observe([Line("A")], 2, 0.5)
    return stabilizer


class StabilizerNoTextSemanticsTest(unittest.TestCase):
    def test_c1_stable_then_real_no_text_emits_one_clear(self) -> None:
        stabilizer = _stable()
        self.assertEqual(_kinds(stabilizer.observe([], 3, 3.0)), ["clear"])
        self.assertEqual(stabilizer.stats().clear_emits, 1)

    def test_c2_first_empty_does_not_clear_prematurely(self) -> None:
        stabilizer = _stable()
        self.assertEqual(stabilizer.observe([], 3, 1.0), [])

    def test_c3_repeated_no_text_does_not_spam(self) -> None:
        stabilizer = _stable()
        stabilizer.observe([], 3, 3.0)
        self.assertEqual(stabilizer.observe([], 4, 4.0), [])
        self.assertEqual(stabilizer.observe([], 5, 5.0), [])
        self.assertEqual(stabilizer.stats().clear_emits, 1)

    def test_c4_skipped_tick_preserves_text_while_present(self) -> None:
        stabilizer = _stable()
        for tick_time in (3.0, 5.0, 9.0):
            self.assertEqual(stabilizer.tick(tick_time), [])
        self.assertEqual(stabilizer.last_emitted_text, "A")

    def test_skipped_tick_after_real_no_text_may_clear(self) -> None:
        stabilizer = _stable()
        stabilizer.observe([], 3, 1.0)  # real no-text evidence, not yet stale
        self.assertEqual(_kinds(stabilizer.tick(3.0)), ["clear"])

    def test_c7_new_text_after_clear_emits(self) -> None:
        stabilizer = _stable()
        stabilizer.observe([], 3, 3.0)
        stabilizer.observe([Line("B")], 4, 4.0)
        self.assertEqual(_kinds(stabilizer.observe([Line("B")], 5, 5.0)), ["text"])

    def test_c8_identical_text_before_and_after_clear_emits_again(self) -> None:
        stabilizer = _stable()
        stabilizer.observe([], 3, 3.0)  # clear "A"
        events = stabilizer.observe([Line("A")], 4, 4.0)
        self.assertEqual(_kinds(events), ["text"])
        self.assertEqual(events[0].text, "A")


def _diagnostic(stabilizer, gate, machine_stream):
    diagnostic = ocr_test.OCRDiagnostic(
        _args(), runtime=None, stabilizer=stabilizer, gate=gate, machine_stream=machine_stream
    )
    diagnostic._wall_start = time.monotonic()
    diagnostic._debug_reset_after = None
    diagnostic._scheduler_reset = False
    diagnostic._debug_resolver = None
    diagnostic._roi_switched = False
    return diagnostic


def _frame():
    return ocr_test._MockCapture(_args()).capture_frame()


class PipelineClearEmissionTest(unittest.TestCase):
    def test_stabilize_clear_reaches_machine_stream(self) -> None:
        stabilizer = _stable()
        stream = io.StringIO()
        diagnostic = _diagnostic(stabilizer, gate=None, machine_stream=stream)
        # Real no-text observation whose timestamp is already past stale.
        result = SimpleNamespace(lines=(), sequence=3)
        diagnostic._stabilize(result)
        kinds = [json.loads(line)["kind"] for line in stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(kinds, ["clear"])

    def test_skipped_frame_tick_clear_reaches_machine_stream(self) -> None:
        # Real no-text observation sets the stale clock; the clear then fires on a
        # skipped (change-gated) frame and must still reach transport.
        stabilizer = OCRStabilizer(stale_timeout_sec=1.0)
        stabilizer.observe([Line("A")], 1, 0.0)
        stabilizer.observe([Line("A")], 2, 0.5)  # emits text, last_activity=0.5
        stabilizer.observe([], 3, 1.0)  # real no-text evidence; not stale yet
        self.assertEqual(stabilizer.stats().clear_emits, 0)

        stream = io.StringIO()
        diagnostic = _diagnostic(stabilizer, gate=SkipGate(), machine_stream=stream)

        asyncio.run(diagnostic._process_and_record(_frame(), 0.0))

        kinds = [json.loads(line)["kind"] for line in stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(kinds, ["clear"])
        self.assertEqual(stabilizer.stats().clear_emits, 1)

    def test_skipped_frame_with_text_present_never_clears(self) -> None:
        stabilizer = _stable()
        stream = io.StringIO()
        diagnostic = _diagnostic(stabilizer, gate=SkipGate(), machine_stream=stream)

        asyncio.run(diagnostic._process_and_record(_frame(), 0.0))

        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(stabilizer.stats().clear_emits, 0)
        self.assertEqual(stabilizer.last_emitted_text, "A")

    def test_detector_error_does_not_synthesize_clear(self) -> None:
        # A detector error fails open to OCR; with no no-text evidence the stable
        # text must be preserved.
        stabilizer = _stable()
        self.assertEqual(stabilizer.tick(time.monotonic()), [])
        self.assertEqual(stabilizer.stats().clear_emits, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
