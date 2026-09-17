#!/usr/bin/env python3
"""Phase 2N.4 tests: stabilizer first-candidate reliability audit (observational).

Verifies the audit records agree with the unchanged consensus behavior, that the
audit never changes stabilization, and that state is bounded and isolated.

Run:
    python3 scripts/test_stabilizer_reliability_audit.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture.recognition_regions import RecognitionRegion  # noqa: E402
from ocr.multi_region import MultiRegionOCRCoordinator  # noqa: E402
from ocr.stabilizer import (  # noqa: E402
    MAX_AUDIT_RECORDS,
    OCRStabilizer,
    audit_confidence_bucket,
)


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.t = float(value)

    def __call__(self) -> float:
        return self.t


def _lines(text: str, confidence: float = 0.95):
    return [SimpleNamespace(text=text, confidence=confidence)]


class StabilizerAuditTest(unittest.TestCase):
    def _stabilizer(self, clock):
        return OCRStabilizer(clock=clock)

    def test_first_matches_final(self) -> None:
        clock = _Clock(1.0)
        stabilizer = self._stabilizer(clock)
        stabilizer.observe(_lines("ABC"), 1, 1.0)
        clock.t = 2.0
        stabilizer.observe(_lines("ABC"), 2, 2.0)
        record = stabilizer.take_audit_record()
        self.assertIsNotNone(record)
        self.assertTrue(record["first_matches_final"])
        self.assertEqual(record["candidate_count_until_accept"], 2)
        self.assertEqual(record["intermediate_distinct_candidate_count"], 1)
        self.assertEqual(record["first_candidate_to_accept_ms"], 1000.0)
        self.assertEqual(record["theoretical_fast_accept_saving_ms"], 1000.0)
        self.assertIsNone(stabilizer.take_audit_record())

    def test_first_differs_from_final(self) -> None:
        clock = _Clock(1.0)
        stabilizer = self._stabilizer(clock)
        stabilizer.observe(_lines("X"), 1, 1.0)
        clock.t = 2.0
        stabilizer.observe(_lines("A"), 2, 2.0)
        clock.t = 3.0
        stabilizer.observe(_lines("A"), 3, 3.0)
        record = stabilizer.take_audit_record()
        self.assertFalse(record["first_matches_final"])
        self.assertEqual(record["candidate_count_until_accept"], 3)
        self.assertEqual(record["intermediate_distinct_candidate_count"], 2)
        self.assertEqual(record["theoretical_fast_accept_saving_ms"], 0.0)
        self.assertGreaterEqual(record["first_candidate_to_accept_ms"], 0.0)

    def test_repeated_stable_text_no_new_trial(self) -> None:
        clock = _Clock(1.0)
        stabilizer = self._stabilizer(clock)
        stabilizer.observe(_lines("A"), 1, 1.0)
        clock.t = 2.0
        stabilizer.observe(_lines("A"), 2, 2.0)
        stabilizer.take_audit_record()
        clock.t = 3.0
        stabilizer.observe(_lines("A"), 3, 3.0)
        self.assertIsNone(stabilizer.take_audit_record())
        self.assertEqual(stabilizer.audit_summary()["transitions_total"], 1)

    def test_confidence_is_min_line_confidence(self) -> None:
        clock = _Clock(1.0)
        stabilizer = self._stabilizer(clock)
        lines = [SimpleNamespace(text="A", confidence=0.9), SimpleNamespace(text="B", confidence=0.82)]
        stabilizer.observe(lines, 1, 1.0)
        clock.t = 2.0
        stabilizer.observe(lines, 2, 2.0)
        record = stabilizer.take_audit_record()
        self.assertAlmostEqual(record["first_candidate_confidence"], 0.82)

    def test_clear_counted_separately(self) -> None:
        clock = _Clock(1.0)
        stabilizer = self._stabilizer(clock)
        stabilizer.observe(_lines("A"), 1, 1.0)
        clock.t = 2.0
        stabilizer.observe(_lines("A"), 2, 2.0)
        stabilizer.take_audit_record()
        clock.t = 5.0  # beyond stale timeout (2.0s)
        events = stabilizer.observe([], 3, 5.0)
        self.assertTrue(any(event.kind == "clear" for event in events))
        self.assertIsNone(stabilizer.take_audit_record())
        summary = stabilizer.audit_summary()
        self.assertEqual(summary["clear_transitions"], 1)
        self.assertEqual(summary["transitions_total"], 1)

    def test_reset_clears_trial_and_stats(self) -> None:
        clock = _Clock(1.0)
        stabilizer = self._stabilizer(clock)
        stabilizer.observe(_lines("X"), 1, 1.0)  # open trial
        stabilizer.reset()
        self.assertIsNone(stabilizer.take_audit_record())
        self.assertEqual(stabilizer.audit_summary()["transitions_total"], 0)
        clock.t = 2.0
        stabilizer.observe(_lines("A"), 1, 2.0)
        clock.t = 3.0
        stabilizer.observe(_lines("A"), 2, 3.0)
        record = stabilizer.take_audit_record()
        self.assertTrue(record["first_matches_final"])

    def test_no_text_content_in_record(self) -> None:
        clock = _Clock(1.0)
        stabilizer = self._stabilizer(clock)
        secret = "SECRET-DIALOG-TEXT"
        stabilizer.observe(_lines(secret), 1, 1.0)
        clock.t = 2.0
        stabilizer.observe(_lines(secret), 2, 2.0)
        record = stabilizer.take_audit_record()
        self.assertNotIn(secret, repr(record))
        self.assertEqual(record["first_candidate_length"], len(secret))

    def test_recent_records_bounded(self) -> None:
        clock = _Clock(0.0)
        stabilizer = self._stabilizer(clock)
        for index in range(MAX_AUDIT_RECORDS + 10):
            text = f"T{index}"
            stabilizer.observe(_lines(text), index * 2 + 1, clock.t)
            clock.t += 1.0
            stabilizer.observe(_lines(text), index * 2 + 2, clock.t)
            clock.t += 1.0
        self.assertLessEqual(len(stabilizer.audit_recent()), MAX_AUDIT_RECORDS)
        self.assertEqual(stabilizer.audit_summary()["transitions_total"], MAX_AUDIT_RECORDS + 10)

    def test_confidence_bucket_helper(self) -> None:
        self.assertEqual(audit_confidence_bucket(0.75), "0.70-0.79")
        self.assertEqual(audit_confidence_bucket(0.85), "0.80-0.89")
        self.assertEqual(audit_confidence_bucket(0.92), "0.90-0.94")
        self.assertEqual(audit_confidence_bucket(0.99), "0.95-1.00")


class _ScriptedRuntime:
    def __init__(self, script):
        self._script = list(script)

    def recognize_rgba(self, crop, width, height, sequence=None):
        text = self._script.pop(0)
        return SimpleNamespace(lines=_lines(text), sequence=sequence, elapsed_ms=10.0)


def _rgba(width=20, height=20) -> bytes:
    return bytes(width * height * 4)


class MultiRegionAuditIsolationTest(unittest.TestCase):
    def test_two_regions_isolated(self) -> None:
        clock = _Clock(1000.0)
        runtime = _ScriptedRuntime(["AAA", "BBB", "AAA", "BBB"])
        coordinator = MultiRegionOCRCoordinator(runtime, clock=clock)
        regions = [
            RecognitionRegion(region_id="A", x=0.1, y=0.1, w=0.2, h=0.2),
            RecognitionRegion(region_id="B", x=0.5, y=0.5, w=0.2, h=0.2),
        ]
        coordinator.process_decoded(_rgba(), 20, 20, 1, regions)
        clock.t = 1001.0
        coordinator.process_decoded(_rgba(), 20, 20, 2, regions)
        records = coordinator.drain_audit()
        self.assertEqual({record["region_id"] for record in records}, {"A", "B"})
        summary = coordinator.audit_summary()
        self.assertEqual(summary["transitions_total"], 2)
        self.assertEqual(summary["first_matches_final"], 2)
        self.assertEqual(summary["regions"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
