#!/usr/bin/env python3
"""Phase 2N.3 tests: end-to-end OCR text-update latency instrumentation.

Covers the optional `captured_monotonic` transport field, the multi-region
latency sampler, stabilizer acceptance timing, and the manager/renderer
diagnostic payload. Instrumentation is runtime-only and must never change
behavior.

Run:
    python3 scripts/test_latency_instrumentation.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ocr import transport as t  # noqa: E402
from ocr.multi_region import MAX_LATENCY_SAMPLES, MultiRegionOCRCoordinator, RegionStableTextEvent  # noqa: E402
from ocr.stabilizer import OCRStabilizer, StableTextEvent  # noqa: E402
from capture.recognition_regions import RecognitionRegion  # noqa: E402
from overlay_manager import OverlayManager, OverlayState  # noqa: E402


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.t = float(value)

    def __call__(self) -> float:
        return self.t


class _FakeResult:
    def __init__(self, lines, sequence, elapsed_ms=12.5) -> None:
        self.lines = lines
        self.sequence = sequence
        self.elapsed_ms = elapsed_ms


class _FakeRuntime:
    def __init__(self, lines_factory) -> None:
        self._lines_factory = lines_factory
        self.calls = 0

    def recognize_rgba(self, crop, width, height, sequence=None):
        self.calls += 1
        return _FakeResult(self._lines_factory(), sequence)


def _lines(text: str):
    return [SimpleNamespace(text=text, confidence=0.95)]


def _rgba(width=20, height=20) -> bytes:
    return bytes(width * height * 4)


def _region(region_id="A"):
    return RecognitionRegion(region_id=region_id, x=0.1, y=0.1, w=0.2, h=0.2)


class TransportCapturedMonotonicTest(unittest.TestCase):
    def test_round_trip_with_captured(self) -> None:
        envelope = t.StableTextEnvelope(2, 1, "text", "hi", 0.9, 3, 10.0, "A", 9.5)
        line = t.encode_envelope(envelope)
        self.assertIn("captured_monotonic", line)
        decoded = t.decode_envelope(line)
        self.assertEqual(decoded.captured_monotonic, 9.5)

    def test_absent_is_backward_compatible(self) -> None:
        envelope = t.StableTextEnvelope(2, 1, "text", "hi", 0.9, 3, 10.0, "A")
        line = t.encode_envelope(envelope)
        self.assertNotIn("captured_monotonic", line)
        self.assertIsNone(t.decode_envelope(line).captured_monotonic)

    def test_invalid_captured_rejected(self) -> None:
        for bad in (-1.0, "x", float("nan"), float("inf"), True):
            payload = {
                "v": 2,
                "type": "stable_text",
                "event_seq": 1,
                "kind": "text",
                "text": "hi",
                "confidence": 0.9,
                "source_seq": 3,
                "timestamp_monotonic": 10.0,
                "region_id": "A",
                "captured_monotonic": bad,
            }
            with self.assertRaises(t.TransportError):
                t.decode_envelope(json.dumps(payload))

    def test_region_event_carries_captured(self) -> None:
        event = StableTextEvent("text", "hi", 0.9, 3, 10.0)
        region_event = RegionStableTextEvent(region_id="A", event=event, captured_monotonic=4.25)
        decoded = t.decode_envelope(t.encode_region_stable_text_event(1, region_event))
        self.assertEqual(decoded.captured_monotonic, 4.25)

    def test_clear_event_captured_allowed(self) -> None:
        envelope = t.StableTextEnvelope(2, 2, "clear", "", None, None, 11.0, "A", 9.0)
        decoded = t.decode_envelope(t.encode_envelope(envelope))
        self.assertEqual(decoded.kind, "clear")
        self.assertEqual(decoded.captured_monotonic, 9.0)


class StabilizerAcceptanceTest(unittest.TestCase):
    def test_first_candidate_timestamp(self) -> None:
        clock = _Clock(100.0)
        stabilizer = OCRStabilizer(clock=clock)
        stabilizer.observe(_lines("ABC"), 1, 100.0)
        clock.t = 101.0
        stabilizer.observe(_lines("ABC"), 2, 101.0)
        self.assertEqual(stabilizer.first_candidate_timestamp("ABC"), 100.0)
        self.assertIsNone(stabilizer.first_candidate_timestamp("NOPE"))


class MultiRegionLatencyTest(unittest.TestCase):
    def _coordinator(self, clock, lines_factory):
        return MultiRegionOCRCoordinator(_FakeRuntime(lines_factory), clock=clock)

    def test_changed_text_records_one_sample(self) -> None:
        clock = _Clock(1000.0)
        coordinator = self._coordinator(clock, lambda: _lines("ABC"))
        coordinator.process_decoded(_rgba(), 20, 20, 1, [_region()], captured_monotonic=1000.0, decode_ms=1.5)
        self.assertEqual(coordinator.drain_latency(), [])
        clock.t = 1001.0
        coordinator.process_decoded(_rgba(), 20, 20, 2, [_region()], captured_monotonic=1000.0, decode_ms=1.5)
        samples = coordinator.drain_latency()
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample["region_id"], "A")
        self.assertEqual(sample["frame_seq"], 2)
        self.assertEqual(sample["stabilizer_accept_ms"], 1000.0)
        self.assertEqual(sample["worker_total_ms"], 1000.0)
        for key in ("capture_age_at_ocr_start_ms", "decode_ms", "roi_ms", "ocr_ms"):
            self.assertIsNotNone(sample[key])
            self.assertGreaterEqual(sample[key], 0.0)
        # drained
        self.assertEqual(coordinator.drain_latency(), [])

    def test_duplicate_text_does_not_record(self) -> None:
        clock = _Clock(1000.0)
        coordinator = self._coordinator(clock, lambda: _lines("ABC"))
        coordinator.process_decoded(_rgba(), 20, 20, 1, [_region()], captured_monotonic=1000.0)
        clock.t = 1001.0
        coordinator.process_decoded(_rgba(), 20, 20, 2, [_region()], captured_monotonic=1000.0)
        coordinator.drain_latency()
        clock.t = 1002.0
        coordinator.process_decoded(_rgba(), 20, 20, 3, [_region()], captured_monotonic=1000.0)
        self.assertEqual(coordinator.drain_latency(), [])

    def test_clear_does_not_record(self) -> None:
        clock = _Clock(1000.0)
        coordinator = self._coordinator(clock, lambda: _lines("ABC"))
        coordinator.process_decoded(_rgba(), 20, 20, 1, [_region()], captured_monotonic=1000.0)
        clock.t = 1001.0
        coordinator.process_decoded(_rgba(), 20, 20, 2, [_region()], captured_monotonic=1000.0)
        coordinator.drain_latency()
        clock.t = 1004.0  # > stale timeout (2.0s) after last activity
        coordinator._runtime = _FakeRuntime(lambda: [])
        events = coordinator.process_decoded(_rgba(), 20, 20, 3, [_region()], captured_monotonic=1000.0)
        self.assertTrue(any(event.event.kind == "clear" for event in events))
        self.assertEqual(coordinator.drain_latency(), [])

    def test_missing_captured_is_backward_compatible(self) -> None:
        clock = _Clock(1000.0)
        coordinator = self._coordinator(clock, lambda: _lines("ABC"))
        coordinator.process_decoded(_rgba(), 20, 20, 1, [_region()])
        clock.t = 1001.0
        coordinator.process_decoded(_rgba(), 20, 20, 2, [_region()])
        sample = coordinator.drain_latency()[0]
        self.assertIsNone(sample["captured_monotonic"])
        self.assertIsNone(sample["capture_age_at_ocr_start_ms"])
        self.assertIsNone(sample["worker_total_ms"])

    def test_latency_samples_bounded(self) -> None:
        clock = _Clock(1000.0)
        text = {"value": "T0"}
        coordinator = self._coordinator(clock, lambda: _lines(text["value"]))
        for index in range(10):
            text["value"] = f"T{index}"
            coordinator.process_decoded(_rgba(), 20, 20, index * 2 + 1, [_region()], captured_monotonic=1000.0)
            clock.t += 0.1
            coordinator.process_decoded(_rgba(), 20, 20, index * 2 + 2, [_region()], captured_monotonic=1000.0)
            clock.t += 0.1
        self.assertLessEqual(len(coordinator.drain_latency()), MAX_LATENCY_SAMPLES)

    def test_no_negative_durations(self) -> None:
        clock = _Clock(1000.0)
        coordinator = self._coordinator(clock, lambda: _lines("ABC"))
        coordinator.process_decoded(_rgba(), 20, 20, 1, [_region()], captured_monotonic=999.0)
        clock.t = 1001.0
        coordinator.process_decoded(_rgba(), 20, 20, 2, [_region()], captured_monotonic=999.0)
        sample = coordinator.drain_latency()[0]
        for key, value in sample.items():
            if isinstance(value, (int, float)):
                self.assertGreaterEqual(value, 0.0, key)


class WorkerLatencyJournalTest(unittest.TestCase):
    def test_latency_stderr_line_is_mirrored(self) -> None:
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
                    b"[latency] region=A frame=2\n",
                    b"[stabilizer-audit] region=A frame=2 first_matches_final=1\n",
                    b"other line\n",
                ]
            )
        )
        manager._read_stderr()
        self.assertTrue(any("[latency]" in line for line in captured))
        self.assertTrue(any("[stabilizer-audit]" in line for line in captured))
        self.assertFalse(any("other line" in line for line in captured))


class ManagerDiagnosticPayloadTest(unittest.TestCase):
    def _manager(self):
        manager = OverlayManager()
        sent: list[dict] = []
        manager._send = lambda payload: sent.append(payload) or True  # type: ignore
        manager._state = OverlayState.RUNNING
        manager._text_enabled = True
        return manager, sent

    def test_optional_diagnostic_fields_included(self) -> None:
        async def run():
            manager, sent = self._manager()
            await manager.set_region_text(
                "A",
                (0.1, 0.1, 0.2, 0.2),
                "hi",
                source_seq=7,
                stable_text_monotonic=time.monotonic(),
                captured_monotonic=time.monotonic(),
            )
            payload = sent[-1]
            self.assertEqual(payload["type"], "set_region_text")
            self.assertEqual(payload["source_seq"], 7)
            self.assertIn("stable_text_monotonic", payload)
            self.assertIn("captured_monotonic", payload)

        asyncio.run(run())

    def test_optional_fields_absent_by_default(self) -> None:
        async def run():
            manager, sent = self._manager()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "hi")
            payload = sent[-1]
            self.assertNotIn("source_seq", payload)
            self.assertNotIn("stable_text_monotonic", payload)
            self.assertNotIn("captured_monotonic", payload)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
