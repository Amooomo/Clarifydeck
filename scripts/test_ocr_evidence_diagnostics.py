#!/usr/bin/env python3
"""Phase 2K.2.2 tests: opt-in OCR false-positive evidence diagnostics.

Diagnostics-only: no OCR thresholds, filtering, stabilizer, transport, or
overlay behavior is changed. Evidence goes to stderr; stdout JSONL is untouched.

Run:
    python3 scripts/test_ocr_evidence_diagnostics.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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
from ocr import OCRError  # noqa: E402
from ocr.stabilizer import OCRStabilizer  # noqa: E402


class Line:
    def __init__(self, text, confidence=0.9, box=None):
        self.text = text
        self.confidence = confidence
        self.box = box


class SkipGate:
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


class RaisingRuntime:
    def recognize_rgba(self, *args, **kwargs):
        raise OCRError("engine_run_failed", "boom")

    @property
    def last_timings(self):
        return {}


def _args(evidence=False, **overrides):
    base = dict(
        app_id=None,
        debug=False,
        debug_roi_copy=None,
        json=False,
        diagnostic_ocr_evidence=evidence,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _diagnostic(stabilizer, machine, evidence, gate=None, runtime=None):
    diagnostic = ocr_test.OCRDiagnostic(
        _args(evidence=evidence), runtime=runtime, stabilizer=stabilizer, gate=gate, machine_stream=machine
    )
    diagnostic._wall_start = time.monotonic()
    diagnostic._debug_reset_after = None
    diagnostic._scheduler_reset = False
    diagnostic._debug_resolver = None
    diagnostic._roi_switched = False
    return diagnostic


def _result(lines, sequence=1, width=64, height=32):
    return SimpleNamespace(lines=tuple(lines), sequence=sequence, roi_width=width, roi_height=height)


def _evidence_records(stderr_text):
    return [
        json.loads(line.split("[ocr-evidence] ", 1)[1])
        for line in stderr_text.splitlines()
        if line.startswith("[ocr-evidence] ")
    ]


def _schedule_records(stderr_text):
    return [
        json.loads(line.split("[ocr-schedule] ", 1)[1])
        for line in stderr_text.splitlines()
        if line.startswith("[ocr-schedule] ")
    ]


def _stable(stabilizer, machine, evidence, results):
    diagnostic = _diagnostic(stabilizer, machine, evidence)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        for result in results:
            diagnostic._stabilize(result)
    return err.getvalue()


class DiagnosticsDefaultTest(unittest.TestCase):
    def test_e1_diagnostics_off_by_default(self) -> None:
        stabilizer = OCRStabilizer()
        stderr = _stable(stabilizer, io.StringIO(), False, [_result([Line("A")])])
        self.assertNotIn("[ocr-evidence]", stderr)

    def test_e2_evidence_goes_to_stderr_not_stdout(self) -> None:
        stabilizer = OCRStabilizer()
        machine = io.StringIO()
        stderr = _stable(stabilizer, machine, True, [_result([Line("A")]), _result([Line("A")], sequence=2)])
        self.assertIn("[ocr-evidence]", stderr)
        # stdout machine stream contains only stable_text JSONL, never evidence.
        self.assertNotIn("[ocr-evidence]", machine.getvalue())
        for line in machine.getvalue().splitlines():
            payload = json.loads(line)
            self.assertEqual(payload["type"], "stable_text")


class EvidenceContentTest(unittest.TestCase):
    def test_e3_true_text_record(self) -> None:
        stabilizer = OCRStabilizer()
        box = ((1.0, 2.0), (30.0, 2.0), (30.0, 10.0), (1.0, 10.0))
        stderr = _stable(stabilizer, io.StringIO(), True, [_result([Line("字幕", 0.93, box)])])
        records = _evidence_records(stderr)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["real_ocr_attempt"])
        self.assertFalse(record["no_usable_text"])
        self.assertEqual(record["candidate_text"], "字幕")
        self.assertAlmostEqual(record["candidate_confidence"], 0.93)
        self.assertEqual(record["raw_line_count"], 1)
        self.assertEqual(record["usable_line_count"], 1)
        self.assertEqual(record["lines"][0]["text"], "字幕")
        self.assertEqual(record["lines"][0]["box"], [list(p) for p in box])

    def test_e4_real_no_text_record(self) -> None:
        stabilizer = OCRStabilizer()
        stderr = _stable(stabilizer, io.StringIO(), True, [_result([])])
        record = _evidence_records(stderr)[0]
        self.assertTrue(record["real_ocr_attempt"])
        self.assertTrue(record["no_usable_text"])
        self.assertIsNone(record["candidate_text"])
        self.assertEqual(record["usable_line_count"], 0)
        self.assertEqual(record["raw_line_count"], 0)

    def test_below_confidence_line_reported_but_not_usable(self) -> None:
        stabilizer = OCRStabilizer(min_line_confidence=0.70)
        stderr = _stable(stabilizer, io.StringIO(), True, [_result([Line("w", 0.20)])])
        record = _evidence_records(stderr)[0]
        self.assertEqual(record["raw_line_count"], 1)
        self.assertEqual(record["usable_line_count"], 0)
        self.assertTrue(record["no_usable_text"])
        self.assertEqual(record["lines"][0]["text"], "w")

    def test_e7_multiline_cjk_exact_preservation(self) -> None:
        stabilizer = OCRStabilizer()
        stderr = _stable(stabilizer, io.StringIO(), True, [_result([Line("第一行"), Line("第二行！")])])
        record = _evidence_records(stderr)[0]
        self.assertEqual(record["candidate_text"], "第一行\n第二行！")

    def test_bounded_output_truncates(self) -> None:
        stabilizer = OCRStabilizer()
        many = [Line(f"line{i}", 0.9) for i in range(ocr_test.MAX_EVIDENCE_LINES + 5)]
        stderr = _stable(stabilizer, io.StringIO(), True, [_result(many)])
        record = _evidence_records(stderr)[0]
        self.assertEqual(len(record["lines"]), ocr_test.MAX_EVIDENCE_LINES)
        self.assertTrue(record["lines_truncated"])


class SkipAndErrorTest(unittest.TestCase):
    def test_e5_scheduler_skip_is_not_no_text(self) -> None:
        stabilizer = OCRStabilizer()
        machine = io.StringIO()
        diagnostic = _diagnostic(stabilizer, machine, True, gate=SkipGate())
        frame = ocr_test._MockCapture(_args()).capture_frame()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            asyncio.run(diagnostic._process_and_record(frame, 0.0))
        stderr = err.getvalue()
        self.assertIn("[ocr-schedule]", stderr)
        schedule = _schedule_records(stderr)
        self.assertEqual(schedule[0]["real_ocr_attempt"], False)
        # No OCR evidence claiming no-text for the skipped frame.
        self.assertEqual(_evidence_records(stderr), [])

    def test_e6_ocr_exception_is_not_no_text(self) -> None:
        stabilizer = OCRStabilizer()
        machine = io.StringIO()
        diagnostic = _diagnostic(stabilizer, machine, True, gate=None, runtime=RaisingRuntime())
        frame = ocr_test._MockCapture(_args()).capture_frame()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(OCRError):
                asyncio.run(diagnostic._process_and_record(frame, 0.0))
        self.assertNotIn("[ocr-evidence]", err.getvalue())
        self.assertNotIn("[ocr-schedule]", err.getvalue())


class TransportUnchangedTest(unittest.TestCase):
    def _kinds(self, machine):
        return [json.loads(line)["kind"] for line in machine.getvalue().splitlines() if line.strip()]

    def test_e8_diagnostics_do_not_alter_transport_events(self) -> None:
        results = [_result([Line("A")], 1), _result([Line("A")], 2), _result([], 3)]

        machine_off = io.StringIO()
        _stable(OCRStabilizer(), machine_off, False, results)

        machine_on = io.StringIO()
        _stable(OCRStabilizer(), machine_on, True, results)

        self.assertEqual(self._kinds(machine_off), self._kinds(machine_on))
        self.assertEqual(self._kinds(machine_on), ["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
