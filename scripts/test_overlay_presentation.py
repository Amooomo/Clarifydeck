#!/usr/bin/env python3
"""Phase 2M.2D tests: per-region overlay presentation persistence (style + font).

Covers the presentation store (schema validation, load/save, atomic write,
corrupt/unsupported handling, orphan preservation) and the OverlayManager /
engine integration (restore on init, explicit save, no OCR/renderer lifecycle).

Run:
    python3 scripts/test_overlay_presentation.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SETTINGS_DIR = tempfile.mkdtemp(prefix="clarifydeck-presentation-")
sys.modules.setdefault(
    "decky",
    SimpleNamespace(
        logger=logging.getLogger("clarifydeck-presentation-test"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_SETTINGS_DIR=_SETTINGS_DIR,
        DECKY_PLUGIN_DIR=".",
        emit=lambda *args, **kwargs: None,
    ),
)

import main  # noqa: E402
from overlay import presentation as pres  # noqa: E402
from overlay import protocol  # noqa: E402
from overlay_manager import OverlayManager  # noqa: E402


def _doc(regions) -> str:
    return json.dumps({"version": 1, "regions": regions})


class PresentationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "overlay_presentation.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_p1_missing_file_defaults_no_create(self) -> None:
        store = pres.PresentationStore(self.path)
        self.assertEqual(store.entries(), {})
        self.assertIsNone(store.last_error)
        self.assertFalse(self.path.exists())

    def test_p2_valid_load_two_regions(self) -> None:
        self.path.write_text(
            _doc(
                {
                    "A": {"style": "white_on_black", "font_size": 16},
                    "B": {"style": "black_on_white", "font_size": 30},
                }
            ),
            encoding="utf-8",
        )
        store = pres.PresentationStore(self.path)
        self.assertEqual(
            store.get("A"),
            {"style": "white_on_black", "font_size": 16, "panel_opacity": 0.65},
        )
        self.assertEqual(
            store.get("B"),
            {"style": "black_on_white", "font_size": 30, "panel_opacity": 0.65},
        )

    def test_p3_style_round_trip(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_WHITE_ON_BLACK, 20)
        store.update("B", protocol.STYLE_BLACK_ON_WHITE, 20)
        store.save()
        reloaded = pres.PresentationStore(self.path)
        self.assertEqual(reloaded.get("A")["style"], "white_on_black")
        self.assertEqual(reloaded.get("B")["style"], "black_on_white")

    def test_p4_font_round_trip(self) -> None:
        store = pres.PresentationStore(self.path)
        sizes = {"a": 14, "b": 20, "c": 30, "d": 48}
        for rid, size in sizes.items():
            store.update(rid, protocol.STYLE_WHITE_ON_BLACK, size)
        store.save()
        reloaded = pres.PresentationStore(self.path)
        for rid, size in sizes.items():
            self.assertEqual(reloaded.get(rid)["font_size"], size)

    def test_p5_save_selected_updates_entry(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_BLACK_ON_WHITE, 32)
        store.save()
        self.assertEqual(
            pres.PresentationStore(self.path).get("A"),
            {"style": "black_on_white", "font_size": 32, "panel_opacity": 0.65},
        )

    def test_p6_save_a_preserves_b(self) -> None:
        self.path.write_text(
            _doc({"A": {"style": "white_on_black", "font_size": 16}, "B": {"style": "black_on_white", "font_size": 30}}),
            encoding="utf-8",
        )
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_BLACK_ON_WHITE, 40)
        store.save()
        reloaded = pres.PresentationStore(self.path)
        self.assertEqual(
            reloaded.get("A"),
            {"style": "black_on_white", "font_size": 40, "panel_opacity": 0.65},
        )
        self.assertEqual(
            reloaded.get("B"),
            {"style": "black_on_white", "font_size": 30, "panel_opacity": 0.65},
        )

    def test_p7_orphan_entry_preserved(self) -> None:
        self.path.write_text(
            _doc({"ORPHAN": {"style": "black_on_white", "font_size": 44}}),
            encoding="utf-8",
        )
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_WHITE_ON_BLACK, 20)
        store.save()
        reloaded = pres.PresentationStore(self.path)
        self.assertEqual(
            reloaded.get("ORPHAN"),
            {"style": "black_on_white", "font_size": 44, "panel_opacity": 0.65},
        )
        self.assertEqual(
            reloaded.get("A"),
            {"style": "white_on_black", "font_size": 20, "panel_opacity": 0.65},
        )

    def test_p9_corrupt_json_safe_and_preserved(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        store = pres.PresentationStore(self.path)
        self.assertEqual(store.entries(), {})
        self.assertEqual(store.last_error, "invalid_json")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_p10_unsupported_version(self) -> None:
        self.path.write_text(json.dumps({"version": 99, "regions": {}}), encoding="utf-8")
        store = pres.PresentationStore(self.path)
        self.assertEqual(store.entries(), {})
        self.assertTrue(store.last_error.startswith("unsupported_version"))

    def test_p11_invalid_style_ignored(self) -> None:
        self.path.write_text(
            _doc({"A": {"style": "purple", "font_size": 20}}), encoding="utf-8"
        )
        self.assertIsNone(pres.PresentationStore(self.path).get("A"))

    def test_p12_invalid_font_ignored(self) -> None:
        self.path.write_text(
            _doc({"A": {"style": "white_on_black", "font_size": -5}}), encoding="utf-8"
        )
        self.assertIsNone(pres.PresentationStore(self.path).get("A"))

    def test_p13_mixed_valid_invalid(self) -> None:
        self.path.write_text(
            _doc(
                {
                    "GOOD": {"style": "white_on_black", "font_size": 20},
                    "BAD": {"style": "PURPLE", "font_size": -5},
                }
            ),
            encoding="utf-8",
        )
        store = pres.PresentationStore(self.path)
        self.assertEqual(
            store.get("GOOD"),
            {"style": "white_on_black", "font_size": 20, "panel_opacity": 0.65},
        )
        self.assertIsNone(store.get("BAD"))

    def test_p14_atomic_write_no_temp(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_WHITE_ON_BLACK, 20)
        store.save()
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        json.loads(self.path.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_p15_permissions(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_WHITE_ON_BLACK, 20)
        store.save()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


class ManagerPresentationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "overlay_presentation.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _manager(self) -> OverlayManager:
        return OverlayManager(presentation_path=self.path)

    def test_p16_unsaved_runtime_edit_does_not_write(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_style("A", "black_on_white"))
        asyncio.run(manager.set_region_font_size("A", 30))
        self.assertFalse(self.path.exists())

    def test_p17_restore_on_init(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_BLACK_ON_WHITE, 30)
        store.save()
        manager = self._manager()
        self.assertEqual(asyncio.run(manager.get_region_panel_style("A"))["style"], "black_on_white")
        self.assertEqual(asyncio.run(manager.get_region_font_size("A"))["font_size"], 30)

    def test_p18_new_region_gets_defaults(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("OLD_ID", protocol.STYLE_BLACK_ON_WHITE, 30)
        store.save()
        manager = self._manager()
        self.assertEqual(asyncio.run(manager.get_region_panel_style("NEW_ID"))["style"], "white_on_black")
        self.assertEqual(asyncio.run(manager.get_region_font_size("NEW_ID"))["font_size"], 20)

    def test_p19_cross_profile_restore(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_WHITE_ON_BLACK, 16)
        store.update("B", protocol.STYLE_BLACK_ON_WHITE, 30)
        store.save()
        manager = self._manager()
        self.assertEqual(asyncio.run(manager.get_region_panel_style("A"))["style"], "white_on_black")
        self.assertEqual(asyncio.run(manager.get_region_font_size("A"))["font_size"], 16)
        self.assertEqual(asyncio.run(manager.get_region_panel_style("B"))["style"], "black_on_white")
        self.assertEqual(asyncio.run(manager.get_region_font_size("B"))["font_size"], 30)

    def test_save_selected_only(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_style("A", "black_on_white"))
        asyncio.run(manager.set_region_font_size("A", 30))
        asyncio.run(manager.set_region_panel_style("B", "white_on_black"))
        asyncio.run(manager.set_region_font_size("B", 16))
        result = asyncio.run(manager.save_region_appearance("A"))
        self.assertTrue(result["ok"])
        reloaded = pres.PresentationStore(self.path)
        self.assertEqual(
            reloaded.get("A"),
            {"style": "black_on_white", "font_size": 30, "panel_opacity": 0.65},
        )
        self.assertIsNone(reloaded.get("B"))

    def test_save_preserves_other_entries(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("B", protocol.STYLE_BLACK_ON_WHITE, 30)
        store.save()
        manager = self._manager()
        asyncio.run(manager.set_region_font_size("A", 16))
        asyncio.run(manager.save_region_appearance("A"))
        reloaded = pres.PresentationStore(self.path)
        self.assertEqual(
            reloaded.get("B"),
            {"style": "black_on_white", "font_size": 30, "panel_opacity": 0.65},
        )
        self.assertEqual(reloaded.get("A")["font_size"], 16)

    def test_save_without_path_unavailable(self) -> None:
        manager = OverlayManager()  # no presentation_path
        result = asyncio.run(manager.save_region_appearance("A"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "presentation_unavailable")

    def test_save_invalid_region_id(self) -> None:
        manager = self._manager()
        result = asyncio.run(manager.save_region_appearance(""))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_region_id")

    def test_restored_style_used_by_set_region_text(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_BLACK_ON_WHITE, 30)
        store.save()
        manager = self._manager()
        asyncio.run(manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "hi"))
        self.assertEqual(manager._region_text["A"]["style"], "black_on_white")
        self.assertEqual(manager._region_text["A"]["font_size"], 30)

    def test_load_and_save_do_not_start_renderer(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_BLACK_ON_WHITE, 30)
        store.save()
        manager = self._manager()
        self.assertEqual(manager.status()["state"], "DISABLED")
        self.assertIsNone(manager._proc)
        self.assertIsNone(manager._sock)
        asyncio.run(manager.set_region_font_size("A", 30))
        asyncio.run(manager.save_region_appearance("A"))
        self.assertEqual(manager.status()["state"], "DISABLED")
        self.assertIsNone(manager._proc)

    def test_save_failure_surfaced(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_font_size("A", 30))
        # Make the target path a directory so os.replace fails.
        self.path.mkdir()
        result = asyncio.run(manager.save_region_appearance("A"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "presentation_write_failed")


class EngineAppearanceRpcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = main.ClarifyDeckEngine()
        self.engine.roi_config_path = lambda: Path(self.tmp.name) / "recognition_roi.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_presentation_path_separate(self) -> None:
        self.assertEqual(self.engine.presentation_path().name, "overlay_presentation.json")
        self.assertNotEqual(self.engine.presentation_path(), self.engine.roi_config_path())
        self.assertNotEqual(self.engine.presentation_path().parent, self.engine.region_profiles_path())

    def test_engine_save_rpc(self) -> None:
        self.engine._role = "leader"
        self.engine._overlay = OverlayManager(presentation_path=self.engine.presentation_path())
        asyncio.run(self.engine.region_panel_style_set("A", "black_on_white"))
        asyncio.run(self.engine.region_font_size_set("A", 30))
        result = asyncio.run(self.engine.region_appearance_save("A"))
        self.assertTrue(result["ok"])
        self.assertTrue(self.engine.presentation_path().is_file())
        payload = json.loads(self.engine.presentation_path().read_text(encoding="utf-8"))
        self.assertEqual(
            payload["regions"]["A"],
            {"style": "black_on_white", "font_size": 30, "panel_opacity": 0.65},
        )

    def test_engine_save_without_overlay(self) -> None:
        result = asyncio.run(self.engine.region_appearance_save("A"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "overlay_unavailable")

    def test_engine_load_restores_without_starting_ocr(self) -> None:
        store = pres.PresentationStore(self.engine.presentation_path())
        store.update("A", protocol.STYLE_BLACK_ON_WHITE, 30)
        store.save()
        self.engine._role = "leader"
        self.engine._overlay = OverlayManager(presentation_path=self.engine.presentation_path())
        self.assertEqual(
            asyncio.run(self.engine.region_panel_style_get("A"))["style"], "black_on_white"
        )
        self.assertIsNone(self.engine._ocr_worker)


class PanelOpacitySemanticsTest(unittest.TestCase):
    """Phase 2P.1I: panel opacity == background alpha; 0.0 is valid."""

    def test_default_is_legacy_panel_alpha(self) -> None:
        self.assertEqual(protocol.DEFAULT_PANEL_OPACITY, 0.65)
        self.assertEqual(protocol.DEFAULT_PANEL_OPACITY, protocol.PANEL_ALPHA)

    def test_sanitize_valid_percentages(self) -> None:
        for value, expected in (
            (0.0, 0.0),
            (0.05, 0.05),
            (0.25, 0.25),
            (0.5, 0.5),
            (0.65, 0.65),
            (1.0, 1.0),
            (0, 0.0),
            (1, 1.0),
        ):
            self.assertEqual(protocol.sanitize_panel_opacity(value), expected, value)

    def test_zero_is_not_missing(self) -> None:
        self.assertEqual(protocol.sanitize_panel_opacity(0.0), 0.0)
        self.assertIsNotNone(protocol.sanitize_panel_opacity(0.0))

    def test_malformed_returns_none(self) -> None:
        for bad in (None, "", "0.5", True, False, [], {}, float("nan"), float("inf")):
            self.assertIsNone(protocol.sanitize_panel_opacity(bad), bad)

    def test_out_of_range_clamped(self) -> None:
        self.assertEqual(protocol.sanitize_panel_opacity(-1.0), 0.0)
        self.assertEqual(protocol.sanitize_panel_opacity(2.0), 1.0)

    def test_style_colors_absolute_alpha_dark(self) -> None:
        for opacity in (0.0, 0.25, 0.5, 0.65, 1.0):
            text, panel = protocol.style_colors(protocol.STYLE_WHITE_ON_BLACK, opacity)
            self.assertEqual(panel, (0.0, 0.0, 0.0, opacity))
            self.assertEqual(text, (1.0, 1.0, 1.0, 1.0))

    def test_style_colors_absolute_alpha_light(self) -> None:
        for opacity in (0.0, 0.25, 0.5, 0.65, 1.0):
            text, panel = protocol.style_colors(protocol.STYLE_BLACK_ON_WHITE, opacity)
            self.assertEqual(panel, (1.0, 1.0, 1.0, opacity))
            self.assertEqual(text, (0.0, 0.0, 0.0, 1.0))

    def test_style_colors_missing_or_malformed_uses_default(self) -> None:
        for bad in (None, "x", True, float("nan")):
            _text, panel = protocol.style_colors(protocol.STYLE_WHITE_ON_BLACK, bad)
            self.assertEqual(panel[3], 0.65)
        _text, panel = protocol.style_colors(protocol.STYLE_WHITE_ON_BLACK)
        self.assertEqual(panel[3], 0.65)

    def test_style_colors_zero_not_multiplied(self) -> None:
        _text, panel = protocol.style_colors(protocol.STYLE_WHITE_ON_BLACK, 0.0)
        self.assertEqual(panel[3], 0.0)


class PresentationPanelOpacityTest(unittest.TestCase):
    """Phase 2P.1I: opacity persistence + legacy default."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "overlay_presentation.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_legacy_entry_defaults_to_065(self) -> None:
        self.path.write_text(
            _doc({"A": {"style": "white_on_black", "font_size": 20}}), encoding="utf-8"
        )
        self.assertEqual(pres.PresentationStore(self.path).get("A")["panel_opacity"], 0.65)

    def test_malformed_opacity_defaults_to_065(self) -> None:
        self.path.write_text(
            _doc({"A": {"style": "white_on_black", "font_size": 20, "panel_opacity": "x"}}),
            encoding="utf-8",
        )
        self.assertEqual(pres.PresentationStore(self.path).get("A")["panel_opacity"], 0.65)

    def test_zero_opacity_preserved_on_load(self) -> None:
        self.path.write_text(
            _doc({"A": {"style": "white_on_black", "font_size": 20, "panel_opacity": 0.0}}),
            encoding="utf-8",
        )
        self.assertEqual(pres.PresentationStore(self.path).get("A")["panel_opacity"], 0.0)

    def test_opacity_round_trip_all_values(self) -> None:
        values = {"a": 0.0, "b": 0.05, "c": 0.25, "d": 0.5, "e": 0.65, "f": 1.0}
        store = pres.PresentationStore(self.path)
        for rid, value in values.items():
            store.update(rid, protocol.STYLE_WHITE_ON_BLACK, 20, value)
        store.save()
        reloaded = pres.PresentationStore(self.path)
        for rid, value in values.items():
            self.assertEqual(reloaded.get(rid)["panel_opacity"], value, rid)


class ManagerPanelOpacityTest(unittest.TestCase):
    """Phase 2P.1I: per-Region runtime opacity + save integration."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "overlay_presentation.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _manager(self) -> OverlayManager:
        return OverlayManager(presentation_path=self.path)

    def test_new_region_defaults_to_065(self) -> None:
        manager = self._manager()
        self.assertEqual(
            asyncio.run(manager.get_region_panel_opacity("NEW_ID"))["panel_opacity"], 0.65
        )

    def test_zero_opacity_remains_zero(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_opacity("A", 0.0))
        self.assertEqual(asyncio.run(manager.get_region_panel_opacity("A"))["panel_opacity"], 0.0)

    def test_per_region_independent(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_opacity("A", 0.3))
        asyncio.run(manager.set_region_panel_opacity("B", 0.8))
        self.assertEqual(asyncio.run(manager.get_region_panel_opacity("A"))["panel_opacity"], 0.3)
        self.assertEqual(asyncio.run(manager.get_region_panel_opacity("B"))["panel_opacity"], 0.8)

    def test_malformed_rejected_preserves_previous(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_opacity("A", 0.3))
        bad = asyncio.run(manager.set_region_panel_opacity("A", "nope"))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "invalid_panel_opacity")
        self.assertEqual(asyncio.run(manager.get_region_panel_opacity("A"))["panel_opacity"], 0.3)

    def test_save_persists_opacity(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_style("A", "black_on_white"))
        asyncio.run(manager.set_region_font_size("A", 30))
        asyncio.run(manager.set_region_panel_opacity("A", 0.3))
        result = asyncio.run(manager.save_region_appearance("A"))
        self.assertTrue(result["ok"])
        reloaded = pres.PresentationStore(self.path)
        self.assertEqual(reloaded.get("A")["panel_opacity"], 0.3)

    def test_save_zero_opacity_persists(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_opacity("A", 0.0))
        asyncio.run(manager.save_region_appearance("A"))
        reloaded = pres.PresentationStore(self.path)
        self.assertEqual(reloaded.get("A")["panel_opacity"], 0.0)

    def test_restore_on_init(self) -> None:
        store = pres.PresentationStore(self.path)
        store.update("A", protocol.STYLE_WHITE_ON_BLACK, 20, 0.25)
        store.update("B", protocol.STYLE_WHITE_ON_BLACK, 20, 0.8)
        store.save()
        manager = self._manager()
        self.assertEqual(asyncio.run(manager.get_region_panel_opacity("A"))["panel_opacity"], 0.25)
        self.assertEqual(asyncio.run(manager.get_region_panel_opacity("B"))["panel_opacity"], 0.8)

    def test_set_region_text_carries_opacity(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_opacity("A", 0.3))
        asyncio.run(manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "hi"))
        self.assertEqual(manager._region_text["A"]["panel_opacity"], 0.3)

    def test_opacity_change_does_not_start_renderer(self) -> None:
        manager = self._manager()
        asyncio.run(manager.set_region_panel_opacity("A", 0.3))
        self.assertEqual(manager.status()["state"], "DISABLED")
        self.assertIsNone(manager._proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
