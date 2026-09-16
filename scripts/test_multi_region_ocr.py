#!/usr/bin/env python3
"""Phase 2L.2 tests: multi-region OCR execution foundation.

Deterministic, pure-stdlib tests. No real RapidOCR runtime, no production wire
format, no renderer.

Run:
    python3 scripts/test_multi_region_ocr.py
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import ocr_test  # noqa: E402
import ocr.multi_region as mr  # noqa: E402
from capture.recognition_regions import RecognitionRegion  # noqa: E402
from ocr.result import OCRFrameResult, OCRLine  # noqa: E402
from ocr.stabilizer import OCRStabilizer  # noqa: E402


def line(text, confidence=0.9):
    return OCRLine(text=text, confidence=confidence, box=None)


def region(rid, x=0.1, y=0.1, w=0.2, h=0.2, enabled=True):
    return RecognitionRegion(region_id=rid, x=x, y=y, w=w, h=h, enabled=enabled)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeRuntime:
    """Records each OCR call and returns scripted lines per call (in order)."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def recognize_rgba(self, rgba, width, height, sequence=None):
        self.calls.append({"rgba_len": len(rgba), "width": width, "height": height, "sequence": sequence})
        lines = self.script.pop(0) if self.script else []
        return OCRFrameResult(
            sequence=sequence,
            lines=tuple(lines),
            elapsed_ms=0.0,
            backend="fake",
            roi_width=width,
            roi_height=height,
        )


def _frame(width=100, height=100):
    return ocr_test._MockCapture(argparse.Namespace()).capture_frame()


def _rgba(width=100, height=100):
    return bytes(width * height * 4)


def _coordinator(runtime, clock=None, **stab_kwargs):
    return mr.MultiRegionOCRCoordinator(
        runtime,
        stabilizer_factory=lambda: OCRStabilizer(**stab_kwargs),
        clock=clock or (lambda: 0.0),
    )


def _ids(events):
    return [event.region_id for event in events]


class BasicExecutionTest(unittest.TestCase):
    def test_m1_one_frame_two_regions(self) -> None:
        runtime = FakeRuntime([[line("A1")], [line("B1")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        events = coordinator.process_decoded(_rgba(), 100, 100, 42, [region("A"), region("B", x=0.5)])
        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(_ids(events), ["A", "B"])
        self.assertEqual(coordinator.stats.crops, 2)
        self.assertEqual(coordinator.stats.ocr_calls, 2)

    def test_m2_disabled_region_not_processed(self) -> None:
        runtime = FakeRuntime([[line("A1")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        events = coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B", enabled=False)])
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(_ids(events), ["A"])
        self.assertEqual(coordinator.state_ids(), ("A",))

    def test_m3_source_seq_shared(self) -> None:
        runtime = FakeRuntime([[line("A1")], [line("B1")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        coordinator.process_decoded(_rgba(), 100, 100, 77, [region("A"), region("B")])
        self.assertEqual(runtime.calls[0]["sequence"], 77)
        self.assertEqual(runtime.calls[1]["sequence"], 77)

    def test_m4_exact_pixel_geometry(self) -> None:
        rect = mr.region_pixel_rect(region("A", x=0.1, y=0.2, w=0.3, h=0.4), 100, 100)
        self.assertEqual(rect.as_tuple(), (10, 20, 30, 40))
        runtime = FakeRuntime([[line("A1")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A", x=0.1, y=0.2, w=0.3, h=0.4)])
        self.assertEqual((runtime.calls[0]["width"], runtime.calls[0]["height"]), (30, 40))

    def test_m5_decode_once(self) -> None:
        original = mr.decode_png_ex
        counter = {"n": 0}

        def counting(encoded):
            counter["n"] += 1
            return original(encoded)

        mr.decode_png_ex = counting
        try:
            runtime = FakeRuntime([[line("A1")], [line("B1")]])
            coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
            coordinator.process_frame(_frame(), [region("A"), region("B")])
        finally:
            mr.decode_png_ex = original
        self.assertEqual(counter["n"], 1)


class StabilizerIsolationTest(unittest.TestCase):
    def test_s1_independent_consensus(self) -> None:
        runtime = FakeRuntime([[line("Hello")], [line("Menu")], [line("Hello")], [line("Price")]])
        coordinator = _coordinator(runtime, consensus_required=2, history_size=2)
        first = coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        second = coordinator.process_decoded(_rgba(), 100, 100, 2, [region("A"), region("B")])
        self.assertEqual(first, [])
        self.assertEqual(_ids(second), ["A"])
        self.assertEqual(second[0].event.text, "Hello")

    def test_s2_simultaneous_different_text(self) -> None:
        runtime = FakeRuntime([[line("Hello")], [line("Menu")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        events = coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        self.assertEqual([e.event.text for e in events], ["Hello", "Menu"])

    def test_s3_clear_does_not_clear_other_region(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("Hello")], [line("Menu")], [], [line("Menu")]])
        coordinator = _coordinator(runtime, clock, consensus_required=1, history_size=1, stale_timeout_sec=2.0)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        clock.value = 3.0
        events = coordinator.process_decoded(_rgba(), 100, 100, 2, [region("A"), region("B")])
        self.assertEqual(_ids(events), ["A"])
        self.assertEqual(events[0].event.kind, "clear")
        b_state = coordinator._states["B"]
        self.assertEqual(b_state.stabilizer.last_emitted_text, "Menu")

    def test_s4_other_region_activity_does_not_postpone_clear(self) -> None:
        clock = Clock(0.0)
        runtime = FakeRuntime(
            [[line("Hello")], [line("Menu")], [], [line("Menu2")], [], [line("Menu2")]]
        )
        coordinator = _coordinator(runtime, clock, consensus_required=1, history_size=1, stale_timeout_sec=2.0)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        clock.value = 1.0
        coordinator.process_decoded(_rgba(), 100, 100, 2, [region("A"), region("B")])
        clock.value = 3.0
        events = coordinator.process_decoded(_rgba(), 100, 100, 3, [region("A"), region("B")])
        self.assertEqual(_ids(events), ["A"])
        self.assertEqual(events[0].event.kind, "clear")

    def test_s5_identical_text_two_regions_independent(self) -> None:
        runtime = FakeRuntime([[line("same")], [line("same")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        events = coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        self.assertEqual(_ids(events), ["A", "B"])
        self.assertEqual([e.event.text for e in events], ["same", "same"])


class RegionStateLifecycleTest(unittest.TestCase):
    def test_l1_removed_region_state_discarded_no_event(self) -> None:
        runtime = FakeRuntime([[line("Hello")], [line("Menu")], [line("Hello")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        events = coordinator.process_decoded(_rgba(), 100, 100, 2, [region("A")])
        self.assertEqual(coordinator.state_ids(), ("A",))
        self.assertGreaterEqual(coordinator.stats.states_discarded, 1)
        self.assertEqual(_ids(events), [])  # A duplicate-suppressed, B silently discarded

    def test_l2_disabled_region_state_discarded_no_clear(self) -> None:
        runtime = FakeRuntime([[line("Hello")], [line("Menu")], [line("Hello")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        events = coordinator.process_decoded(_rgba(), 100, 100, 2, [region("A"), region("B", enabled=False)])
        self.assertEqual(coordinator.state_ids(), ("A",))
        self.assertNotIn("clear", [e.event.kind for e in events])

    def test_l3_re_enabled_region_starts_fresh(self) -> None:
        runtime = FakeRuntime([[line("Hello")], [line("Hello")], [line("Hello")]])
        coordinator = _coordinator(runtime, consensus_required=2, history_size=2)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A")])  # history 1
        coordinator.process_decoded(_rgba(), 100, 100, 2, [region("A", enabled=False)])  # discard
        fresh = coordinator.process_decoded(_rgba(), 100, 100, 3, [region("A")])  # history 1 again
        self.assertEqual(fresh, [])
        emitted = coordinator.process_decoded(_rgba(), 100, 100, 4, [region("A")])  # history 2
        self.assertEqual(_ids(emitted), ["A"])

    def test_l4_geometry_change_resets_only_that_region(self) -> None:
        runtime = FakeRuntime([[line("Hello")], [line("Menu")], [line("Hello")], [line("Menu")]])
        coordinator = _coordinator(runtime, consensus_required=2, history_size=2)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        events = coordinator.process_decoded(
            _rgba(), 100, 100, 2, [region("A", x=0.5), region("B")]
        )
        self.assertEqual(coordinator.stats.states_reset, 1)
        self.assertEqual(_ids(events), ["B"])  # B preserved consensus, A reset

    def test_l5_reorder_preserves_state(self) -> None:
        runtime = FakeRuntime([[line("Hello")], [line("Menu")], [line("Menu")], [line("Hello")]])
        coordinator = _coordinator(runtime, consensus_required=2, history_size=2)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        events = coordinator.process_decoded(_rgba(), 100, 100, 2, [region("B"), region("A")])
        self.assertEqual(coordinator.stats.states_reset, 0)
        self.assertEqual(_ids(events), ["B", "A"])  # output follows current order


class EventTaggingTest(unittest.TestCase):
    def test_e1_region_id_exact(self) -> None:
        runtime = FakeRuntime([[line("A")], [line("B")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        events = coordinator.process_decoded(_rgba(), 100, 100, 1, [region("region-x"), region("region-y")])
        self.assertEqual([e.region_id for e in events], ["region-x", "region-y"])

    def test_e2_deterministic_collection_order(self) -> None:
        runtime = FakeRuntime([[line("A")], [line("B")], [line("C")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        events = coordinator.process_decoded(
            _rgba(), 100, 100, 1, [region("C"), region("A"), region("B")]
        )
        self.assertEqual(_ids(events), ["C", "A", "B"])

    def test_e3_canonical_stable_event_semantics(self) -> None:
        # The coordinator adds no normalization of its own; the wrapped event is
        # the canonical stabilizer output (which preserves CJK/newlines exactly).
        runtime = FakeRuntime([[line("多行\nCJK!", 0.93)]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        events = coordinator.process_decoded(_rgba(), 100, 100, 9, [region("A")])
        event = events[0].event
        self.assertEqual(event.kind, "text")
        self.assertEqual(event.text, "多行\nCJK!")
        self.assertAlmostEqual(event.confidence, 0.93)
        self.assertEqual(event.source_seq, 9)


class RuntimeReuseTest(unittest.TestCase):
    def test_r1_one_runtime_object(self) -> None:
        runtime = FakeRuntime([[line("A")], [line("B")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        self.assertIs(coordinator._runtime, runtime)

    def test_r2_sequential_calls(self) -> None:
        order = []

        class SequentialRuntime(FakeRuntime):
            def recognize_rgba(self, rgba, width, height, sequence=None):
                order.append("enter")
                result = super().recognize_rgba(rgba, width, height, sequence)
                order.append("exit")
                return result

        runtime = SequentialRuntime([[line("A")], [line("B")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        coordinator.process_decoded(_rgba(), 100, 100, 1, [region("A"), region("B")])
        self.assertEqual(order, ["enter", "exit", "enter", "exit"])

    def test_r3_bounded_by_enabled_count(self) -> None:
        runtime = FakeRuntime([[line("A")], [line("B")], [line("C")]])
        coordinator = _coordinator(runtime, consensus_required=1, history_size=1)
        coordinator.process_decoded(
            _rgba(), 100, 100, 1, [region("A"), region("B", enabled=False), region("C")]
        )
        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(coordinator.stats.ocr_calls, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
