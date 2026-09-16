#!/usr/bin/env python3
"""Phase 2L.4 tests: opt-in multi-region OCR worker integration.

Deterministic, no real RapidOCR runtime, no renderer. Covers mode selection,
effective region resolution, v2 + primary-v1 wire emission, global sequencing,
execution efficiency, backend compatibility, and overlay compatibility.

Run:
    python3 scripts/test_multi_region_worker.py
"""

from __future__ import annotations

import argparse
import asyncio
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
import ocr.multi_region as mr  # noqa: E402
import ocr.transport as t  # noqa: E402
import scripts.ocr_worker as worker  # noqa: E402
from backend.ocr_transport import OCRTransportReceiver  # noqa: E402
from backend.overlay_delivery import OverlayDeliveryObserver  # noqa: E402
from backend.overlay_text import OverlayTextCoordinator  # noqa: E402
from capture import recognition_roi, recognition_regions as rr  # noqa: E402
from capture.recognition_regions import RecognitionRegion  # noqa: E402
from capture.scheduler import OCRChangeGate  # noqa: E402
from ocr.result import OCRFrameResult, OCRLine  # noqa: E402
from ocr.stabilizer import OCRStabilizer, StableTextEvent  # noqa: E402
from ocr.transport import decode_envelope  # noqa: E402


def line(text, confidence=0.9):
    return OCRLine(text=text, confidence=confidence, box=None)


def region(rid, x=0.1, y=0.1, w=0.2, h=0.2, enabled=True):
    return RecognitionRegion(region_id=rid, x=x, y=y, w=w, h=h, enabled=enabled)


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
        changed = self._changes[min(self._index, len(self._changes) - 1)] if self._changes else False
        self._index += 1
        return SimpleNamespace(changed=changed, score=0.0, reason="x", age_ms=0.0)

    def reset_state(self):
        self._index = 0


class BrokenDetector:
    def classify_rgba(self, **kwargs):
        raise RuntimeError("detector boom")

    def reset_state(self):
        pass


def gate(detector, clock, force=3.0):
    return OCRChangeGate(detector, force_interval_sec=force, clock=clock)


def _gated_coordinator(runtime, gates, clock):
    queue = list(gates)
    return mr.MultiRegionOCRCoordinator(
        runtime,
        stabilizer_factory=lambda: OCRStabilizer(consensus_required=1, history_size=1, stale_timeout_sec=2.0),
        gate_factory=lambda: queue.pop(0),
        clock=clock,
    )


class FakeRuntime:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def recognize_rgba(self, rgba, width, height, sequence=None):
        self.calls.append({"width": width, "height": height, "sequence": sequence})
        lines = self.script.pop(0) if self.script else []
        return OCRFrameResult(
            sequence=sequence, lines=tuple(lines), elapsed_ms=0.0, backend="fake", roi_width=width, roi_height=height
        )


class SpyDelivery:
    def __init__(self):
        self.sessions = []
        self.submitted = []

    def set_session(self, session_id):
        self.sessions.append(session_id)

    def submit(self, action):
        self.submitted.append(action)


def _args(**overrides):
    base = dict(app_id=None, debug=False, debug_roi_copy=None, json=False, multi_region=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _frame():
    return ocr_test._MockCapture(_args()).capture_frame()


def _diag(machine, regions, runtime, primary=None, clock=None, multi_region=True):
    diagnostic = ocr_test.OCRDiagnostic(
        _args(multi_region=multi_region),
        runtime=runtime,
        machine_stream=machine,
        multi_region_regions=regions,
    )
    diagnostic._multi_region_primary_id = (
        primary if primary is not None else (regions[0].region_id if regions else None)
    )
    diagnostic._multi_region_coordinator = mr.MultiRegionOCRCoordinator(
        runtime,
        stabilizer_factory=lambda: OCRStabilizer(consensus_required=1, history_size=1, stale_timeout_sec=2.0),
        clock=clock or (lambda: 0.0),
    )
    return diagnostic


def _wire(machine):
    return [json.loads(line) for line in machine.getvalue().splitlines() if line.strip()]


class ModeSelectionTest(unittest.TestCase):
    def test_w1_default_worker_mode_unchanged(self) -> None:
        diagnostic = ocr_test.OCRDiagnostic(_args(), runtime=None)
        self.assertFalse(diagnostic._multi_region_enabled)
        self.assertIsNone(diagnostic._multi_region_coordinator)

    def test_w2_explicit_flag_selects_multi_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            rr.RegionConfigStore(path).set_regions(None, rr.RecognitionRegionSet((region("A"),)))
            saved = (recognition_roi._STORE, recognition_roi._RESOLVER)
            try:
                recognition_roi.configure(path)
                # Config presence alone must NOT enable multi-region.
                off = ocr_test.OCRDiagnostic(_args(multi_region=False), runtime=None)
                self.assertFalse(off._multi_region_enabled)
                on = ocr_test.OCRDiagnostic(_args(multi_region=True), runtime=None)
                self.assertTrue(on._multi_region_enabled)
            finally:
                recognition_roi._STORE, recognition_roi._RESOLVER = saved

    def test_w2_multi_region_change_gate_accepted(self) -> None:
        args = worker._parse_args(["--multi-region", "--change-gate"])
        self.assertTrue(args.multi_region and args.change_gate)
        diagnostic = ocr_test.OCRDiagnostic(
            _args(multi_region=True), runtime=FakeRuntime(), gate=object(), multi_region_regions=(region("A"),)
        )
        diagnostic._setup_multi_region()
        self.assertIsNotNone(diagnostic._multi_region_coordinator)
        self.assertIsNotNone(diagnostic._multi_region_coordinator._gate_factory)

    def test_w1_multi_region_gate_off_has_no_gates(self) -> None:
        diagnostic = _diag(io.StringIO(), [region("A")], FakeRuntime())
        self.assertIsNone(diagnostic._multi_region_coordinator._gate_factory)

    def test_w4_legacy_change_gate_unchanged(self) -> None:
        diagnostic = ocr_test.OCRDiagnostic(_args(multi_region=False), runtime=None, gate=object())
        self.assertFalse(diagnostic._multi_region_enabled)

    def test_w3_one_unchanged_one_changing(self) -> None:
        machine = io.StringIO()
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("B1")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime, clock=clock)
        diagnostic._multi_region_coordinator = _gated_coordinator(
            runtime,
            [gate(FakeDetector([True, False]), clock, 1000), gate(FakeDetector([True, True]), clock, 1000)],
            clock,
        )
        diagnostic._process_multi_region(_frame(), 0.0)  # warm-up: both
        diagnostic._process_multi_region(_frame(), 0.0)  # A skip, B OCR
        wire = _wire(machine)
        # A has a v1 projection; B never does.
        self.assertEqual([(w["v"], w.get("region_id")) for w in wire], [(2, "A"), (1, None), (2, "B"), (2, "B")])

    def test_w4_forced_refresh_causes_region_ocr(self) -> None:
        machine = io.StringIO()
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("A1")]])
        diagnostic = _diag(machine, [region("A")], runtime, clock=clock)
        diagnostic._multi_region_coordinator = _gated_coordinator(
            runtime, [gate(FakeDetector([True, False]), clock, 3.0)], clock
        )
        diagnostic._process_multi_region(_frame(), 0.0)  # warm-up
        before = len(runtime.calls)
        clock.value = 3.5
        diagnostic._process_multi_region(_frame(), 0.0)  # forced refresh
        self.assertEqual(len(runtime.calls) - before, 1)

    def test_w5_detector_error_fail_open_per_region(self) -> None:
        machine = io.StringIO()
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("A1")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime, clock=clock)
        diagnostic._multi_region_coordinator = _gated_coordinator(
            runtime,
            [gate(BrokenDetector(), clock, 1000), gate(FakeDetector([True, False]), clock, 1000)],
            clock,
        )
        diagnostic._process_multi_region(_frame(), 0.0)  # warm-up
        before = len(runtime.calls)
        diagnostic._process_multi_region(_frame(), 0.0)  # A fail-open, B skip
        self.assertEqual(len(runtime.calls) - before, 1)

    def test_w6_legacy_single_region_gate_unchanged(self) -> None:
        diagnostic = ocr_test.OCRDiagnostic(_args(multi_region=False), runtime=None, gate=object())
        self.assertFalse(diagnostic._multi_region_enabled)
        self.assertIsNone(diagnostic._multi_region_coordinator)


class ConfigResolutionTest(unittest.TestCase):
    def _resolve(self, path, app_id=None):
        saved = (recognition_roi._STORE, recognition_roi._RESOLVER)
        try:
            recognition_roi.configure(path)
            diagnostic = ocr_test.OCRDiagnostic(
                _args(multi_region=True, app_id=app_id), runtime=None
            )
            return diagnostic._resolve_effective_regions()
        finally:
            recognition_roi._STORE, recognition_roi._RESOLVER = saved

    def _path(self, tmp):
        return Path(tmp) / "recognition_roi.json"

    def test_c1_v2_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp)
            rr.RegionConfigStore(path).set_regions(
                None, rr.RecognitionRegionSet((region("g1"), region("g2", x=0.5)))
            )
            regions = self._resolve(path)
            self.assertEqual([r.region_id for r in regions], ["g1", "g2"])

    def test_c2_v2_per_game_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp)
            store = rr.RegionConfigStore(path)
            store.set_regions(None, rr.RecognitionRegionSet((region("global"),)))
            store.set_regions("app1", rr.RecognitionRegionSet((region("game"),)))
            regions = self._resolve(path, app_id="app1")
            self.assertEqual([r.region_id for r in regions], ["game"])

    def test_c3_legacy_v1_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp)
            path.write_text(
                json.dumps({"version": 1, "default_roi": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}}),
                encoding="utf-8",
            )
            regions = self._resolve(path)
            self.assertEqual(len(regions), 1)
            self.assertAlmostEqual(regions[0].x, 0.1)

    def test_c4_builtin_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            regions = self._resolve(self._path(tmp))
            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0].region_id, rr.BUILTIN_REGION_ID)

    def test_c5_explicit_fully_disabled_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp)
            rr.RegionConfigStore(path).set_regions(
                None, rr.RecognitionRegionSet((region("off", enabled=False),))
            )
            regions = self._resolve(path)
            self.assertEqual(len(regions), 1)
            self.assertFalse(regions[0].enabled)
            runtime = FakeRuntime()
            diagnostic = _diag(io.StringIO(), regions, runtime)
            diagnostic._process_multi_region(_frame(), 0.0)
            self.assertEqual(runtime.calls, [])
            self.assertEqual(diagnostic.machine_stream.getvalue(), "")


class WireEmissionTest(unittest.TestCase):
    def test_e1_one_primary_text(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[line("Hello")]])
        diagnostic = _diag(machine, [region("A")], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        wire = _wire(machine)
        self.assertEqual(len(wire), 2)
        self.assertEqual((wire[0]["v"], wire[0]["region_id"], wire[0]["kind"], wire[0]["event_seq"]), (2, "A", "text", 1))
        self.assertEqual((wire[1]["v"], wire[1]["kind"], wire[1]["event_seq"], wire[1]["text"]), (1, "text", 2, "Hello"))
        self.assertNotIn("region_id", wire[1])

    def test_e2_primary_and_secondary_text(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[line("A-text")], [line("B-text")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        wire = _wire(machine)
        self.assertEqual([(w["v"], w.get("region_id"), w["event_seq"]) for w in wire], [(2, "A", 1), (1, None, 2), (2, "B", 3)])

    def test_e3_secondary_only_event(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[], [line("B-text")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        wire = _wire(machine)
        self.assertEqual([(w["v"], w["region_id"]) for w in wire], [(2, "B")])

    def test_e4_primary_clear(self) -> None:
        machine = io.StringIO()
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("Hello")], []])
        diagnostic = _diag(machine, [region("A")], runtime, clock=clock)
        diagnostic._process_multi_region(_frame(), 0.0)
        clock.value = 3.0
        diagnostic._process_multi_region(_frame(), 0.0)
        wire = _wire(machine)
        self.assertEqual([(w["v"], w["kind"], w["event_seq"]) for w in wire], [(2, "text", 1), (1, "text", 2), (2, "clear", 3), (1, "clear", 4)])
        for w in wire[2:]:
            self.assertEqual(w["text"], "")
            self.assertIsNone(w["confidence"])
            self.assertIsNone(w["source_seq"])

    def test_e5_secondary_clear(self) -> None:
        machine = io.StringIO()
        clock = Clock(0.0)
        runtime = FakeRuntime([[line("A-text")], [line("B-text")], [line("A-text")], []])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime, clock=clock)
        diagnostic._process_multi_region(_frame(), 0.0)
        clock.value = 3.0
        diagnostic._process_multi_region(_frame(), 0.0)
        wire = _wire(machine)
        self.assertEqual([(w["v"], w.get("region_id"), w["kind"]) for w in wire], [(2, "A", "text"), (1, None, "text"), (2, "B", "text"), (2, "B", "clear")])

    def test_e6_unicode_multiline_exact(self) -> None:
        machine = io.StringIO()
        text = "第一行\n第二行！"
        runtime = FakeRuntime([[line(text)]])
        diagnostic = _diag(machine, [region("A")], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        wire = _wire(machine)
        self.assertEqual(wire[0]["text"], text)
        self.assertEqual(wire[1]["text"], text)


class GlobalSequencingTest(unittest.TestCase):
    def test_s1_sequence_starts_at_one(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[line("Hello")]])
        diagnostic = _diag(machine, [region("A")], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        self.assertEqual(_wire(machine)[0]["event_seq"], 1)

    def test_s2_projection_consumes_sequence(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[line("Hello")]])
        diagnostic = _diag(machine, [region("A")], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        seqs = [w["event_seq"] for w in _wire(machine)]
        self.assertEqual(seqs, [1, 2])

    def test_s3_multi_frame_strict_order(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[line("A1")], [line("B1")], [line("B2")], [line("A2")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        diagnostic._process_multi_region(_frame(), 0.0)
        seqs = [w["event_seq"] for w in _wire(machine)]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_s4_same_source_seq_across_regions(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[line("A-text")], [line("B-text")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        receiver = OCRTransportReceiver(session_id="s1")
        for line_json in machine.getvalue().splitlines():
            self.assertTrue(receiver.handle_line(line_json))
        self.assertEqual(receiver.status()["transport_out_of_order"], 0)


class EfficiencyTest(unittest.TestCase):
    def test_p1_p2_one_decode(self) -> None:
        original = mr.decode_png_ex
        counter = {"n": 0}

        def counting(encoded):
            counter["n"] += 1
            return original(encoded)

        mr.decode_png_ex = counting
        try:
            machine = io.StringIO()
            runtime = FakeRuntime([[line("A")], [line("B")]])
            diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime)
            diagnostic._process_multi_region(_frame(), 0.0)
        finally:
            mr.decode_png_ex = original
        self.assertEqual(counter["n"], 1)

    def test_p3_one_runtime(self) -> None:
        runtime = FakeRuntime([[line("A")], [line("B")]])
        diagnostic = _diag(io.StringIO(), [region("A"), region("B", x=0.5)], runtime)
        self.assertIs(diagnostic._multi_region_coordinator._runtime, runtime)

    def test_p4_no_duplicate_primary_ocr(self) -> None:
        machine = io.StringIO()
        runtime = FakeRuntime([[line("A-text")], [line("B-text")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime)
        diagnostic._process_multi_region(_frame(), 0.0)
        # Two regions -> exactly two OCR calls, despite the extra v1 projection.
        self.assertEqual(len(runtime.calls), 2)


class BackendCompatibilityTest(unittest.TestCase):
    def _wire_lines(self, regions, script, primary=None, clock=None):
        machine = io.StringIO()
        runtime = FakeRuntime(script)
        diagnostic = _diag(machine, regions, runtime, primary=primary, clock=clock)
        for _ in range(len(script) // max(1, len(regions))):
            diagnostic._process_multi_region(_frame(), 0.0)
            if clock is not None:
                clock.value += 3.0
        return [line for line in machine.getvalue().splitlines() if line.strip()]

    def test_b1_primary_region(self) -> None:
        lines = self._wire_lines([region("A")], [[line("Hello")]])
        receiver = OCRTransportReceiver(session_id="s1")
        for line_json in lines:
            self.assertTrue(receiver.handle_line(line_json))
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "Hello")
        self.assertEqual(receiver.state().text, "Hello")

    def test_b2_secondary_region(self) -> None:
        lines = self._wire_lines([region("A"), region("B", x=0.5)], [[line("A-text")], [line("B-text")]])
        receiver = OCRTransportReceiver(session_id="s1")
        for line_json in lines:
            self.assertTrue(receiver.handle_line(line_json))
        self.assertEqual(receiver.latest_stable_text_by_region("B")["text"], "B-text")
        self.assertEqual(receiver.state().text, "A-text")

    def test_b3_primary_clear(self) -> None:
        clock = Clock(0.0)
        lines = self._wire_lines([region("A")], [[line("Hello")], []], clock=clock)
        receiver = OCRTransportReceiver(session_id="s1")
        for line_json in lines:
            self.assertTrue(receiver.handle_line(line_json))
        self.assertEqual(receiver.latest_stable_text_by_region("A")["kind"], "clear")
        self.assertEqual(receiver.state().kind, "clear")

    def test_b4_secondary_clear(self) -> None:
        clock = Clock(0.0)
        lines = self._wire_lines([region("A"), region("B", x=0.5)], [[line("A")], [line("B")], [line("A")], []], clock=clock)
        receiver = OCRTransportReceiver(session_id="s1")
        for line_json in lines:
            self.assertTrue(receiver.handle_line(line_json))
        self.assertEqual(receiver.latest_stable_text_by_region("B")["kind"], "clear")
        self.assertEqual(receiver.state().text, "A")

    def test_b5_strict_event_seq_end_to_end(self) -> None:
        lines = self._wire_lines([region("A"), region("B", x=0.5)], [[line("A")], [line("B")]])
        receiver = OCRTransportReceiver(session_id="s1")
        for line_json in lines:
            self.assertTrue(receiver.handle_line(line_json))
        status = receiver.status()
        self.assertEqual(status["transport_messages_rejected"], 0)
        self.assertEqual(status["transport_out_of_order"], 0)

    def test_b6_mixed_gated_stream(self) -> None:
        # Primary A (with v1 projection) + secondary B (v2 only), gated.
        clock = Clock(0.0)
        machine = io.StringIO()
        runtime = FakeRuntime([[line("A0")], [line("B0")], [line("B1")]])
        diagnostic = _diag(machine, [region("A"), region("B", x=0.5)], runtime, clock=clock)
        diagnostic._multi_region_coordinator = _gated_coordinator(
            runtime,
            [gate(FakeDetector([True, False]), clock, 1000), gate(FakeDetector([True, True]), clock, 1000)],
            clock,
        )
        diagnostic._process_multi_region(_frame(), 0.0)
        diagnostic._process_multi_region(_frame(), 0.0)
        lines = [line_json for line_json in machine.getvalue().splitlines() if line_json.strip()]
        receiver = OCRTransportReceiver(session_id="s1")
        for line_json in lines:
            self.assertTrue(receiver.handle_line(line_json))
        status = receiver.status()
        self.assertEqual(status["transport_messages_rejected"], 0)
        self.assertEqual(status["transport_out_of_order"], 0)
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "A0")
        self.assertEqual(receiver.latest_stable_text_by_region("B")["text"], "B1")
        self.assertEqual(receiver.state().text, "A0")  # legacy only from primary v1 projection


class OverlayCompatibilityTest(unittest.TestCase):
    def _receiver(self):
        delivery = SpyDelivery()
        coordinator = OverlayTextCoordinator()
        observer = OverlayDeliveryObserver(coordinator, delivery)
        receiver = OCRTransportReceiver(session_id="s1", observer=observer)
        receiver.begin_session("s1")
        return receiver, delivery

    def _lines(self, regions, script, primary=None, clock=None):
        machine = io.StringIO()
        runtime = FakeRuntime(script)
        diagnostic = _diag(machine, regions, runtime, primary=primary, clock=clock)
        for _ in range(len(script) // max(1, len(regions))):
            diagnostic._process_multi_region(_frame(), 0.0)
            if clock is not None:
                clock.value += 3.0
        return [line for line in machine.getvalue().splitlines() if line.strip()]

    def test_o1_v2_primary_alone_no_overlay_action(self) -> None:
        lines = self._lines([region("A")], [[line("Hello")]])
        receiver, delivery = self._receiver()
        # Feed only the v2 line (skip the v1 projection).
        receiver.handle_line(lines[0])
        self.assertEqual(delivery.submitted, [])

    def test_o2_paired_v1_primary_is_one_action(self) -> None:
        lines = self._lines([region("A")], [[line("Hello")]])
        receiver, delivery = self._receiver()
        for line_json in lines:
            receiver.handle_line(line_json)
        self.assertEqual(len(delivery.submitted), 1)
        self.assertEqual(delivery.submitted[0].text, "Hello")

    def test_o3_secondary_v2_no_overlay_action(self) -> None:
        lines = self._lines([region("A"), region("B", x=0.5)], [[line("A")], [line("B")]])
        receiver, delivery = self._receiver()
        # Feed only the secondary v2 line (lines[2]).
        receiver.handle_line(lines[2])
        self.assertEqual(delivery.submitted, [])

    def test_o4_primary_clear_projection_is_one_hide(self) -> None:
        clock = Clock(0.0)
        lines = self._lines([region("A")], [[line("Hello")], []], clock=clock)
        receiver, delivery = self._receiver()
        for line_json in lines:
            receiver.handle_line(line_json)
        hides = [action for action in delivery.submitted if action.kind == "hide"]
        self.assertEqual(len(hides), 1)

    def test_o5_primary_tick_clear_projection_is_one_hide(self) -> None:
        clock = Clock(0.0)
        machine = io.StringIO()
        runtime = FakeRuntime([[line("Hello")], []])
        diagnostic = _diag(machine, [region("A")], runtime, clock=clock)
        diagnostic._multi_region_coordinator = _gated_coordinator(
            runtime, [gate(FakeDetector([True, True, False, False]), clock, 1000)], clock
        )
        diagnostic._process_multi_region(_frame(), 0.0)  # text
        clock.value = 0.5
        diagnostic._process_multi_region(_frame(), 0.0)  # real no-text
        clock.value = 1.0
        diagnostic._process_multi_region(_frame(), 0.0)  # skip tick
        clock.value = 3.0
        diagnostic._process_multi_region(_frame(), 0.0)  # skip tick -> clear
        lines = [line_json for line_json in machine.getvalue().splitlines() if line_json.strip()]
        receiver, delivery = self._receiver()
        for line_json in lines:
            receiver.handle_line(line_json)
        hides = [action for action in delivery.submitted if action.kind == "hide"]
        self.assertEqual(len(hides), 1)

    def test_o6_secondary_tick_clear_no_overlay_action(self) -> None:
        clear = StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=1.0)
        line = t.encode_region_stable_text_event(1, mr.RegionStableTextEvent(region_id="B", event=clear))
        receiver, delivery = self._receiver()
        self.assertTrue(receiver.handle_line(line))
        self.assertEqual(delivery.submitted, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
