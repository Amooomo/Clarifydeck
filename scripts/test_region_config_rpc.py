#!/usr/bin/env python3
"""Phase 2L.7 tests: v2 RecognitionRegion backend config RPC.

Run:
    python3 scripts/test_region_config_rpc.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SETTINGS_DIR = tempfile.mkdtemp(prefix="clarifydeck-region-rpc-")
sys.modules.setdefault(
    "decky",
    SimpleNamespace(
        logger=logging.getLogger("clarifydeck-region-rpc-test"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_SETTINGS_DIR=_SETTINGS_DIR,
        DECKY_PLUGIN_DIR=".",
        emit=lambda *args, **kwargs: None,
    ),
)

import main  # noqa: E402
from capture import recognition_regions as rr  # noqa: E402


def _region(rid="r1", x=0.1, y=0.1, w=0.2, h=0.2, enabled=True, name=None):
    return {"region_id": rid, "x": x, "y": y, "w": w, "h": h, "enabled": enabled, "name": name}


def write_v1(path: Path) -> None:
    path.write_text(
        json.dumps({"version": 1, "default_roi": {"x": 0.08, "y": 0.62, "width": 0.84, "height": 0.32}, "games": {}}),
        encoding="utf-8",
    )


class RegionConfigRpcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "recognition_roi.json"
        self.engine = main.ClarifyDeckEngine()
        self.engine.roi_config_path = lambda: self.path  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_r1_get_global_v2(self) -> None:
        result = self.engine.region_config_set([_region("a", 0.1, 0.2, 0.3, 0.4)], None)
        self.assertTrue(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["scope"], "global")
        self.assertEqual(result["source"], "global")
        self.assertEqual([r["region_id"] for r in result["configured_regions"]], ["a"])
        self.assertEqual([r["region_id"] for r in result["effective_regions"]], ["a"])

    def test_r2_get_per_game(self) -> None:
        self.engine.region_config_set([_region("g")], None)
        self.engine.region_config_set([_region("p")], "app1")
        result = self.engine.region_config_get("app1")
        self.assertEqual(result["scope"], "per_game")
        self.assertEqual(result["source"], "per_game")
        self.assertEqual([r["region_id"] for r in result["configured_regions"]], ["p"])

    def test_r3_per_game_inheritance(self) -> None:
        self.engine.region_config_set([_region("g")], None)
        result = self.engine.region_config_get("app1")
        self.assertFalse(result["configured"])
        self.assertEqual(result["configured_regions"], [])
        self.assertEqual([r["region_id"] for r in result["effective_regions"]], ["g"])
        self.assertEqual(result["source"], "global")

    def test_r4_explicit_empty_set(self) -> None:
        result = self.engine.region_config_set([], None)
        self.assertTrue(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["configured_regions"], [])
        self.assertEqual(result["effective_regions"], [])

    def test_r5_legacy_v1_migrates_to_active_profile_no_rewrite(self) -> None:
        write_v1(self.path)
        before = self.path.read_bytes()
        result = self.engine.region_config_get(None)
        self.assertTrue(result["ok"])
        # First access bootstraps one profile preserving the legacy region.
        self.assertTrue(result["configured"])
        self.assertEqual(len(result["configured_regions"]), 1)
        self.assertAlmostEqual(result["configured_regions"][0]["x"], 0.08)
        self.assertEqual(result["configured_regions"][0]["region_id"], rr.LEGACY_REGION_ID)
        # The legacy migration source is never rewritten (no dual-write).
        self.assertEqual(self.path.read_bytes(), before)

    def test_r6_explicit_edit_adopts_v2(self) -> None:
        write_v1(self.path)
        result = self.engine.region_config_set([_region(rid="", x=0.08, y=0.62, w=0.84, h=0.32)], None)
        self.assertTrue(result["ok"])
        self.assertTrue(result["configured"])
        adopted_id = result["configured_regions"][0]["region_id"]
        self.assertTrue(adopted_id)
        self.assertNotEqual(adopted_id, rr.LEGACY_REGION_ID)
        reloaded = self.engine.region_config_get(None)
        self.assertEqual(reloaded["configured_regions"][0]["region_id"], adopted_id)
        self.assertAlmostEqual(reloaded["configured_regions"][0]["x"], 0.08)

    def test_r7_malformed_rejected_no_write(self) -> None:
        result = self.engine.region_config_set([_region("a", x=-1.0)], None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_region")
        self.assertFalse(self.path.exists())

    def test_r8_duplicate_ids_rejected(self) -> None:
        result = self.engine.region_config_set([_region("dup"), _region("dup", x=0.5)], None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "duplicate_region_id")

    def test_r9_max_regions_enforced(self) -> None:
        too_many = [_region(f"r{i}", x=0.0, y=0.0, w=0.1, h=0.1) for i in range(rr.MAX_REGIONS + 1)]
        result = self.engine.region_config_set(too_many, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "too_many_regions")

    def test_r10_backend_assigns_stable_id(self) -> None:
        result = self.engine.region_config_set([_region(rid="")], None)
        new_id = result["configured_regions"][0]["region_id"]
        self.assertTrue(new_id)
        self.assertEqual(self.engine.region_config_get(None)["configured_regions"][0]["region_id"], new_id)

    def test_reset_clears_scope(self) -> None:
        self.engine.region_config_set([_region("a")], None)
        result = self.engine.region_config_reset(None)
        self.assertTrue(result["ok"])
        self.assertFalse(result["configured"])

    def test_atomic_persistence_and_stable_ids(self) -> None:
        result = self.engine.region_config_set(
            [_region("a"), _region("b", x=0.5, enabled=False, name="Dialogue")], None
        )
        self.assertTrue(result["ok"])
        profile_path = Path(result["config_path"])
        leftovers = [p.name for p in profile_path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        regions = payload["global"]["regions"]
        self.assertEqual([r["region_id"] for r in regions], ["a", "b"])
        self.assertFalse(regions[1]["enabled"])
        self.assertEqual(regions[1]["name"], "Dialogue")
        # The legacy recognition_roi.json is never created/updated after migration.
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
