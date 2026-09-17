#!/usr/bin/env python3
"""Phase 2M.2C tests: Region Profile ("Region Set") JSON management + Region CRUD.

Covers profile index validation, bootstrap/migration from recognition_roi.json,
add/delete/select, atomic writes, orphan/corrupt handling, path-traversal
rejection, and the engine RPC surface (including no-OCR/no-renderer lifecycle).

Run:
    python3 scripts/test_region_profiles.py
"""

from __future__ import annotations

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

_SETTINGS_DIR = tempfile.mkdtemp(prefix="clarifydeck-region-profiles-")
sys.modules.setdefault(
    "decky",
    SimpleNamespace(
        logger=logging.getLogger("clarifydeck-region-profiles-test"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_SETTINGS_DIR=_SETTINGS_DIR,
        DECKY_PLUGIN_DIR=".",
        emit=lambda *args, **kwargs: None,
    ),
)

import main  # noqa: E402
from capture import recognition_regions as rr  # noqa: E402
from capture import region_profiles as rp  # noqa: E402
from capture.errors import CaptureError  # noqa: E402


def _region(rid="r1", x=0.1, y=0.1, w=0.2, h=0.2, enabled=True, name=None):
    return {"region_id": rid, "x": x, "y": y, "w": w, "h": h, "enabled": enabled, "name": name}


def _write_v2_legacy(path: Path, regions) -> None:
    path.write_text(
        json.dumps({"version": 2, "global": {"regions": regions}, "per_game": {}}),
        encoding="utf-8",
    )


def _write_v1_legacy(path: Path) -> None:
    path.write_text(
        json.dumps(
            {"version": 1, "default_roi": {"x": 0.08, "y": 0.62, "width": 0.84, "height": 0.32}, "games": {}}
        ),
        encoding="utf-8",
    )


def _profile_regions(store: rp.RegionProfileStore):
    return rr.RegionConfigStore(store.active_profile_path()).get_global_regions()


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / "region_profiles"
        self.legacy = self.root / "recognition_roi.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_g1_valid_v2_legacy_becomes_one_active_profile(self) -> None:
        _write_v2_legacy(self.legacy, [_region("keep-a"), _region("keep-b", x=0.5)])
        store = rp.RegionProfileStore(self.directory, legacy_path=self.legacy)
        payload = store.payload()
        self.assertEqual(len(payload["profiles"]), 1)
        self.assertEqual(payload["active_profile_id"], payload["profiles"][0]["profile_id"])
        regions = _profile_regions(store)
        self.assertEqual([r.region_id for r in regions], ["keep-a", "keep-b"])
        self.assertAlmostEqual(regions[1].x, 0.5)

    def test_g1b_valid_v1_legacy_region_id_preserved(self) -> None:
        _write_v1_legacy(self.legacy)
        store = rp.RegionProfileStore(self.directory, legacy_path=self.legacy)
        regions = _profile_regions(store)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].region_id, rr.LEGACY_REGION_ID)
        self.assertAlmostEqual(regions[0].x, 0.08)

    def test_g2_no_legacy_creates_default_profile(self) -> None:
        store = rp.RegionProfileStore(self.directory, legacy_path=self.legacy)
        self.assertEqual(len(store.payload()["profiles"]), 1)
        regions = _profile_regions(store)
        self.assertEqual(len(regions), 1)
        self.assertTrue(regions[0].region_id)

    def test_g3_invalid_legacy_falls_back_safely(self) -> None:
        self.legacy.write_text("{not json", encoding="utf-8")
        store = rp.RegionProfileStore(self.directory, legacy_path=self.legacy)
        self.assertEqual(len(store.payload()["profiles"]), 1)
        self.assertEqual(len(_profile_regions(store)), 1)

    def test_g4_migration_idempotent_after_index_exists(self) -> None:
        _write_v2_legacy(self.legacy, [_region("first")])
        store = rp.RegionProfileStore(self.directory, legacy_path=self.legacy)
        first_id = store.active_profile_id
        # Change the legacy file; a fresh store must NOT re-import it.
        _write_v2_legacy(self.legacy, [_region("second", x=0.4)])
        store2 = rp.RegionProfileStore(self.directory, legacy_path=self.legacy)
        self.assertEqual(store2.active_profile_id, first_id)
        self.assertEqual([r.region_id for r in _profile_regions(store2)], ["first"])

    def test_g5_no_dual_write(self) -> None:
        _write_v2_legacy(self.legacy, [_region("legacy-a")])
        store = rp.RegionProfileStore(self.directory, legacy_path=self.legacy)
        before = self.legacy.read_bytes()
        path = store.active_profile_path()
        config = rr.RegionConfigStore(path)
        config.set_regions(None, rr.RecognitionRegionSet((rr.RecognitionRegion("new-a", 0.1, 0.1, 0.2, 0.2),)))
        self.assertEqual(self.legacy.read_bytes(), before)
        self.assertEqual([r.region_id for r in _profile_regions(store)], ["new-a"])


class ProfileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / "region_profiles"
        self.legacy = self.root / "recognition_roi.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self) -> rp.RegionProfileStore:
        return rp.RegionProfileStore(self.directory, legacy_path=self.legacy)

    def test_p1_missing_index_bootstraps(self) -> None:
        store = self._store()
        self.assertTrue((self.directory / rp.INDEX_FILENAME).is_file())
        self.assertEqual(len(store.payload()["profiles"]), 1)

    def test_p2_valid_load_restores_active(self) -> None:
        store = self._store()
        store.add()
        active = store.active_profile_id
        store2 = self._store()
        self.assertEqual(store2.active_profile_id, active)

    def test_p3_add_profile_unique_identity_and_label(self) -> None:
        store = self._store()
        before = {p["profile_id"] for p in store.payload()["profiles"]}
        result = store.add()
        self.assertEqual(len(result["profiles"]), 2)
        ids = {p["profile_id"] for p in result["profiles"]}
        self.assertEqual(len(ids), 2)
        self.assertTrue(ids - before)
        self.assertEqual(result["active_profile_id"], result["profiles"][-1]["profile_id"])
        self.assertEqual(result["profiles"][-1]["label"], "Region Set 2")
        self.assertTrue((self.directory / f"profile_{result['active_profile_id']}.json").is_file())

    def test_p4_new_profile_region_ids_unique(self) -> None:
        store = self._store()
        first_ids = {r.region_id for r in _profile_regions(store)}
        store.add()
        second_ids = {r.region_id for r in _profile_regions(store)}
        self.assertTrue(first_ids.isdisjoint(second_ids))

    def test_p5_max_profile_limit(self) -> None:
        store = self._store()
        for _ in range(rp.MAX_REGION_PROFILES - 1):
            store.add()
        self.assertEqual(len(store.payload()["profiles"]), rp.MAX_REGION_PROFILES)
        with self.assertRaises(CaptureError) as ctx:
            store.add()
        self.assertEqual(ctx.exception.code, "too_many_profiles")

    def test_p6_delete_non_last_removes_only_target(self) -> None:
        store = self._store()
        first_id = store.active_profile_id
        store.add()
        second_id = store.active_profile_id
        result = store.delete(first_id)
        ids = {p["profile_id"] for p in result["profiles"]}
        self.assertEqual(ids, {second_id})
        self.assertFalse((self.directory / f"profile_{first_id}.json").exists())

    def test_p7_cannot_delete_last_profile(self) -> None:
        store = self._store()
        with self.assertRaises(CaptureError) as ctx:
            store.delete(store.active_profile_id)
        self.assertEqual(ctx.exception.code, "cannot_delete_last_profile")

    def test_p8_delete_active_selects_valid_fallback(self) -> None:
        store = self._store()
        first_id = store.active_profile_id
        store.add()
        second_id = store.active_profile_id
        result = store.delete(second_id)
        self.assertEqual(result["active_profile_id"], first_id)
        self.assertTrue((self.directory / f"profile_{first_id}.json").is_file())

    def test_p9_switch_active_persists(self) -> None:
        store = self._store()
        first_id = store.active_profile_id
        store.add()
        self.assertTrue(store.select(first_id))
        self.assertEqual(self._store().active_profile_id, first_id)

    def test_p10_corrupt_index_recovers_and_preserves_evidence(self) -> None:
        store = self._store()
        (self.directory / rp.INDEX_FILENAME).write_text("{not json", encoding="utf-8")
        store2 = self._store()
        self.assertEqual(len(store2.payload()["profiles"]), 1)
        quarantined = list(self.directory.glob("index.corrupt-*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{not json")

    def test_p11_missing_referenced_profile_falls_back(self) -> None:
        store = self._store()
        first_id = store.active_profile_id
        store.add()
        second_id = store.active_profile_id
        (self.directory / f"profile_{second_id}.json").unlink()
        store2 = self._store()
        self.assertEqual(store2.active_profile_id, first_id)
        self.assertEqual([p["profile_id"] for p in store2.payload()["profiles"]], [first_id])

    def test_p12_corrupt_profile_file_skipped(self) -> None:
        store = self._store()
        first_id = store.active_profile_id
        store.add()
        second_id = store.active_profile_id
        (self.directory / f"profile_{second_id}.json").write_text("{not json", encoding="utf-8")
        store2 = self._store()
        self.assertEqual(store2.active_profile_id, first_id)
        self.assertEqual([p["profile_id"] for p in store2.payload()["profiles"]], [first_id])

    def test_p13_orphan_file_ignored(self) -> None:
        store = self._store()
        orphan_id = "orphanprofile"
        (self.directory / f"profile_{orphan_id}.json").write_text(
            json.dumps({"version": 2, "global": {"regions": [_region("orphan")]}, "per_game": {}}),
            encoding="utf-8",
        )
        payload = store.payload()
        self.assertEqual(len(payload["profiles"]), 1)
        self.assertFalse(store.select(orphan_id))

    def test_p14_traversal_filename_rejected(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        evil = self.root / "evil.json"
        evil.write_text("SHOULD_NOT_BE_LOADED", encoding="utf-8")
        (self.directory / rp.INDEX_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_profile_id": "evil",
                    "next_label_number": 2,
                    "profiles": [{"profile_id": "evil", "label": "Evil", "file": "../evil.json"}],
                }
            ),
            encoding="utf-8",
        )
        store = self._store()
        self.assertEqual(len(store.payload()["profiles"]), 1)
        self.assertNotEqual(store.active_profile_id, "evil")
        self.assertEqual(evil.read_text(encoding="utf-8"), "SHOULD_NOT_BE_LOADED")

    def test_p15_atomic_index_write_no_temp(self) -> None:
        store = self._store()
        store.add()
        leftovers = [p.name for p in self.directory.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        json.loads((self.directory / rp.INDEX_FILENAME).read_text(encoding="utf-8"))

    def test_p16_atomic_profile_write_no_temp(self) -> None:
        store = self._store()
        store.add()
        leftovers = [p.name for p in self.directory.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        json.loads(store.active_profile_path().read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_p17_permissions(self) -> None:
        store = self._store()
        store.add()
        self.assertEqual(stat.S_IMODE(os.stat(self.directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.directory / rp.INDEX_FILENAME).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(store.active_profile_path()).st_mode), 0o600)


class EngineProfileRpcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "recognition_roi.json"
        self.engine = main.ClarifyDeckEngine()
        self.engine.roi_config_path = lambda: self.path  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_profiles_get_default(self) -> None:
        result = self.engine.region_profiles_get()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["profiles"]), 1)
        self.assertEqual(result["active_profile_id"], result["profiles"][0]["profile_id"])
        self.assertEqual(result["max_profiles"], rp.MAX_REGION_PROFILES)

    def test_profile_add_and_select(self) -> None:
        first = self.engine.region_profiles_get()
        first_id = first["active_profile_id"]
        added = self.engine.region_profile_add()
        self.assertTrue(added["ok"])
        self.assertEqual(len(added["profiles"]), 2)
        second_id = added["active_profile_id"]
        self.assertNotEqual(first_id, second_id)
        selected = self.engine.region_profile_select(first_id)
        self.assertTrue(selected["ok"])
        self.assertEqual(selected["active_profile_id"], first_id)

    def test_profile_delete_and_last_guard(self) -> None:
        first_id = self.engine.region_profiles_get()["active_profile_id"]
        self.engine.region_profile_add()
        deleted = self.engine.region_profile_delete(
            self.engine.region_profiles_get()["active_profile_id"]
        )
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["active_profile_id"], first_id)
        last = self.engine.region_profile_delete(first_id)
        self.assertFalse(last["ok"])
        self.assertEqual(last["error"], "cannot_delete_last_profile")

    def test_profile_operations_do_not_start_ocr_or_renderer(self) -> None:
        self.engine.region_profile_add()
        self.engine.region_profile_select(
            self.engine.region_profiles_get()["active_profile_id"]
        )
        self.engine.region_profile_delete(
            self.engine.region_profiles_get()["active_profile_id"]
        )
        self.assertIsNone(self.engine._ocr_worker)
        self.assertIsNone(self.engine._overlay)

    def test_region_edit_writes_active_profile_only(self) -> None:
        self.engine.region_profile_add()
        active_path = Path(self.engine.region_config_get(None)["config_path"])
        other = [
            p
            for p in Path(self.engine.region_profiles_path()).glob("profile_*.json")
            if p != active_path
        ][0]
        other_before = other.read_bytes()
        result = self.engine.region_config_set([_region("active-a")], None)
        self.assertTrue(result["ok"])
        self.assertEqual(other.read_bytes(), other_before)
        self.assertEqual(
            [r["region_id"] for r in result["configured_regions"]], ["active-a"]
        )

    def test_profile_switch_reloads_regions(self) -> None:
        first_id = self.engine.region_profiles_get()["active_profile_id"]
        self.engine.region_config_set([_region("first-region")], None)
        self.engine.region_profile_add()
        self.engine.region_config_set([_region("second-region")], None)
        self.engine.region_profile_select(first_id)
        self.assertEqual(
            [r["region_id"] for r in self.engine.region_config_get(None)["configured_regions"]],
            ["first-region"],
        )

    def test_region_set_fresh_region_id_and_max(self) -> None:
        result = self.engine.region_config_set([_region(rid="")], None)
        new_id = result["configured_regions"][0]["region_id"]
        self.assertTrue(new_id)
        too_many = [_region(f"r{i}", x=0.0, y=0.0, w=0.1, h=0.1) for i in range(rr.MAX_REGIONS + 1)]
        rejected = self.engine.region_config_set(too_many, None)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "too_many_regions")

    def test_start_layout_snapshot_uses_active_profile(self) -> None:
        first_id = self.engine.region_profiles_get()["active_profile_id"]
        self.engine.region_config_set([_region("snap-a", x=0.1)], None)
        layout_a = self.engine._resolve_region_layout()
        self.assertEqual(list(layout_a.keys()), ["snap-a"])
        # A running session keeps its Start-time snapshot even after switching.
        self.engine.region_profile_add()
        self.engine.region_config_set([_region("snap-b", x=0.5)], None)
        self.assertEqual(list(layout_a.keys()), ["snap-a"])
        layout_b = self.engine._resolve_region_layout()
        self.assertEqual(list(layout_b.keys()), ["snap-b"])
        self.engine.region_profile_select(first_id)
        self.assertEqual(list(self.engine._resolve_region_layout().keys()), ["snap-a"])

    def test_no_dual_write_legacy_after_edit(self) -> None:
        _write_v2_legacy(self.path, [_region("legacy-a")])
        self.engine.region_config_get(None)  # bootstrap
        before = self.path.read_bytes()
        self.engine.region_config_set([_region("edited-a")], None)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reset_does_not_resurface_legacy(self) -> None:
        _write_v2_legacy(self.path, [_region("legacy-a")])
        self.engine.region_config_get(None)  # bootstrap migrates legacy-a
        result = self.engine.region_config_reset(None)
        self.assertFalse(result["configured"])
        self.assertNotEqual(result["effective_regions"][0]["region_id"], "legacy-a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
