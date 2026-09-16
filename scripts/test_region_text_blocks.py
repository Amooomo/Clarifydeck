#!/usr/bin/env python3
"""Phase 2M.1 tests: per-region persistent OCR text blocks.

Covers per-region pending coalescing, observer session mode, protocol text
wrapping/clipping, and the integration stream (v2 authoritative, v1 ignored).

Run:
    python3 scripts/test_region_text_blocks.py
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ocr.transport as t  # noqa: E402
from backend.ocr_transport import AcceptedStableTextEvent, OCRTransportReceiver  # noqa: E402
from backend.overlay_delivery import (  # noqa: E402
    MODE_LEGACY_V1,
    MODE_REGION_V2,
    MainLoopOverlayDelivery,
    OverlayDeliveryObserver,
)
from backend.overlay_text import OverlayTextAction, OverlayTextCoordinator  # noqa: E402
from ocr.multi_region import RegionStableTextEvent  # noqa: E402
from ocr.stabilizer import StableTextEvent  # noqa: E402
from overlay import protocol  # noqa: E402


def action(region_id, text, *, kind="text", session="s1", seq=1, rect=None):
    return OverlayTextAction(
        action_seq=seq,
        kind=kind,
        worker_session_id=session,
        source_event_seq=seq,
        text=text,
        region_id=region_id,
    )


class RegionFakeManager:
    def __init__(self):
        self.region_texts = {}
        self.region_hides = []
        self.clears = 0
        self.updates = []
        self.hides = 0
        self.enabled = True

    def status(self):
        return {"enabled": self.enabled, "preview_enabled": False}

    async def update(self, text):
        self.updates.append(text)

    async def hide(self):
        self.hides += 1

    async def set_region_text(self, region_id, rect, text):
        self.region_texts[region_id] = text

    async def hide_region_text(self, region_id):
        self.region_hides.append(region_id)
        self.region_texts.pop(region_id, None)

    async def clear_all_region_text(self):
        self.clears += 1
        self.region_texts.clear()


async def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


LAYOUT = {"A": (0.1, 0.1, 0.2, 0.2), "B": (0.5, 0.5, 0.2, 0.2)}


class PerRegionDeliveryTest(unittest.TestCase):
    def test_d1_two_regions_queued_both_survive(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = RegionFakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_session("s1")
            delivery.set_region_layout(LAYOUT)
            delivery.submit_region(action("A", "A-text"))
            delivery.submit_region(action("B", "B-text"))
            self.assertTrue(await wait_until(lambda: len(manager.region_texts) == 2))
            self.assertEqual(manager.region_texts, {"A": "A-text", "B": "B-text"})

        asyncio.run(scenario())

    def test_d2_same_region_latest_wins(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = RegionFakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_session("s1")
            delivery.set_region_layout(LAYOUT)
            delivery.submit_region(action("A", "old"))
            delivery.submit_region(action("A", "new"))
            self.assertTrue(await wait_until(lambda: manager.region_texts.get("A") == "new"))
            self.assertEqual(manager.region_texts, {"A": "new"})
            self.assertGreaterEqual(delivery.status()["actions_coalesced"], 1)

        asyncio.run(scenario())

    def test_d3_clear_a_and_text_b(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = RegionFakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_session("s1")
            delivery.set_region_layout(LAYOUT)
            delivery.submit_region(action("A", "", kind="hide"))
            delivery.submit_region(action("B", "B-text"))
            self.assertTrue(await wait_until(lambda: manager.region_hides == ["A"] and manager.region_texts.get("B") == "B-text"))

        asyncio.run(scenario())

    def test_d4_session_switch_drops_old_pending(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = RegionFakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_session("s1")
            delivery.set_region_layout(LAYOUT)
            delivery.submit_region(action("A", "old", session="s1"))
            delivery.set_session("s2")
            await asyncio.sleep(0.05)
            self.assertEqual(manager.region_texts, {})
            delivery.submit_region(action("A", "new", session="s2"))
            self.assertTrue(await wait_until(lambda: manager.region_texts.get("A") == "new"))

        asyncio.run(scenario())

    def test_d5_close_idempotent(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = RegionFakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_region_layout(LAYOUT)
            delivery.close()
            delivery.close()
            delivery.submit_region(action("A", "x"))
            await asyncio.sleep(0.05)
            self.assertEqual(delivery.status()["actions_dropped_closed"], 1)
            self.assertEqual(manager.region_texts, {})

        asyncio.run(scenario())

    def test_d6_loop_closed_isolated(self) -> None:
        loop = asyncio.new_event_loop()
        manager = RegionFakeManager()
        delivery = MainLoopOverlayDelivery(loop, lambda: manager)
        delivery.set_region_layout(LAYOUT)
        loop.close()
        delivery.submit_region(action("A", "x"))  # must not raise
        self.assertGreaterEqual(delivery.status()["delivery_errors"], 1)

    def test_unknown_region_not_rendered(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = RegionFakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_session("s1")
            delivery.set_region_layout(LAYOUT)
            delivery.submit_region(action("Z", "nope"))
            await asyncio.sleep(0.05)
            self.assertEqual(manager.region_texts, {})
            self.assertGreaterEqual(delivery.status()["actions_dropped_unknown_region"], 1)

        asyncio.run(scenario())


class ProtocolTextTest(unittest.TestCase):
    def test_r6_geometry_maps(self) -> None:
        rect = protocol.preview_pixel_rect({"x": 0.23, "y": 0.80, "w": 0.20, "h": 0.12}, 1280, 800)
        self.assertAlmostEqual(rect[0], 294.4)
        self.assertAlmostEqual(rect[1], 640.0)
        self.assertAlmostEqual(rect[2], 256.0)
        self.assertAlmostEqual(rect[3], 96.0)

    def test_r7_wraps_within_width(self) -> None:
        lines = protocol.wrap_text("abcdefgh", 3, lambda s: len(s))
        self.assertTrue(all(len(line) <= 3 for line in lines))
        self.assertEqual("".join(lines), "abcdefgh")

    def test_r8_vertical_clip(self) -> None:
        lines = protocol.clip_lines(["a", "b", "c", "d"], 10, 25)
        self.assertEqual(lines, ["a", "b"])

    def test_r9_no_paint_beyond_width(self) -> None:
        text = "a" * 100
        lines = protocol.wrap_text(text, 12, lambda s: len(s))
        self.assertTrue(all(len(line) * 1 <= 12 for line in lines))

    def test_r10_unicode_newlines_preserved(self) -> None:
        lines = protocol.wrap_text("第一行\n第二行", 100, lambda s: len(s))
        self.assertEqual(lines, ["第一行", "第二行"])

    def test_sanitize_region_text(self) -> None:
        block = protocol.sanitize_region_text("A", {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1}, "hi")
        self.assertEqual(block["text"], "hi")
        self.assertIsNone(protocol.sanitize_region_text("", {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1}, "hi"))
        self.assertIsNone(protocol.sanitize_region_text("A", {"x": 5, "y": 0.2, "w": 0.3, "h": 0.1}, "hi"))


class SpyDelivery:
    def __init__(self):
        self.sessions = []
        self.submitted = []
        self.region_submitted = []

    def set_session(self, session_id):
        self.sessions.append(session_id)

    def submit(self, action):
        self.submitted.append(action)

    def submit_region(self, action):
        self.region_submitted.append(action)

    def clear_region_pending(self):
        pass

    def schedule_clear_region_text(self):
        pass


def _region_line(seq, region_id, event):
    return t.encode_region_stable_text_event(seq, RegionStableTextEvent(region_id=region_id, event=event))


def _text(text):
    return StableTextEvent(kind="text", text=text, confidence=0.9, source_seq=1, timestamp_monotonic=1.0)


def _clear():
    return StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=2.0)


class ObserverModeTest(unittest.TestCase):
    def _receiver(self):
        delivery = SpyDelivery()
        coordinator = OverlayTextCoordinator()
        observer = OverlayDeliveryObserver(coordinator, delivery)
        receiver = OCRTransportReceiver(session_id="s1", observer=observer)
        receiver.begin_session("s1")
        return receiver, delivery, observer

    def test_o1_first_v2_sets_region_mode(self) -> None:
        receiver, delivery, observer = self._receiver()
        receiver.handle_line(_region_line(1, "A", _text("hi")))
        self.assertEqual(observer.mode, MODE_REGION_V2)
        self.assertEqual([a.region_id for a in delivery.region_submitted], ["A"])

    def test_o2_paired_v1_ignored_by_overlay(self) -> None:
        receiver, delivery, observer = self._receiver()
        receiver.handle_line(_region_line(1, "A", _text("hi")))
        receiver.handle_line(t.encode_envelope(t.envelope_from_event(2, _text("hi"))))
        self.assertEqual(delivery.submitted, [])  # no legacy duplicate
        self.assertEqual(len(delivery.region_submitted), 1)

    def test_o3_secondary_v2_region_action(self) -> None:
        receiver, delivery, _ = self._receiver()
        receiver.handle_line(_region_line(1, "B", _text("sec")))
        self.assertEqual([a.region_id for a in delivery.region_submitted], ["B"])

    def test_o4_v1_still_updates_backend_legacy_state(self) -> None:
        receiver, _delivery, _ = self._receiver()
        receiver.handle_line(_region_line(1, "A", _text("region")))
        receiver.handle_line(t.encode_envelope(t.envelope_from_event(2, _text("legacy"))))
        self.assertEqual(receiver.state().text, "legacy")
        self.assertEqual(receiver.latest_stable_text_by_region("A")["text"], "region")

    def test_o5_v1_only_session_uses_legacy_block(self) -> None:
        receiver, delivery, observer = self._receiver()
        receiver.handle_line(t.encode_envelope(t.envelope_from_event(1, _text("legacy"))))
        self.assertEqual(observer.mode, MODE_LEGACY_V1)
        self.assertEqual(len(delivery.submitted), 1)
        self.assertEqual(delivery.submitted[0].text, "legacy")

    def test_o6_rejected_never_reaches_observer(self) -> None:
        receiver, delivery, _ = self._receiver()
        receiver.handle_line("{not json")
        receiver.handle_line(_region_line(1, "A", _text("hi")))
        receiver.handle_line(_region_line(1, "A", _text("dup")))
        self.assertEqual(len(delivery.region_submitted), 1)


class IntegrationStreamTest(unittest.TestCase):
    def test_representative_stream_no_duplicate_primary(self) -> None:
        delivery = SpyDelivery()
        coordinator = OverlayTextCoordinator()
        observer = OverlayDeliveryObserver(coordinator, delivery)
        receiver = OCRTransportReceiver(session_id="s1", observer=observer)
        receiver.begin_session("s1")

        stream = [
            _region_line(1, "A", _text("A1")),
            t.encode_envelope(t.envelope_from_event(2, _text("A1"))),  # v1 compatibility
            _region_line(3, "B", _text("B1")),
            _region_line(4, "A", _clear()),
            _region_line(5, "B", _text("B2")),
        ]
        for line_json in stream:
            self.assertTrue(receiver.handle_line(line_json))

        self.assertEqual(delivery.submitted, [])  # no legacy single-block action
        self.assertEqual(
            [(a.region_id, a.kind, a.text) for a in delivery.region_submitted],
            [("A", "text", "A1"), ("B", "text", "B1"), ("A", "hide", ""), ("B", "text", "B2")],
        )
        self.assertEqual(receiver.status()["transport_messages_rejected"], 0)
        self.assertEqual(receiver.status()["transport_out_of_order"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
