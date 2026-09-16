#!/usr/bin/env python3
"""Phase 2L.5 tests: per-region change-gated scheduling.

Deterministic, no real RapidOCR runtime. Covers independent per-region change
decisions, independent forced refresh, detector fail-open isolation, skip/tick
clear forwarding, and region state lifecycle.

Run:
    python3 scripts/test_multi_region_scheduler.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ocr.multi_region as mr  # noqa: E402
from capture.recognition_regions import RecognitionRegion  # noqa: E402
from capture.scheduler import OCRChangeGate  # noqa: E402
from ocr.result import OCRFrameResult, OCRLine  # noqa: E402
from ocr.stabilizer import OCRStabilizer  # noqa: E402


def line(text, confidence=0.9):
    return OCRLine(text=text, confidence=confidence, box=None)


def region(rid, x=0.1, y=0.1, w=0.2, h=0.2, enabled=True):
    return RecognitionRegion(region_id=rid, x=x, y=y, w=w, h=h, enabled=enabled)


def _rgba(width=100, height=100):
    return bytes(width * height * 4)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeDetector:
    def __init__(self, changes):
        self._changes = list(changes)
        self._index = 0

    def classify_rgba(self, **kwargs):
        if not self._changes:
            changed = False
        else:
            changed = self._changes[min(self._index, len(self._changes) - 1)]
        self._index += 1
        return SimpleNamespace(changed=changed, score=0.0, reason="x", age_ms=0.0)

    def reset_state(self):
        self._index = 0


class BrokenDetector:
    def classify_rgba(self, **kwargs):
        raise RuntimeError("detector boom")

    def reset_state(self):
        pass


class FakeRuntime:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def recognize_rgba(self, rgba, width, height, sequence=None):
        self.calls.append(sequence)
        lines = self.script.pop(0) if self.script else []
        return OCRFrameResult(
            sequence=sequence, lines=tuple(lines), elapsed_ms=0.0, backend="fake", roi_width=width, roi_height=height
        )


def gate(detector, clock, force=3.0):
    return OCRChangeGate(detector, force_interval_sec=force, clock=clock)


def coordinator(runtime, gates, clock, **stab_kwargs):
    queue = list(gates)

    def gate_factory():
        return queue.pop(0)

    return mr.MultiRegionOCRCoordinator(
        runtime,
        stabilizer_factory=lambda: OCRStabilizer(**stab_kwargs),
        gate_factory=gate_factory,
        clock=clock,
    )


def feed(coord, regions, clock, sequence):
    return coord.process_decoded(_rgba(), 100, 100, sequence, regions, captured_monotonic=clock())


class IndependentGatingTest(unittest.TestCase):
    def test_g1_a_unchanged_b_changed(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("B1")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, False]), clock, 1000), gate(FakeDetector([True, True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A"), region("B", x=0.5)]
        feed(coord, regions, clock, 1)  # warm-up: both OCR
        before = len(runtime.calls)
        events = feed(coord, regions, clock, 2)
        self.assertEqual(len(runtime.calls) - before, 1)
        self.assertEqual([e.region_id for e in events], ["B"])

    def test_g2_a_changed_b_unchanged(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("A1")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, True]), clock, 1000), gate(FakeDetector([True, False]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A"), region("B", x=0.5)]
        feed(coord, regions, clock, 1)
        before = len(runtime.calls)
        events = feed(coord, regions, clock, 2)
        self.assertEqual(len(runtime.calls) - before, 1)
        self.assertEqual([e.region_id for e in events], ["A"])

    def test_g3_both_unchanged_no_ocr(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, False]), clock, 1000), gate(FakeDetector([True, False]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A"), region("B", x=0.5)]
        feed(coord, regions, clock, 1)
        before = len(runtime.calls)
        events = feed(coord, regions, clock, 2)
        self.assertEqual(len(runtime.calls), before)
        self.assertEqual(events, [])

    def test_g4_both_changed_two_ocr(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("A1")], [line("B1")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, True]), clock, 1000), gate(FakeDetector([True, True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A"), region("B", x=0.5)]
        feed(coord, regions, clock, 1)
        before = len(runtime.calls)
        feed(coord, regions, clock, 2)
        self.assertEqual(len(runtime.calls) - before, 2)

    def test_g5_disabled_region_not_gated(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("A1")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A"), region("B", enabled=False)]
        feed(coord, regions, clock, 1)
        before = len(runtime.calls)
        feed(coord, regions, clock, 2)
        self.assertEqual(len(runtime.calls) - before, 1)
        self.assertEqual(coord.state_ids(), ("A",))


class ForcedRefreshTest(unittest.TestCase):
    def _coord(self):
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("A1")], [line("B1")]])
        coord = coordinator(
            runtime,
            [gate(FakeDetector([True, False, False]), clock, 3.0), gate(FakeDetector([True, False, False]), clock, 5.0)],
            clock,
            consensus_required=1,
            history_size=1,
        )
        regions = [region("A"), region("B", x=0.5)]
        feed(coord, regions, clock, 1)  # t=0 warm-up, last_ocr=0 both
        return clock, runtime, coord, regions

    def test_f1_independent_timers_a_forces_b_not(self) -> None:
        clock, runtime, coord, regions = self._coord()
        clock.value = 3.5
        before = len(runtime.calls)
        feed(coord, regions, clock, 2)
        self.assertEqual(len(runtime.calls) - before, 1)  # A only
        self.assertGreaterEqual(coord.stats.regions_forced, 1)

    def test_f2_b_reaches_own_deadline(self) -> None:
        clock, runtime, coord, regions = self._coord()
        clock.value = 3.5
        feed(coord, regions, clock, 2)  # A forced
        clock.value = 5.5
        before = len(runtime.calls)
        feed(coord, regions, clock, 3)  # B forced, A skips (2s < 3s)
        self.assertEqual(len(runtime.calls) - before, 1)

    def test_f3_force_resets_only_processed_region(self) -> None:
        clock, runtime, coord, regions = self._coord()
        clock.value = 3.5
        feed(coord, regions, clock, 2)  # A forced (last_ocr 3.5), B skip (last_ocr 0)
        clock.value = 5.5
        feed(coord, regions, clock, 3)  # A skip, B forced
        self.assertEqual(coord.stats.regions_forced, 2)


class DetectorErrorTest(unittest.TestCase):
    def test_d1_d2_detector_error_fail_open_per_region(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("A1")]])
        coord = coordinator(runtime, [gate(BrokenDetector(), clock, 1000), gate(FakeDetector([True, False]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A"), region("B", x=0.5)]
        feed(coord, regions, clock, 1)
        before = len(runtime.calls)
        feed(coord, regions, clock, 2)
        self.assertEqual(len(runtime.calls) - before, 1)  # A fail-open, B skip
        self.assertGreaterEqual(coord.stats.detector_errors, 1)

    def test_d3_detector_error_is_not_no_text(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("Hello")], [line("Hello")]])
        coord = coordinator(runtime, [gate(BrokenDetector(), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A")]
        first = feed(coord, regions, clock, 1)
        second = feed(coord, regions, clock, 2)
        self.assertEqual([e.event.kind for e in first], ["text"])
        self.assertEqual(second, [])  # duplicate suppressed; no clear from detector failure

    def test_d4_repeated_failure_does_not_disable_other_gate(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("A1")], [line("A2")]])
        coord = coordinator(runtime, [gate(BrokenDetector(), clock, 1000), gate(FakeDetector([True, False, False]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A"), region("B", x=0.5)]
        feed(coord, regions, clock, 1)  # 2 calls
        feed(coord, regions, clock, 2)  # A only
        feed(coord, regions, clock, 3)  # A only
        self.assertEqual(len(runtime.calls), 4)  # B never OCRs again
        self.assertGreaterEqual(coord.stats.regions_skipped, 2)


class TickClearTest(unittest.TestCase):
    def test_c1_prior_no_text_then_skips_emits_one_clear(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("Hello")], []])
        coord = coordinator(runtime, [gate(FakeDetector([True, True, False, False]), clock, 1000)], clock, consensus_required=1, history_size=1, stale_timeout_sec=2.0)
        regions = [region("A")]
        self.assertEqual([e.event.kind for e in feed(coord, regions, clock, 1)], ["text"])
        clock.value = 0.5
        self.assertEqual(feed(coord, regions, clock, 2), [])  # real no-text, not stale yet
        clock.value = 1.0
        self.assertEqual(feed(coord, regions, clock, 3), [])  # skip tick
        clock.value = 3.0
        events = feed(coord, regions, clock, 4)  # skip tick -> clear
        self.assertEqual([e.event.kind for e in events], ["clear"])
        self.assertIsNone(events[0].event.source_seq)

    def test_c2_unchanged_skip_with_text_does_not_clear(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("Hello")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, False]), clock, 1000)], clock, consensus_required=1, history_size=1, stale_timeout_sec=2.0)
        regions = [region("A")]
        feed(coord, regions, clock, 1)
        clock.value = 100.0
        events = feed(coord, regions, clock, 2)  # skip tick keep-alive
        self.assertEqual(events, [])

    def test_c3_clear_exactly_once(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("Hello")], []])
        coord = coordinator(runtime, [gate(FakeDetector([True, True, False, False, False]), clock, 1000)], clock, consensus_required=1, history_size=1, stale_timeout_sec=2.0)
        regions = [region("A")]
        feed(coord, regions, clock, 1)
        clock.value = 0.5
        feed(coord, regions, clock, 2)
        clock.value = 3.0
        self.assertEqual([e.event.kind for e in feed(coord, regions, clock, 3)], ["clear"])
        clock.value = 10.0
        self.assertEqual(feed(coord, regions, clock, 4), [])

    def test_c6_new_text_after_clear(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("Hello")], [], [line("Hello")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, True, False, True]), clock, 1000)], clock, consensus_required=1, history_size=1, stale_timeout_sec=2.0)
        regions = [region("A")]
        feed(coord, regions, clock, 1)
        clock.value = 0.5
        feed(coord, regions, clock, 2)
        clock.value = 3.0
        self.assertEqual([e.event.kind for e in feed(coord, regions, clock, 3)], ["clear"])
        clock.value = 4.0
        events = feed(coord, regions, clock, 4)
        self.assertEqual([e.event.kind for e in events], ["text"])
        self.assertEqual(events[0].event.text, "Hello")


class SchedulerLifecycleTest(unittest.TestCase):
    def _coord(self, gates):
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")]])
        return clock, runtime, coordinator(runtime, gates, clock, consensus_required=1, history_size=1)

    def test_l1_geometry_change_resets_only_target(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("A1")]])
        coord = coordinator(runtime, [gate(FakeDetector([True]), clock, 1000), gate(FakeDetector([True]), clock, 1000), gate(FakeDetector([True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        feed(coord, [region("A"), region("B", x=0.5)], clock, 1)
        a_gate = coord._states["A"].gate
        b_gate = coord._states["B"].gate
        feed(coord, [region("A", x=0.6), region("B", x=0.5)], clock, 2)
        self.assertIsNot(coord._states["A"].gate, a_gate)
        self.assertIs(coord._states["B"].gate, b_gate)
        self.assertEqual(coord.stats.states_reset, 1)

    def test_l2_disable_discards_gate_state(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")]])
        coord = coordinator(runtime, [gate(FakeDetector([True]), clock, 1000), gate(FakeDetector([True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        feed(coord, [region("A"), region("B", x=0.5)], clock, 1)
        feed(coord, [region("A"), region("B", x=0.5, enabled=False)], clock, 2)
        self.assertEqual(coord.state_ids(), ("A",))

    def test_l3_re_enable_starts_fresh(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("B1")]])
        coord = coordinator(runtime, [gate(FakeDetector([True]), clock, 1000), gate(FakeDetector([True]), clock, 1000), gate(FakeDetector([True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        feed(coord, [region("A"), region("B", x=0.5)], clock, 1)
        first_b_gate = coord._states["B"].gate
        feed(coord, [region("A"), region("B", x=0.5, enabled=False)], clock, 2)
        feed(coord, [region("A"), region("B", x=0.5)], clock, 3)
        self.assertIsNot(coord._states["B"].gate, first_b_gate)

    def test_l4_remove_discards_gate_state(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")]])
        coord = coordinator(runtime, [gate(FakeDetector([True]), clock, 1000), gate(FakeDetector([True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        feed(coord, [region("A"), region("B", x=0.5)], clock, 1)
        feed(coord, [region("A")], clock, 2)
        self.assertEqual(coord.state_ids(), ("A",))

    def test_l5_reorder_preserves_state(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("B1")], [line("A1")]])
        coord = coordinator(runtime, [gate(FakeDetector([True]), clock, 1000), gate(FakeDetector([True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        feed(coord, [region("A"), region("B", x=0.5)], clock, 1)
        a_gate = coord._states["A"].gate
        b_gate = coord._states["B"].gate
        feed(coord, [region("B", x=0.5), region("A")], clock, 2)
        self.assertIs(coord._states["A"].gate, a_gate)
        self.assertIs(coord._states["B"].gate, b_gate)
        self.assertEqual(coord.stats.states_reset, 0)


class SourceSequenceTest(unittest.TestCase):
    def test_s1_skipped_regions_emit_no_event(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")]])
        coord = coordinator(runtime, [gate(FakeDetector([True, False]), clock, 1000)], clock, consensus_required=1, history_size=1)
        regions = [region("A")]
        feed(coord, regions, clock, 1)
        events = feed(coord, regions, clock, 2)
        self.assertEqual(events, [])

    def test_s2_ocr_results_retain_source_sequence(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")]])
        coord = coordinator(runtime, [gate(FakeDetector([True]), clock, 1000)], clock, consensus_required=1, history_size=1)
        events = feed(coord, [region("A")], clock, 42)
        self.assertEqual(events[0].event.source_seq, 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
