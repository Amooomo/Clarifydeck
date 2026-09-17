#!/usr/bin/env python3
"""Phase 2N.5A tests: conservative high-confidence fast accept.

Covers the replacement-only fast-accept predicate, the post-fast-accept
anti-flapping lock, per-region isolation/reset, and the bounded diagnostics.
The existing consensus path must remain the fallback everywhere else.

Run:
    python3 scripts/test_ocr_fast_accept.py
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
    FAST_ACCEPT_MIN_CONFIDENCE,
    MAX_FAST_ACCEPT_RECORDS,
    OCRStabilizer,
)


def _lines(text, confidence=0.99):
    return [SimpleNamespace(text=text, confidence=confidence)]


def _stable(text="OLD", confidence=0.99):
    """Publish an initial Stable Text through the unchanged consensus path."""
    stabilizer = OCRStabilizer()
    stabilizer.observe(_lines(text, confidence), 1, 0.0)
    stabilizer.observe(_lines(text, confidence), 2, 1.0)
    return stabilizer


def _texts(events):
    return [event.text for event in events if event.kind == "text"]


class FastAcceptPredicateTest(unittest.TestCase):
    def test_threshold_constant(self) -> None:
        self.assertEqual(FAST_ACCEPT_MIN_CONFIDENCE, 0.95)

    def test_exactly_threshold_fast_accepts(self) -> None:
        stabilizer = _stable()
        events = stabilizer.observe(_lines("NEW", 0.95), 3, 2.0)
        self.assertEqual(_texts(events), ["NEW"])
        self.assertEqual(stabilizer.stats().stable_text_emits, 2)
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 1)

    def test_above_threshold_fast_accepts(self) -> None:
        stabilizer = _stable()
        events = stabilizer.observe(_lines("NEW", 0.997), 3, 2.0)
        self.assertEqual(_texts(events), ["NEW"])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 1)

    def test_below_threshold_uses_consensus(self) -> None:
        stabilizer = _stable()
        self.assertEqual(stabilizer.observe(_lines("NEW", 0.9499), 3, 2.0), [])
        events = stabilizer.observe(_lines("NEW", 0.9499), 4, 3.0)
        self.assertEqual(_texts(events), ["NEW"])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 0)
        self.assertEqual(stabilizer.fast_accept_summary()["fallback_accept_total"], 2)

    def test_initial_publication_requires_consensus(self) -> None:
        stabilizer = OCRStabilizer()
        self.assertEqual(stabilizer.observe(_lines("FIRST", 0.99), 1, 0.0), [])
        events = stabilizer.observe(_lines("FIRST", 0.99), 2, 1.0)
        self.assertEqual(_texts(events), ["FIRST"])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 0)

    def test_candidate_equal_stable_no_transition(self) -> None:
        stabilizer = _stable()
        self.assertEqual(stabilizer.observe(_lines("OLD", 0.99), 3, 2.0), [])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 0)

    def test_empty_candidate_no_fast_accept(self) -> None:
        stabilizer = _stable()
        self.assertEqual(stabilizer.observe([], 3, 2.0), [])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 0)

    def test_clear_behavior_unchanged(self) -> None:
        stabilizer = _stable()
        events = stabilizer.observe([], 3, 5.0)  # beyond stale timeout (2.0s)
        self.assertEqual([event.kind for event in events], ["clear"])
        self.assertIsNone(stabilizer.last_emitted_text)
        self.assertEqual(stabilizer.stats().clear_emits, 1)
        self.assertFalse(stabilizer.fast_accept_locked)

    def test_exact_string_semantics(self) -> None:
        # Normalization is the existing NFC + inner-space collapse + strip; a
        # candidate equal to the Stable Text after normalization is not a change.
        stabilizer = _stable("a b")
        self.assertEqual(stabilizer.observe(_lines("a   b", 0.99), 3, 2.0), [])


class AntiFlapTest(unittest.TestCase):
    def test_fast_accept_once_then_diff_not_fast(self) -> None:
        stabilizer = _stable()
        self.assertEqual(_texts(stabilizer.observe(_lines("A", 0.99), 3, 2.0)), ["A"])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 1)
        # B differs from the fast-accepted A: must NOT be fast accepted.
        self.assertEqual(stabilizer.observe(_lines("B", 0.99), 4, 3.0), [])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 1)
        # B again: the ordinary consensus path may accept it.
        self.assertEqual(_texts(stabilizer.observe(_lines("B", 0.99), 5, 4.0)), ["B"])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 1)
        self.assertEqual(stabilizer.fast_accept_summary()["fallback_accept_total"], 2)

    def test_confirmation_no_duplicate_and_unlocks(self) -> None:
        stabilizer = _stable()
        self.assertEqual(_texts(stabilizer.observe(_lines("A", 0.99), 3, 2.0)), ["A"])
        # Immediate next candidate matches the fast-accepted text: confirms.
        events = stabilizer.observe(_lines("A", 0.98), 4, 3.0)
        self.assertEqual(events, [])
        self.assertEqual(stabilizer.stats().stable_text_emits, 2)  # OLD + A only
        summary = stabilizer.fast_accept_summary()
        self.assertEqual(summary["fast_accept_next_match"], 1)
        self.assertEqual(summary["fast_accept_next_diff"], 0)
        # Fast path is eligible again for a genuine future replacement.
        self.assertEqual(_texts(stabilizer.observe(_lines("C", 0.99), 5, 4.0)), ["C"])
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 2)

    def test_diff_then_consensus_releases_lock(self) -> None:
        stabilizer = _stable()
        stabilizer.observe(_lines("A", 0.99), 3, 2.0)  # fast accept A
        stabilizer.observe(_lines("B", 0.99), 4, 3.0)  # diff, locked
        self.assertTrue(stabilizer.fast_accept_locked)
        stabilizer.observe(_lines("B", 0.99), 5, 4.0)  # consensus accepts B
        self.assertFalse(stabilizer.fast_accept_locked)

    def test_alternating_high_confidence_not_per_frame(self) -> None:
        texts = ["A", "B", "A", "B", "A", "B", "A", "B"]
        fast = _stable()
        events = []
        for index, text in enumerate(texts, start=3):
            events += _texts(fast.observe(_lines(text, 0.99), index, float(index)))
        # Not one Stable Text event per frame.
        self.assertLess(len(events), len(texts))

        # The fast path must not emit more events than the unchanged consensus
        # path would on the same alternating input (0.94 is below the fast threshold).
        baseline = _stable(confidence=0.94)
        baseline_events = []
        for index, text in enumerate(texts, start=3):
            baseline_events += _texts(baseline.observe(_lines(text, 0.94), index, float(index)))
        self.assertLessEqual(len(events), len(baseline_events))


class LifecycleAndIsolationTest(unittest.TestCase):
    def test_reset_clears_fast_lock(self) -> None:
        stabilizer = _stable()
        stabilizer.observe(_lines("A", 0.99), 3, 2.0)  # fast accept, lock
        self.assertTrue(stabilizer.fast_accept_locked)
        stabilizer.reset()
        self.assertFalse(stabilizer.fast_accept_locked)
        self.assertEqual(stabilizer.fast_accept_summary()["fast_accept_total"], 0)
        stabilizer.observe(_lines("A", 0.99), 1, 10.0)
        self.assertEqual(_texts(stabilizer.observe(_lines("A", 0.99), 2, 11.0)), ["A"])

    def test_clear_clears_fast_lock(self) -> None:
        stabilizer = _stable()
        stabilizer.observe(_lines("A", 0.99), 3, 2.0)  # fast accept, lock
        stabilizer.observe([], 4, 10.0)  # clear
        self.assertFalse(stabilizer.fast_accept_locked)


class _ScriptedRuntime:
    def __init__(self, script):
        self._script = list(script)

    def recognize_rgba(self, crop, width, height, sequence=None):
        text, confidence = self._script.pop(0)
        return SimpleNamespace(
            lines=_lines(text, confidence) if text else [],
            sequence=sequence,
            elapsed_ms=10.0,
        )


def _rgba(width=20, height=20) -> bytes:
    return bytes(width * height * 4)


def _regions():
    return [
        RecognitionRegion(region_id="A", x=0.1, y=0.1, w=0.2, h=0.2),
        RecognitionRegion(region_id="B", x=0.5, y=0.5, w=0.2, h=0.2),
    ]


class MultiRegionFastAcceptTest(unittest.TestCase):
    def _coordinator(self, script):
        return MultiRegionOCRCoordinator(_ScriptedRuntime(script))

    def test_regions_isolated_and_no_cross_unlock(self) -> None:
        script = [
            ("OLD", 0.99), ("OLD", 0.99),  # frame 1
            ("OLD", 0.99), ("OLD", 0.99),  # frame 2 -> initial publication
            ("A1", 0.99), ("B1", 0.99),    # frame 3 -> both fast accept
            ("A1", 0.99), ("BX", 0.99),    # frame 4 -> A confirms, B differs/locked
            ("A2", 0.99), ("BY", 0.99),    # frame 5 -> A fast accepts, B still locked
        ]
        coordinator = self._coordinator(script)
        regions = _regions()
        for sequence in range(1, 6):
            events = coordinator.process_decoded(_rgba(), 20, 20, sequence, regions)
            by_region = {event.region_id: event.event for event in events}
            if sequence == 3:
                self.assertEqual(by_region["A"].text, "A1")
                self.assertEqual(by_region["B"].text, "B1")
            elif sequence == 4:
                self.assertEqual(events, [])
            elif sequence == 5:
                self.assertEqual(list(by_region), ["A"])
                self.assertEqual(by_region["A"].text, "A2")

        summary = coordinator.fast_accept_summary()
        self.assertEqual(summary["fast_accept_total"], 3)
        self.assertEqual(summary["fast_accept_next_match"], 1)
        self.assertEqual(summary["fast_accept_next_diff"], 1)
        self.assertEqual(summary["regions"], 2)

    def test_region_removal_isolates_state(self) -> None:
        script = [
            ("OLD", 0.99), ("OLD", 0.99),
            ("OLD", 0.99), ("OLD", 0.99),
            ("A1", 0.99), ("B1", 0.99),  # both fast accept
            ("A1", 0.99),                # frame 4: only region A remains
        ]
        coordinator = self._coordinator(script)
        regions = _regions()
        for sequence in range(1, 4):
            coordinator.process_decoded(_rgba(), 20, 20, sequence, regions)
        self.assertEqual(coordinator.state_ids(), ("A", "B"))
        # Drop B; A's fast-accept state must be unaffected.
        coordinator.process_decoded(_rgba(), 20, 20, 4, regions[:1])
        self.assertEqual(coordinator.state_ids(), ("A",))
        self.assertEqual(coordinator.fast_accept_summary()["fast_accept_total"], 1)


class DiagnosticsTest(unittest.TestCase):
    def test_emit_and_confirm_records(self) -> None:
        stabilizer = _stable()
        stabilizer.observe(_lines("A", 0.99), 3, 2.0)
        record = stabilizer.take_fast_accept_record()
        self.assertIsNotNone(record)
        self.assertEqual(record["frame_seq"], 3)
        self.assertAlmostEqual(record["confidence"], 0.99)
        self.assertEqual(record["previous_stable"], 1)
        self.assertEqual(record["threshold"], FAST_ACCEPT_MIN_CONFIDENCE)
        self.assertIsNone(stabilizer.take_fast_accept_record())

        stabilizer.observe(_lines("A", 0.99), 4, 3.0)
        confirm = stabilizer.take_fast_accept_confirm()
        self.assertIsNotNone(confirm)
        self.assertTrue(confirm["match"])
        self.assertIsNone(stabilizer.take_fast_accept_confirm())

    def test_no_raw_text_leakage(self) -> None:
        stabilizer = _stable("SECRET-OLD")
        stabilizer.observe(_lines("SECRET-NEW", 0.99), 3, 2.0)
        stabilizer.observe(_lines("SECRET-NEW", 0.99), 4, 3.0)
        blob = (
            repr(stabilizer.take_fast_accept_record())
            + repr(stabilizer.take_fast_accept_confirm())
            + repr(stabilizer.fast_accept_recent())
            + repr(stabilizer.fast_accept_summary())
            + repr(stabilizer.audit_recent())
        )
        self.assertNotIn("SECRET", blob)

    def test_recent_records_bounded(self) -> None:
        stabilizer = _stable()
        for index in range(MAX_FAST_ACCEPT_RECORDS + 10):
            text = f"T{index}"
            stabilizer.observe(_lines(text, 0.99), index * 2 + 3, float(index * 2 + 3))
            stabilizer.observe(_lines(text, 0.99), index * 2 + 4, float(index * 2 + 4))
        self.assertLessEqual(len(stabilizer.fast_accept_recent()), MAX_FAST_ACCEPT_RECORDS)
        self.assertEqual(
            stabilizer.fast_accept_summary()["fast_accept_total"], MAX_FAST_ACCEPT_RECORDS + 10
        )

    def test_audit_tags_fast_accept_transitions(self) -> None:
        stabilizer = _stable()
        stabilizer.observe(_lines("A", 0.99), 3, 2.0)  # fast accept
        record = stabilizer.take_audit_record()
        self.assertTrue(record["fast_accept"])
        self.assertEqual(stabilizer.audit_summary()["fast_accept_transitions"], 1)


class WorkerJournalMirrorTest(unittest.TestCase):
    def test_fast_accept_lines_are_mirrored(self) -> None:
        from backend.ocr_worker import OCRWorkerManager

        class _Stream:
            def __init__(self, lines):
                self._lines = list(lines)

            def readline(self, limit=-1):
                return self._lines.pop(0) if self._lines else b""

            def close(self):
                pass

        captured: list[str] = []
        manager = OCRWorkerManager(logger=captured.append)
        manager._proc = SimpleNamespace(
            stderr=_Stream(
                [
                    b"[fast-accept] region=A frame=3 conf=0.997 previous_stable=1 threshold=0.95\n",
                    b"[fast-accept-confirm] region=A match=1 next_conf=0.98 elapsed_ms=998.0\n",
                    b"unrelated line\n",
                ]
            )
        )
        manager._read_stderr()
        self.assertTrue(any("[fast-accept]" in line for line in captured))
        self.assertTrue(any("[fast-accept-confirm]" in line for line in captured))
        self.assertFalse(any("unrelated line" in line for line in captured))


if __name__ == "__main__":
    unittest.main(verbosity=2)
