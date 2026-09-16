#!/usr/bin/env python3
"""Phase 2L.3 tests: region-tagged StableText transport v2 + backend per-region state.

Pure stdlib. Covers v2 serialization, global sequencing, backend per-region
authoritative state, observer behavior, canonical clear, and the defensive
single-block overlay guard.

Run:
    python3 scripts/test_region_transport.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ocr.transport as t  # noqa: E402
from backend.ocr_transport import AcceptedStableTextEvent, OCRTransportReceiver  # noqa: E402
from backend.overlay_delivery import OverlayDeliveryObserver  # noqa: E402
from backend.overlay_text import OverlayTextCoordinator  # noqa: E402
from ocr.multi_region import RegionStableTextEvent  # noqa: E402
from ocr.stabilizer import OCRStabilizer, StableTextEvent  # noqa: E402


class Line:
    def __init__(self, text, confidence=0.9):
        self.text = text
        self.confidence = confidence


def text_event(text="Hello", confidence=0.98, source_seq=42, ts=123.45):
    return StableTextEvent(kind="text", text=text, confidence=confidence, source_seq=source_seq, timestamp_monotonic=ts)


def clear_event(ts=124.0):
    return StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=ts)


def region_event(region_id, event):
    return RegionStableTextEvent(region_id=region_id, event=event)


def v2_text(region_id="A", seq=1, text="Hello", confidence=0.98, source_seq=42, ts=1.0):
    return t.encode_region_stable_text_event(seq, region_event(region_id, text_event(text, confidence, source_seq, ts)))


def v2_clear(region_id="A", seq=1, ts=1.0):
    return t.encode_region_stable_text_event(seq, region_event(region_id, clear_event(ts)))


class V2SerializationTest(unittest.TestCase):
    def test_t1_v2_text_round_trip(self) -> None:
        line = v2_text("reg-a", 17, "Hello", 0.98, 42, 123.45)
        payload = json.loads(line)
        self.assertEqual(payload["v"], 2)
        self.assertEqual(payload["type"], "stable_text")
        self.assertEqual(payload["region_id"], "reg-a")
        env = t.decode_envelope(line)
        self.assertEqual(env.version, 2)
        self.assertEqual(env.region_id, "reg-a")
        self.assertEqual(env.kind, "text")
        self.assertEqual(env.text, "Hello")
        self.assertAlmostEqual(env.confidence, 0.98)
        self.assertEqual(env.source_seq, 42)
        self.assertAlmostEqual(env.timestamp_monotonic, 123.45)

    def test_t2_v2_clear_canonical(self) -> None:
        env = t.decode_envelope(v2_clear("reg-a", 18, 124.0))
        self.assertEqual(env.kind, "clear")
        self.assertEqual(env.text, "")
        self.assertIsNone(env.confidence)
        self.assertIsNone(env.source_seq)

    def test_t3_region_id_validation(self) -> None:
        for bad in ("", None, 123, "x" * (t.MAX_REGION_ID_LENGTH + 1)):
            with self.subTest(region_id=bad):
                with self.assertRaises(t.TransportError):
                    t.encode_region_stable_text_event(1, region_event(bad, text_event()))
        # decode-side rejection too
        with self.assertRaises(t.TransportError):
            t.decode_envelope('{"v":2,"type":"stable_text","event_seq":1,"region_id":"","kind":"text","text":"x","confidence":0.9,"source_seq":1,"timestamp_monotonic":1.0}')

    def test_t4_64kib_limit_preserved(self) -> None:
        huge = "a" * (t.MAX_LINE_BYTES + 10)
        line = v2_text("A", 1, huge)
        with self.assertRaises(t.TransportError) as ctx:
            t.decode_envelope(line)
        self.assertEqual(ctx.exception.code, "line_too_large")

    def test_t5_unicode_newline_preserved(self) -> None:
        env = t.decode_envelope(v2_text("A", 1, "第一行\n第二行！"))
        self.assertEqual(env.text, "第一行\n第二行！")


class GlobalSequencingTest(unittest.TestCase):
    def test_s1_global_seq_across_regions(self) -> None:
        events = [
            region_event("A", text_event("one")),
            region_event("B", text_event("two")),
            region_event("A", clear_event()),
        ]
        lines = t.encode_region_stable_text_stream(events)
        seqs = [t.decode_envelope(line).event_seq for line in lines]
        self.assertEqual(seqs, [1, 2, 3])
        self.assertEqual([t.decode_envelope(line).region_id for line in lines], ["A", "B", "A"])

    def test_s2_same_source_seq_across_regions_allowed(self) -> None:
        receiver = OCRTransportReceiver(session_id="s1")
        self.assertTrue(receiver.handle_line(v2_text("A", 1, "one", source_seq=42)))
        self.assertTrue(receiver.handle_line(v2_text("B", 2, "two", source_seq=42)))

    def test_s3_duplicate_event_seq_different_region_rejected(self) -> None:
        receiver = OCRTransportReceiver(session_id="s1")
        self.assertTrue(receiver.handle_line(v2_text("A", 1, "one")))
        self.assertFalse(receiver.handle_line(v2_text("B", 1, "two")))
        self.assertIsNone(receiver.latest_stable_text_by_region("B"))

    def test_s4_out_of_order_different_region_rejected(self) -> None:
        receiver = OCRTransportReceiver(session_id="s1")
        self.assertTrue(receiver.handle_line(v2_text("A", 10, "one")))
        self.assertFalse(receiver.handle_line(v2_text("B", 9, "two")))
        self.assertIsNone(receiver.latest_stable_text_by_region("B"))
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "one")


class BackendPerRegionStateTest(unittest.TestCase):
    def _receiver(self):
        receiver = OCRTransportReceiver(session_id="s1")
        receiver.handle_line(v2_text("A", 1, "one"))
        receiver.handle_line(v2_text("B", 2, "two"))
        return receiver

    def test_b1_independent_text_states(self) -> None:
        receiver = self._receiver()
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "one")
        self.assertEqual(receiver.latest_stable_text_by_region("B")["text"], "two")

    def test_b2_clear_a_leaves_b_untouched(self) -> None:
        receiver = self._receiver()
        self.assertTrue(receiver.handle_line(v2_clear("A", 3)))
        self.assertEqual(receiver.latest_stable_text_by_region("A")["kind"], "clear")
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "")
        self.assertEqual(receiver.latest_stable_text_by_region("B")["text"], "two")

    def test_b3_later_a_text_restores_only_a(self) -> None:
        receiver = self._receiver()
        receiver.handle_line(v2_clear("A", 3))
        receiver.handle_line(v2_text("A", 4, "three"))
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "three")
        self.assertEqual(receiver.latest_stable_text_by_region("B")["text"], "two")

    def test_b4_begin_session_clears_all_region_state(self) -> None:
        receiver = self._receiver()
        receiver.begin_session("s2")
        self.assertEqual(receiver.latest_stable_text_regions(), {})

    def test_b5_v1_and_v2_state_independent(self) -> None:
        receiver = OCRTransportReceiver(session_id="s1")
        v1_line = t.encode_envelope(t.envelope_from_event(1, text_event("legacy")))
        self.assertTrue(receiver.handle_line(v1_line))
        self.assertTrue(receiver.handle_line(v2_text("A", 2, "region")))
        self.assertEqual(receiver.state().text, "legacy")
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "region")


class RecordingObserver:
    def __init__(self, fail=False):
        self.sessions = []
        self.events = []
        self.fail = fail

    def begin_session(self, worker_session_id):
        self.sessions.append(worker_session_id)

    def on_accepted_event(self, event):
        if self.fail:
            raise RuntimeError("boom")
        self.events.append(event)


class ObserverBehaviorTest(unittest.TestCase):
    def test_o1_accepted_v2_observer_sees_region_id(self) -> None:
        observer = RecordingObserver()
        receiver = OCRTransportReceiver(session_id="s1", observer=observer)
        receiver.handle_line(v2_text("reg-a", 1))
        self.assertEqual(observer.events[0].region_id, "reg-a")
        self.assertEqual(observer.events[0].transport_version, 2)

    def test_o2_rejected_v2_never_reaches_observer(self) -> None:
        observer = RecordingObserver()
        receiver = OCRTransportReceiver(session_id="s1", observer=observer)
        receiver.handle_line("{not json")
        receiver.handle_line(v2_text("A", 1))
        receiver.handle_line(v2_text("A", 1))  # duplicate
        self.assertEqual(len(observer.events), 1)

    def test_o3_observer_exception_isolated(self) -> None:
        observer = RecordingObserver(fail=True)
        receiver = OCRTransportReceiver(session_id="s1", observer=observer)
        self.assertTrue(receiver.handle_line(v2_text("A", 1, "committed")))
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "committed")
        self.assertEqual(receiver.status()["observer_errors"], 1)

    def test_o4_v1_observer_unchanged(self) -> None:
        observer = RecordingObserver()
        receiver = OCRTransportReceiver(session_id="s1", observer=observer)
        receiver.handle_line(t.encode_envelope(t.envelope_from_event(1, text_event("v1"))))
        self.assertIsNone(observer.events[0].region_id)
        self.assertEqual(observer.events[0].transport_version, 1)

    def test_o5_single_block_overlay_adapter_ignores_v2(self) -> None:
        class SpyDelivery:
            def __init__(self):
                self.sessions = []
                self.submitted = []

            def set_session(self, session_id):
                self.sessions.append(session_id)

            def submit(self, action):
                self.submitted.append(action)

        delivery = SpyDelivery()
        coordinator = OverlayTextCoordinator()
        observer = OverlayDeliveryObserver(coordinator, delivery)

        v2 = AcceptedStableTextEvent(
            worker_session_id="s1",
            event_seq=1,
            kind="text",
            text="region text",
            confidence=0.9,
            source_seq=1,
            timestamp_monotonic=1.0,
            region_id="A",
            transport_version=2,
        )
        observer.on_accepted_event(v2)
        self.assertEqual(delivery.submitted, [])
        self.assertIsNone(coordinator.latest_action())

        observer.begin_session("s1")
        v1 = AcceptedStableTextEvent(
            worker_session_id="s1",
            event_seq=2,
            kind="text",
            text="legacy text",
            confidence=0.9,
            source_seq=2,
            timestamp_monotonic=2.0,
        )
        observer.on_accepted_event(v1)
        self.assertEqual(len(delivery.submitted), 1)


class CanonicalClearTest(unittest.TestCase):
    def _stable(self):
        stabilizer = OCRStabilizer(stale_timeout_sec=2.0)
        stabilizer.observe([Line("A")], 1, 0.0)
        stabilizer.observe([Line("A")], 2, 0.5)
        return stabilizer

    def test_c1_observe_clear_source_seq_none(self) -> None:
        events = self._stable().observe([], 3, 3.0)
        self.assertEqual(events[0].kind, "clear")
        self.assertIsNone(events[0].source_seq)

    def test_c2_tick_clear_source_seq_none(self) -> None:
        stabilizer = self._stable()
        stabilizer.observe([], 3, 1.0)
        events = stabilizer.tick(3.0)
        self.assertEqual(events[0].kind, "clear")
        self.assertIsNone(events[0].source_seq)

    def test_c3_both_encode_through_v1_encoder(self) -> None:
        observe_clear = self._stable().observe([], 3, 3.0)[0]
        stabilizer = self._stable()
        stabilizer.observe([], 3, 1.0)
        tick_clear = stabilizer.tick(3.0)[0]
        for event in (observe_clear, tick_clear):
            decoded = t.decode_envelope(t.encode_envelope(t.envelope_from_event(1, event)))
            self.assertEqual(decoded.kind, "clear")
            self.assertIsNone(decoded.source_seq)
            self.assertEqual(decoded.text, "")
            self.assertIsNone(decoded.confidence)

    def test_c4_v2_wrapped_clear_encodes(self) -> None:
        observe_clear = self._stable().observe([], 3, 3.0)[0]
        decoded = t.decode_envelope(t.encode_region_stable_text_event(1, region_event("A", observe_clear)))
        self.assertEqual(decoded.kind, "clear")
        self.assertEqual(decoded.region_id, "A")
        self.assertIsNone(decoded.source_seq)

    def test_encoder_canonicalizes_non_canonical_clear(self) -> None:
        # Defense in depth: even a producer that forgets canonical clear fields
        # cannot emit a second clear shape.
        env = t.envelope_from_event(1, StableTextEvent(kind="clear", text="oops", confidence=0.5, source_seq=7, timestamp_monotonic=1.0))
        self.assertEqual(env.text, "")
        self.assertIsNone(env.confidence)
        self.assertIsNone(env.source_seq)


if __name__ == "__main__":
    unittest.main(verbosity=2)
