#!/usr/bin/env python3
"""Phase 2K.2 tests: thread-safe main-loop overlay delivery.

Local only: no real renderer, no sockets. Uses fakes + real asyncio.

Run:
    python3 scripts/test_overlay_delivery.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SETTINGS_DIR = tempfile.mkdtemp(prefix="clarifydeck-delivery-settings-")
sys.modules.setdefault(
    "decky",
    types.SimpleNamespace(
        logger=logging.getLogger("clarifydeck-delivery-test"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_SETTINGS_DIR=_SETTINGS_DIR,
        DECKY_PLUGIN_DIR=".",
        emit=lambda *args, **kwargs: None,
    ),
)

import main  # noqa: E402
from backend.ocr_transport import AcceptedStableTextEvent, OCRTransportReceiver  # noqa: E402
from backend.overlay_delivery import MainLoopOverlayDelivery, OverlayDeliveryObserver  # noqa: E402
from backend.overlay_text import OverlayTextAction, OverlayTextCoordinator  # noqa: E402
from ocr.stabilizer import StableTextEvent  # noqa: E402
from ocr.transport import encode_envelope, envelope_from_event  # noqa: E402


def _text_line(seq, text="hello", confidence=0.9, source_seq=3, ts=1.0):
    event = StableTextEvent(kind="text", text=text, confidence=confidence, source_seq=source_seq, timestamp_monotonic=ts)
    return encode_envelope(envelope_from_event(seq, event))


def _clear_line(seq, ts=2.0):
    event = StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=ts)
    return encode_envelope(envelope_from_event(seq, event))


def make_action(session="A", seq=1, kind="text", text="hello") -> OverlayTextAction:
    return OverlayTextAction(
        action_seq=seq,
        kind=kind,
        worker_session_id=session,
        source_event_seq=seq,
        text=text,
    )


class FakeManager:
    def __init__(self, running=True, gate=None, fail=False):
        self.running = running
        self.gate = gate
        self.fail = fail
        self.updates: list = []
        self.hides = 0
        self.threads: list = []
        self.started = False
        self.concurrent = 0
        self.max_concurrent = 0

    def status(self):
        return {"enabled": self.running, "state": "RUNNING" if self.running else "DISABLED"}

    async def update(self, text):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.started = True
            self.threads.append(threading.get_ident())
            if self.gate is not None:
                await self.gate.wait()
            if self.fail:
                raise RuntimeError("update boom")
            self.updates.append(text)
        finally:
            self.concurrent -= 1

    async def hide(self):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.started = True
            self.threads.append(threading.get_ident())
            if self.gate is not None:
                await self.gate.wait()
            if self.fail:
                raise RuntimeError("hide boom")
            self.hides += 1
        finally:
            self.concurrent -= 1

    async def clear_all_region_text(self):
        pass

    async def set_region_text(self, region_id, rect, text):
        pass

    async def hide_region_text(self, region_id):
        pass


async def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class DeliveryPrimitiveTest(unittest.TestCase):
    def test_d1_submit_from_thread_delivers_on_loop(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            loop_thread = threading.get_ident()
            manager = FakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            producer_done = threading.Event()

            def producer():
                delivery.submit(make_action(text="from-thread"))
                producer_done.set()

            threading.Thread(target=producer, daemon=True).start()
            self.assertTrue(await wait_until(lambda: manager.updates == ["from-thread"]))
            self.assertTrue(producer_done.wait(1.0))
            self.assertTrue(all(tid == loop_thread for tid in manager.threads))

        asyncio.run(scenario())

    def test_d2_submit_is_non_blocking(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            gate = asyncio.Event()
            manager = FakeManager(gate=gate)
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            result = {}

            def producer():
                start = time.monotonic()
                delivery.submit(make_action(text="blocked"))
                result["elapsed"] = time.monotonic() - start

            thread = threading.Thread(target=producer, daemon=True)
            thread.start()
            self.assertTrue(await wait_until(lambda: manager.started))
            thread.join(1.0)
            self.assertIn("elapsed", result)
            self.assertLess(result["elapsed"], 0.1)
            self.assertEqual(manager.updates, [])
            gate.set()
            self.assertTrue(await wait_until(lambda: manager.updates == ["blocked"]))

        asyncio.run(scenario())

    def test_d3_text_calls_update_exact(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            text = "  lead\n中间 CJK! 尾  "
            delivery.submit(make_action(text=text))
            self.assertTrue(await wait_until(lambda: len(manager.updates) == 1))
            self.assertEqual(manager.updates, [text])

        asyncio.run(scenario())

    def test_d4_hide_calls_hide_once(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.submit(make_action(kind="hide", text=""))
            self.assertTrue(await wait_until(lambda: manager.hides == 1))
            self.assertEqual(manager.updates, [])

        asyncio.run(scenario())

    def test_d5_disabled_drops_without_creation(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            calls = {"n": 0}

            def accessor():
                calls["n"] += 1
                return None

            delivery = MainLoopOverlayDelivery(loop, accessor)
            delivery.submit(make_action(text="x"))
            self.assertTrue(await wait_until(lambda: delivery.status()["actions_dropped_disabled"] == 1))
            self.assertEqual(delivery.status()["actions_delivered"], 0)
            self.assertEqual(calls["n"], 1)  # peeked only; never created a manager

        asyncio.run(scenario())

    def test_d6_newest_wins_capacity_one(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            gate = asyncio.Event()
            manager = FakeManager(gate=gate)
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.submit(make_action(seq=1, text="A"))
            self.assertTrue(await wait_until(lambda: manager.started))
            delivery.submit(make_action(seq=2, text="B"))
            delivery.submit(make_action(seq=3, text="C"))
            gate.set()
            self.assertTrue(await wait_until(lambda: manager.updates and manager.updates[-1] == "C"))
            self.assertEqual(manager.updates, ["A", "C"])
            self.assertGreaterEqual(delivery.status()["actions_coalesced"], 1)

        asyncio.run(scenario())

    def test_d7_no_overlapping_manager_calls(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            gate = asyncio.Event()
            manager = FakeManager(gate=gate)
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.submit(make_action(seq=1, text="A"))
            self.assertTrue(await wait_until(lambda: manager.started))
            for index in range(2, 8):
                delivery.submit(make_action(seq=index, text=f"t{index}"))
            gate.set()
            self.assertTrue(await wait_until(lambda: delivery.status()["pending"] is False and manager.concurrent == 0))
            await asyncio.sleep(0.05)
            self.assertEqual(manager.max_concurrent, 1)

        asyncio.run(scenario())

    def test_d8_no_stranded_wakeup(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            for index in range(1, 101):
                delivery.submit(make_action(seq=index, text=f"t{index}"))
                await asyncio.sleep(0)
            self.assertTrue(await wait_until(lambda: manager.updates and manager.updates[-1] == "t100"))
            self.assertFalse(delivery.status()["pending"])

        asyncio.run(scenario())

    def test_d9_session_change_drops_pending_old_action(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            gate = asyncio.Event()
            manager = FakeManager(gate=gate)
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_session("A")
            delivery.submit(make_action(session="A", seq=1, text="A1"))
            self.assertTrue(await wait_until(lambda: manager.started))
            delivery.submit(make_action(session="A", seq=2, text="A2"))
            delivery.set_session("B")
            self.assertGreaterEqual(delivery.status()["actions_dropped_session"], 1)
            gate.set()
            self.assertTrue(await wait_until(lambda: manager.updates == ["A1"]))
            await asyncio.sleep(0.05)
            self.assertEqual(manager.updates, ["A1"])

        asyncio.run(scenario())

    def test_d10_new_session_action_delivers(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.set_session("B")
            delivery.submit(make_action(session="B", seq=1, text="B1"))
            self.assertTrue(await wait_until(lambda: manager.updates == ["B1"]))

        asyncio.run(scenario())

    def test_d11_delivery_exception_isolated(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager(fail=True)
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.submit(make_action(text="boom"))
            self.assertTrue(await wait_until(lambda: delivery.status()["delivery_errors"] == 1))
            manager.fail = False
            delivery.submit(make_action(seq=2, text="after"))
            self.assertTrue(await wait_until(lambda: manager.updates == ["after"]))

        asyncio.run(scenario())

    def test_d12_closed_delivery_drops(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            delivery = MainLoopOverlayDelivery(loop, lambda: manager)
            delivery.close()
            delivery.submit(make_action(text="x"))
            await asyncio.sleep(0.05)
            self.assertEqual(delivery.status()["actions_dropped_closed"], 1)
            self.assertFalse(delivery.status()["pending"])
            self.assertEqual(manager.updates, [])

        asyncio.run(scenario())

    def test_d13_loop_closed_scheduling_failure_isolated(self) -> None:
        loop = asyncio.new_event_loop()
        manager = FakeManager()
        delivery = MainLoopOverlayDelivery(loop, lambda: manager)
        loop.close()
        delivery.submit(make_action(text="x"))  # must not raise
        status = delivery.status()
        self.assertGreaterEqual(status["delivery_errors"], 1)
        self.assertIsNotNone(status["last_delivery_error"])


class ObserverIntegrationTest(unittest.TestCase):
    def _build(self, loop, manager):
        coordinator = OverlayTextCoordinator()
        delivery = MainLoopOverlayDelivery(loop, lambda: manager)
        observer = OverlayDeliveryObserver(coordinator, delivery)
        receiver = OCRTransportReceiver(observer=observer)
        return coordinator, delivery, receiver

    def test_i1_accepted_text_reaches_manager(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            _coordinator, _delivery, receiver = self._build(loop, manager)
            receiver.begin_session("s1")
            text = "  多行\ntext!  "
            self.assertTrue(receiver.handle_line(_text_line(1, text=text)))
            self.assertTrue(await wait_until(lambda: manager.updates == [text]))

        asyncio.run(scenario())

    def test_i2_clear_reaches_hide(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            _coordinator, _delivery, receiver = self._build(loop, manager)
            receiver.begin_session("s1")
            self.assertTrue(receiver.handle_line(_text_line(1, text="shown")))
            self.assertTrue(await wait_until(lambda: manager.updates == ["shown"]))
            self.assertTrue(receiver.handle_line(_clear_line(2)))
            self.assertTrue(await wait_until(lambda: manager.hides == 1))

        asyncio.run(scenario())

    def test_i3_duplicate_never_reaches_manager(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            _coordinator, _delivery, receiver = self._build(loop, manager)
            receiver.begin_session("s1")
            receiver.handle_line(_text_line(1, text="once"))
            receiver.handle_line(_text_line(1, text="dup"))
            self.assertTrue(await wait_until(lambda: manager.updates == ["once"]))
            await asyncio.sleep(0.05)
            self.assertEqual(manager.updates, ["once"])

        asyncio.run(scenario())

    def test_i4_malformed_never_reaches_manager(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            _coordinator, _delivery, receiver = self._build(loop, manager)
            receiver.begin_session("s1")
            self.assertFalse(receiver.handle_line("{not json"))
            await asyncio.sleep(0.05)
            self.assertEqual(manager.updates, [])
            self.assertEqual(manager.hides, 0)

        asyncio.run(scenario())

    def test_i5_delivery_failure_does_not_reject_ocr(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager(fail=True)
            _coordinator, delivery, receiver = self._build(loop, manager)
            receiver.begin_session("s1")
            accepted = receiver.handle_line(_text_line(1, text="kept"))
            self.assertTrue(accepted)
            self.assertEqual(receiver.state().text, "kept")
            self.assertTrue(await wait_until(lambda: delivery.status()["delivery_errors"] == 1))
            self.assertEqual(receiver.status()["transport_messages_rejected"], 0)

        asyncio.run(scenario())

    def test_i6_no_manager_does_not_affect_ocr(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            created = {"n": 0}

            def accessor():
                created["n"] += 1
                return None

            coordinator = OverlayTextCoordinator()
            delivery = MainLoopOverlayDelivery(loop, accessor)
            observer = OverlayDeliveryObserver(coordinator, delivery)
            receiver = OCRTransportReceiver(observer=observer)
            receiver.begin_session("s1")
            self.assertTrue(receiver.handle_line(_text_line(1, text="kept")))
            self.assertEqual(receiver.state().text, "kept")
            self.assertTrue(await wait_until(lambda: delivery.status()["actions_dropped_disabled"] == 1))
            # The accessor is a non-creating peek (also used by the session clear);
            # it must never construct a manager (always returns None here).
            self.assertGreaterEqual(created["n"], 1)

        asyncio.run(scenario())

    def test_i7_begin_session_resets_coordinator_and_delivery(self) -> None:
        async def scenario():
            loop = asyncio.get_running_loop()
            manager = FakeManager()
            coordinator, delivery, receiver = self._build(loop, manager)
            receiver.begin_session("A")
            receiver.handle_line(_text_line(1, text="a"))
            self.assertTrue(await wait_until(lambda: manager.updates == ["a"]))
            receiver.begin_session("B")
            self.assertEqual(coordinator.session_id, "B")
            self.assertEqual(delivery.status()["session_id"], "B")
            self.assertIsNone(coordinator.latest_action())

        asyncio.run(scenario())

    def test_i8_shared_receiver_remains_single_object(self) -> None:
        engine = main.ClarifyDeckEngine()
        engine._role = "leader"

        async def scenario():
            engine.init_overlay_delivery(asyncio.get_running_loop())

        asyncio.run(scenario())
        receiver = engine._transport_receiver()
        manager = engine._ocr_worker_manager()
        self.assertIs(manager._receiver, receiver)
        self.assertIs(engine._transport_receiver(), receiver)
        self.assertIs(receiver._observer, engine._overlay_observer)


class NoAutoStartTest(unittest.TestCase):
    def test_delivery_install_does_not_create_worker_or_renderer(self) -> None:
        engine = main.ClarifyDeckEngine()
        engine._role = "leader"

        async def scenario():
            engine.init_overlay_delivery(asyncio.get_running_loop())

        asyncio.run(scenario())
        self.assertIsNone(engine._ocr_worker)
        self.assertIsNone(engine._overlay)  # delivery never constructs a manager
        self.assertIsNotNone(engine._overlay_delivery)

    def test_ocr_event_while_overlay_disabled_starts_nothing(self) -> None:
        engine = main.ClarifyDeckEngine()
        engine._role = "leader"

        async def scenario():
            engine.init_overlay_delivery(asyncio.get_running_loop())
            receiver = engine._transport_receiver()
            receiver.begin_session("s1")
            receiver.handle_line(_text_line(1, text="no renderer"))
            await asyncio.sleep(0.05)
            self.assertIsNone(engine._overlay)
            self.assertGreaterEqual(engine._overlay_delivery.status()["actions_dropped_disabled"], 1)

        asyncio.run(scenario())

    def test_overlay_enable_does_not_start_ocr(self) -> None:
        engine = main.ClarifyDeckEngine()
        engine._role = "leader"
        self.assertIsNone(engine._overlay)
        result = asyncio.run(engine.enable_overlay())
        self.assertFalse(result.get("ok", True) and result.get("enabled", False))
        self.assertIsNone(engine._ocr_worker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
