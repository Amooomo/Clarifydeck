#!/usr/bin/env python3
"""Phase 2L.1 tests: multi-region recognition configuration foundation.

Pure stdlib. Covers model validation, v2 persistence, precedence, legacy
single-ROI compatibility, and the transitional primary-region rule.

Run:
    python3 scripts/test_recognition_regions.py
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import recognition_roi  # noqa: E402
from capture import recognition_regions as rr  # noqa: E402
from capture.errors import CaptureError  # noqa: E402
from capture.roi import DEFAULT_ROI  # noqa: E402


def region(rid="r1", x=0.1, y=0.2, w=0.3, h=0.4, enabled=True, name=None):
    return rr.RecognitionRegion(region_id=rid, x=x, y=y, w=w, h=h, enabled=enabled, name=name)


def write_v1(path: Path, default_roi=None, games=None) -> None:
    path.write_text(
        json.dumps({"version": 1, "default_roi": default_roi, "games": games or {}}),
        encoding="utf-8",
    )


class ModelValidationTest(unittest.TestCase):
    def test_r1_valid_region(self) -> None:
        rr.validate_region(region())
        region_set = rr.RecognitionRegionSet((region(),))
        self.assertEqual(region_set.regions[0].region_id, "r1")

    def test_r2_invalid_geometry_rejected(self) -> None:
        bad = [
            dict(x=-0.1),
            dict(y=-0.1),
            dict(w=0.0),
            dict(h=0.0),
            dict(w=0.5, x=0.6),  # x + w > 1
            dict(h=0.5, y=0.6),  # y + h > 1
            dict(x=math.nan),
            dict(w=math.inf),
            dict(h=-math.inf),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(CaptureError):
                    rr.validate_region(region(**overrides))

    def test_r3_duplicate_ids_rejected(self) -> None:
        with self.assertRaises(CaptureError) as ctx:
            rr.RecognitionRegionSet((region("dup"), region("dup", x=0.5)))
        self.assertEqual(ctx.exception.code, "duplicate_region_id")

    def test_r4_bounded_region_count(self) -> None:
        regions = tuple(region(f"r{i}", x=0.0, y=0.0, w=0.1, h=0.1) for i in range(rr.MAX_REGIONS))
        rr.RecognitionRegionSet(regions)  # exactly max is fine
        too_many = tuple(region(f"r{i}", x=0.0, y=0.0, w=0.1, h=0.1) for i in range(rr.MAX_REGIONS + 1))
        with self.assertRaises(CaptureError) as ctx:
            rr.RecognitionRegionSet(too_many)
        self.assertEqual(ctx.exception.code, "too_many_regions")

    def test_r5_disabled_state_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            store = rr.RegionConfigStore(path)
            store.set_regions(None, rr.RecognitionRegionSet((region(enabled=False),)))
            reloaded = rr.RegionConfigStore(path)
            self.assertFalse(reloaded.get_global_regions()[0].enabled)

    def test_r6_order_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            store = rr.RegionConfigStore(path)
            store.set_regions(None, rr.RecognitionRegionSet((region("c"), region("a"), region("b"))))
            reloaded = rr.RegionConfigStore(path)
            self.assertEqual([r.region_id for r in reloaded.get_global_regions()], ["c", "a", "b"])


class PersistenceTest(unittest.TestCase):
    def test_p1_v2_global_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            original = rr.RecognitionRegionSet((region("a", 0.1, 0.2, 0.3, 0.4, True), region("b", 0.5, 0.5, 0.2, 0.2, False)))
            rr.RegionConfigStore(path).set_regions(None, original)
            reloaded = rr.RegionConfigStore(path)
            self.assertEqual(reloaded.get_global_regions(), original.regions)

    def test_p2_v2_per_game_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            original = rr.RecognitionRegionSet((region("a", 0.0, 0.0, 0.5, 0.5, True),))
            rr.RegionConfigStore(path).set_regions("app1", original)
            reloaded = rr.RegionConfigStore(path)
            self.assertEqual(reloaded.get_regions("app1"), original.regions)

    def test_p3_precedence_per_game_over_global_over_legacy_over_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            write_v1(path, default_roi={"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2})
            store = rr.RegionConfigStore(path)
            legacy_resolver = recognition_roi.ActiveROIResolver(recognition_roi.ROIConfigStore(path))
            resolver = rr.RegionResolver(store, legacy_resolver)

            # legacy layer
            effective = resolver.resolve_effective_regions()
            self.assertAlmostEqual(effective.primary().x, 0.1)
            self.assertAlmostEqual(effective.primary().w, 0.2)

            # global v2 wins over legacy
            store.set_regions(None, rr.RecognitionRegionSet((region("g", 0.3, 0.3, 0.3, 0.3),)))
            self.assertEqual(resolver.resolve_effective_regions().primary().region_id, "g")

            # per-game v2 wins over global
            store.set_regions("app1", rr.RecognitionRegionSet((region("p", 0.4, 0.4, 0.2, 0.2),)))
            self.assertEqual(resolver.resolve_effective_regions("app1").primary().region_id, "p")

    def test_builtin_default_when_nothing_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = rr.RegionConfigStore(Path(tmp) / "recognition_roi.json")
            resolver = rr.RegionResolver(store)
            effective = resolver.resolve_effective_regions()
            self.assertEqual(len(effective.regions), 1)
            self.assertEqual(effective.primary().region_id, rr.BUILTIN_REGION_ID)
            self.assertAlmostEqual(effective.primary().x, DEFAULT_ROI.x)

    def test_p4_malformed_config_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"

            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(rr.RegionConfigStore(path).last_error, "invalid_json")

            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            self.assertTrue(rr.RegionConfigStore(path).last_error.startswith("unsupported_version"))

            path.write_text(
                json.dumps({"version": 2, "global": {"regions": [{"region_id": "bad", "x": 5, "y": 0, "w": 0.1, "h": 0.1}]}}),
                encoding="utf-8",
            )
            store = rr.RegionConfigStore(path)
            self.assertIsNone(store.get_global_regions())  # untrusted geometry dropped

    def test_p5_atomic_write_leaves_no_temp_and_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            rr.RegionConfigStore(path).set_regions(None, rr.RecognitionRegionSet((region(),)))
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])
            json.loads(path.read_text(encoding="utf-8"))


class LegacyCompatibilityTest(unittest.TestCase):
    def test_l1_legacy_global_single_roi_loads_as_one_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            write_v1(path, default_roi={"x": 0.08, "y": 0.62, "width": 0.84, "height": 0.32})
            store = rr.RegionConfigStore(path)
            resolver = rr.RegionResolver(store)
            effective = resolver.resolve_effective_regions()
            self.assertEqual(len(effective.regions), 1)
            primary = effective.primary()
            self.assertAlmostEqual(primary.x, 0.08)
            self.assertAlmostEqual(primary.y, 0.62)
            self.assertAlmostEqual(primary.w, 0.84)
            self.assertAlmostEqual(primary.h, 0.32)

    def test_l2_legacy_per_game_roi_loads_as_one_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            write_v1(path, games={"app1": {"roi": {"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.3}}})
            store = rr.RegionConfigStore(path)
            resolver = rr.RegionResolver(store)
            effective = resolver.resolve_effective_regions("app1")
            self.assertAlmostEqual(effective.primary().x, 0.2)
            self.assertAlmostEqual(effective.primary().w, 0.3)

    def test_l3_read_only_resolution_does_not_rewrite_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            write_v1(path, default_roi={"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2})
            before = path.read_bytes()
            resolver = rr.RegionResolver(rr.RegionConfigStore(path))
            resolver.resolve_effective_regions()
            resolver.primary_region()
            self.assertEqual(path.read_bytes(), before)

    def test_l4_first_v2_write_creates_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            write_v1(path, default_roi={"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2})
            store = rr.RegionConfigStore(path)
            resolver = rr.RegionResolver(store)
            adopted = resolver.adopt_effective_regions()
            new_id = adopted.primary().region_id
            self.assertNotEqual(new_id, rr.LEGACY_REGION_ID)
            reloaded = rr.RegionConfigStore(path)
            self.assertEqual(reloaded.get_global_regions()[0].region_id, new_id)
            self.assertAlmostEqual(reloaded.get_global_regions()[0].x, 0.1)

    def test_l5_legacy_roi_store_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recognition_roi.json"
            legacy = recognition_roi.ROIConfigStore(path)
            roi = recognition_roi.parse_roi({"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2})
            legacy.set(None, roi)
            self.assertAlmostEqual(recognition_roi.ROIConfigStore(path).get(None).x, 0.1)


class PrimaryRegionTest(unittest.TestCase):
    def _resolver(self, regions):
        with tempfile.TemporaryDirectory() as tmp:
            store = rr.RegionConfigStore(Path(tmp) / "recognition_roi.json")
            store.set_regions(None, rr.RecognitionRegionSet(tuple(regions)))
            return rr.RegionResolver(store)

    def test_c1_one_enabled_region(self) -> None:
        resolver = self._resolver([region("only")])
        self.assertEqual(resolver.primary_region().region_id, "only")

    def test_c2_multiple_enabled_first_wins(self) -> None:
        resolver = self._resolver([region("first"), region("second", x=0.5)])
        self.assertEqual(resolver.primary_region().region_id, "first")

    def test_c3_disabled_first_uses_next_enabled(self) -> None:
        resolver = self._resolver([region("off", enabled=False), region("on", x=0.5)])
        self.assertEqual(resolver.primary_region().region_id, "on")

    def test_c4_no_enabled_user_regions(self) -> None:
        resolver = self._resolver([region("off", enabled=False)])
        self.assertIsNone(resolver.primary_region())

    def test_c4_empty_region_set_is_valid(self) -> None:
        resolver = self._resolver([])
        effective = resolver.resolve_effective_regions()
        self.assertEqual(effective.regions, ())
        self.assertIsNone(resolver.primary_region())


if __name__ == "__main__":
    unittest.main(verbosity=2)
