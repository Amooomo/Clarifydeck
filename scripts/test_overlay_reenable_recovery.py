#!/usr/bin/env python3
"""Phase 2M.2D.1 tests: Persistent Overlay re-enable fresh-text recovery.

Proves the frozen invariant: Persistent Overlay may clear rendered text, but it
must never permanently poison future fresh Stable Text delivery, and the OCR
worker must resolve the same authoritative region source as the overlay (the
active Region Profile), not the frozen legacy recognition_roi.json.

Run:
    python3 scripts/test_overlay_reenable_recovery.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SETTINGS_DIR = tempfile.mkdtemp(prefix="clarifydeck-reenable-")
sys.modules.setdefault(
    "decky",
    SimpleNamespace(
        logger=logging.getLogger("clarifydeck-reenable-test"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_SETTINGS_DIR=_SETTINGS_DIR,
        DECKY_PLUGIN_DIR=".",
        emit=lambda *args, **kwargs: None,
    ),
)

import main  # noqa: E402
from backend.ocr_transport import AcceptedStableTextEvent  # noqa: E402
from backend.overlay_delivery import MainLoopOverlayDelivery, OverlayDeliveryObserver  # noqa: E402
from backend.overlay_text import OverlayTextCoordinator  # noqa: E402
from capture import recognition_regions as rr  # noqa: E402
from capture import recognition_roi  # noqa: E402
from overlay_manager import OverlayManager, OverlayState  # noqa: E402


class _FakeProc:
    pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _event(session, seq, region, text, kind="text"):
    return AcceptedStableTextEvent(
        worker_session_id=session,
        event_seq=seq,
        kind=kind,
        text=text,
        confidence=0.9,
        source_seq=seq,
        timestamp_monotonic=float(seq),
        region_id=region,
        transport_version=2,
    )


class _Harness:
    def __init__(self, layout, *, style=None, font_size=None):
        self.manager = OverlayManager()
        self.sent: list[dict] = []
        self.manager._send = lambda payload: self.sent.append(payload) or True  # type: ignore
        self.manager._state = OverlayState.RUNNING
        self.manager._proc = _FakeProc()
        self.manager._sock = object()
        self.manager._text_enabled = True
        self.manager._preview_enabled = True
        self.manager._preview_regions = [{"region_id": "A", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "selected": False, "primary": True, "enabled": True, "label": "Region 1"}]
        if style is not None:
            self.manager._region_style["A"] = style
        if font_size is not None:
            self.manager._region_font_size["A"] = font_size
        loop = asyncio.get_running_loop()
        self.delivery = MainLoopOverlayDelivery(loop, lambda: self.manager)
        self.coordinator = OverlayTextCoordinator()
        self.observer = OverlayDeliveryObserver(self.coordinator, self.delivery)
        self.delivery.set_region_layout(layout)

    def types(self):
        return [payload.get("type") for payload in self.sent]

    def sent_regions(self):
        return [payload["region_id"] for payload in self.sent if payload.get("type") == "set_region_text"]


class ReenableRecoveryTest(unittest.TestCase):
    def _run(self, coro):
        asyncio.run(coro)

    def test_t1_baseline_render(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            self.assertEqual(h.manager._region_text["A"]["text"], "ABC")
            self.assertIn("set_region_text", h.types())

        self._run(run())

    def test_t2_disable_clears_text(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            await h.manager.disable()
            self.assertEqual(h.manager._region_text, {})
            self.assertIn("clear_all_region_text", h.types())

        self._run(run())

    def test_t3_reenable_no_replay(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            await h.manager.disable()
            h.sent.clear()
            await h.manager.enable()
            self.assertEqual(h.manager._region_text, {})
            self.assertNotIn("set_region_text", h.types())

        self._run(run())

    def test_t4_restart_same_text_renders(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            await h.manager.disable()
            await h.manager.enable()
            h.observer.begin_session("s2")
            h.observer.on_accepted_event(_event("s2", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            self.assertEqual(h.manager._region_text["A"]["text"], "ABC")
            self.assertIn("A", h.sent_regions())

        self._run(run())

    def test_t5_restart_changed_text_renders(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            await h.manager.disable()
            await h.manager.enable()
            h.observer.begin_session("s2")
            h.observer.on_accepted_event(_event("s2", 1, "A", "XYZ"))
            await asyncio.sleep(0.02)
            self.assertEqual(h.manager._region_text["A"]["text"], "XYZ")

        self._run(run())

    def test_t6_two_regions_after_restart(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2), "B": (0.5, 0.5, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "AAA"))
            h.observer.on_accepted_event(_event("s1", 2, "B", "BBB"))
            await asyncio.sleep(0.02)
            await h.manager.disable()
            await h.manager.enable()
            h.observer.begin_session("s2")
            h.observer.on_accepted_event(_event("s2", 1, "A", "AAA"))
            h.observer.on_accepted_event(_event("s2", 2, "B", "CCC"))
            await asyncio.sleep(0.02)
            self.assertEqual(h.manager._region_text["A"]["text"], "AAA")
            self.assertEqual(h.manager._region_text["B"]["text"], "CCC")

        self._run(run())

    def test_t7_preview_remains(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            await h.manager.disable()
            await h.manager.enable()
            self.assertTrue(h.manager._preview_enabled)
            self.assertEqual(len(h.manager._preview_regions), 1)
            self.assertIn("set_region_preview", h.types())

        self._run(run())

    def test_t8_saved_appearance_applies_to_fresh_text(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)}, style="black_on_white", font_size=30)
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            self.assertEqual(h.manager._region_text["A"]["style"], "black_on_white")
            self.assertEqual(h.manager._region_text["A"]["font_size"], 30)

        self._run(run())

    def test_t9_overlay_enable_does_not_start_ocr(self) -> None:
        async def run():
            engine = main.ClarifyDeckEngine()
            engine._role = "leader"

            class _FakeOverlay:
                async def enable(self):
                    return {"enabled": True, "state": "RUNNING"}

            engine._overlay = _FakeOverlay()
            await engine.enable_overlay()
            self.assertIsNone(engine._ocr_worker)

        self._run(run())

    def test_t10_reenable_alone_no_old_replay(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            await h.manager.disable()
            h.sent.clear()
            await h.manager.enable()
            self.assertEqual(h.manager._region_text, {})
            self.assertEqual(h.sent_regions(), [])

        self._run(run())

    def test_same_session_duplicate_protection_preserved(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            self.assertEqual(h.coordinator.status()["inputs_rejected"], 1)
            self.assertEqual(h.coordinator.status()["last_error"], "stale_or_duplicate")

        self._run(run())

    def test_new_session_same_string_is_eligible(self) -> None:
        async def run():
            h = _Harness({"A": (0.1, 0.1, 0.2, 0.2)})
            h.observer.begin_session("s1")
            h.observer.on_accepted_event(_event("s1", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            h.observer.begin_session("s2")
            h.observer.on_accepted_event(_event("s2", 1, "A", "ABC"))
            await asyncio.sleep(0.02)
            self.assertEqual(h.coordinator.status()["inputs_rejected"], 0)
            self.assertEqual(h.manager._region_text["A"]["text"], "ABC")

        self._run(run())


class RegionSourceUnificationTest(unittest.TestCase):
    """Phase 2M.2D.1: worker and overlay must share the active Region Profile."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = main.ClarifyDeckEngine()
        self.engine.roi_config_path = lambda: Path(self.tmp.name) / "recognition_roi.json"  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ocr_start_uses_active_profile_region_source(self) -> None:
        self.engine.region_config_set(
            [{"region_id": "fresh-region", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "enabled": True, "name": None}],
            None,
        )
        active = self.engine.active_region_config_path()
        self.assertIsNotNone(active)
        self.assertEqual(active.parent.name, "region_profiles")
        self.assertNotEqual(active.name, "recognition_roi.json")

        calls: list[dict] = []

        class _FakeWorker:
            def start(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "state": "RUNNING"}

        self.engine._role = "leader"
        self.engine._ocr_worker = _FakeWorker()
        self.engine.start_ocr_worker()
        self.assertEqual(calls[0]["roi_config"], str(active))

    def test_worker_region_resolution_reads_profile_ids(self) -> None:
        self.engine.region_config_set(
            [{"region_id": "fresh-region", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "enabled": True, "name": None}],
            None,
        )
        active = self.engine.active_region_config_path()
        # Reproduce the worker's resolution path against the profile file.
        recognition_roi.configure(active)
        store = rr.RegionConfigStore(recognition_roi.get_store().path)
        regions = rr.RegionResolver(store).resolve_effective_regions(None).regions
        self.assertEqual([region.region_id for region in regions], ["fresh-region"])
        # The overlay layout resolves the same ids from the same file.
        self.assertEqual(list(self.engine._resolve_region_layout().keys()), ["fresh-region"])

    def test_legacy_file_is_not_the_worker_source(self) -> None:
        self.engine.region_config_set(
            [{"region_id": "profile-only", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "enabled": True, "name": None}],
            None,
        )
        active = self.engine.active_region_config_path()
        legacy = self.engine.roi_config_path()
        self.assertNotEqual(active, legacy)
        # The profile edit never writes the legacy file (no dual-write).
        self.assertFalse(legacy.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
