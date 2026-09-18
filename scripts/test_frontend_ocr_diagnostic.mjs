// Phase 2P.1 frontend harness: production QAM logic + static safety guards.
// No new test framework: transpiles the pure TS modules with the existing
// `typescript` devDependency and asserts behavior + source invariants.

import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import url from "node:url";

const require = createRequire(import.meta.url);
const ts = require("typescript");

const ROOT = path.resolve(path.dirname(url.fileURLToPath(import.meta.url)), "..");
const LOGIC = path.join(ROOT, "src", "ocrControl.ts");
const COMPONENT = path.join(ROOT, "src", "components", "OCRControl.tsx");
const OVERLAY_COMPONENT = path.join(ROOT, "src", "components", "PersistentOverlayControl.tsx");
const INDEX = path.join(ROOT, "src", "index.tsx");
const REGION_LOGIC = path.join(ROOT, "src", "regionEditor.ts");
const REGION_COMPONENT = path.join(ROOT, "src", "components", "RegionEditor.tsx");

async function loadTs(file, tag) {
  const source = fs.readFileSync(file, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const tmp = path.join(os.tmpdir(), `clarifydeck-${tag}-${process.pid}.mjs`);
  fs.writeFileSync(tmp, output, "utf8");
  try {
    return await import(url.pathToFileURL(tmp).href);
  } finally {
    fs.unlinkSync(tmp);
  }
}

const logic = await loadTs(LOGIC, "ocrcontrol");
const regionLogic = await loadTs(REGION_LOGIC, "region");
const componentSrc = fs.readFileSync(COMPONENT, "utf8");
const overlayComponentSrc = fs.readFileSync(OVERLAY_COMPONENT, "utf8");
const regionComponentSrc = fs.readFileSync(REGION_COMPONENT, "utf8");
const indexSrc = fs.readFileSync(INDEX, "utf8");

let passed = 0;
function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`ok - ${name}`);
  } catch (error) {
    console.error(`FAIL - ${name}`);
    console.error(error);
    process.exitCode = 1;
  }
}

function countOccurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

function effectBlock(source) {
  const start = source.indexOf("useEffect(");
  const end = source.indexOf("}, []);", start);
  return source.slice(start, end);
}

// -- production OCR worker pure logic ----------------------------------------

check("error: not_leader", () =>
  assert.equal(logic.mapOcrWorkerError("not_leader"), "OCR worker can only be started by the backend leader."),
);
check("error: capture_conflict", () =>
  assert.equal(logic.mapOcrWorkerError("capture_conflict"), "Stop the backend capture producer before starting OCR."),
);
check("error: model_missing", () => assert.equal(logic.mapOcrWorkerError("model_missing"), "OCR model files are missing."));
check("error: forbidden_interpreter", () =>
  assert.equal(logic.mapOcrWorkerError("forbidden_interpreter"), "Unsafe Python interpreter was rejected."),
);
check("error: unknown surfaced safely", () => assert.equal(logic.mapOcrWorkerError("weird", "boom"), "weird: boom"));
check("error: empty falls back", () => assert.equal(logic.mapOcrWorkerError(undefined, undefined), "OCR request failed."));

check("default change gate OFF", () => assert.equal(logic.DEFAULT_CHANGE_GATE, false));
check("poll cadence 1Hz", () => assert.equal(logic.POLL_INTERVAL_MS, 1000));

check("state: RUNNING", () => assert.equal(logic.describeWorkerState({ state: "RUNNING" }), "RUNNING"));
check("state: FAILED renders", () => assert.equal(logic.describeWorkerState({ state: "FAILED" }), "FAILED"));
check("state: default STOPPED", () => assert.equal(logic.describeWorkerState(undefined), "STOPPED"));

check("running helper", () => {
  assert.equal(logic.isRunning("RUNNING"), true);
  assert.equal(logic.isRunning("STOPPED"), false);
});
check("start disabled while RUNNING", () => assert.equal(logic.isStartDisabled("RUNNING", false), true));
check("start disabled while STARTING/STOPPING", () => {
  assert.equal(logic.isStartDisabled("STARTING", false), true);
  assert.equal(logic.isStartDisabled("STOPPING", false), true);
});
check("start enabled when STOPPED/FAILED", () => {
  assert.equal(logic.isStartDisabled("STOPPED", false), false);
  assert.equal(logic.isStartDisabled("FAILED", false), false);
});
check("start disabled while busy", () => assert.equal(logic.isStartDisabled("STOPPED", true), true));
check("stop enabled only while RUNNING", () => {
  assert.equal(logic.isStopDisabled("RUNNING", false), false);
  assert.equal(logic.isStopDisabled("STOPPED", false), true);
  assert.equal(logic.isStopDisabled("STARTING", false), true);
  assert.equal(logic.isStopDisabled("STOPPING", false), true);
  assert.equal(logic.isStopDisabled("FAILED", false), true);
});

// -- production OCR control component ----------------------------------------

check("ocr: RPCs declared once", () => {
  for (const needle of ["start_ocr_worker", "stop_ocr_worker", "get_ocr_worker_status"]) {
    assert.equal(countOccurrences(componentSrc, `"${needle}"`), 1, needle);
  }
});
check("ocr: start called once (explicit handler)", () => assert.equal(countOccurrences(componentSrc, "startOcrWorker("), 1));
check("ocr: stop called once (explicit handler)", () => assert.equal(countOccurrences(componentSrc, "stopOcrWorker("), 1));
check("ocr: start uses production change gate value", () =>
  assert.ok(componentSrc.includes("startOcrWorker(DEFAULT_CHANGE_GATE)")),
);
check("ocr: mount/effect does not start or stop OCR", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.length > 0, "useEffect block not found");
  assert.equal(body.includes("startOcrWorker"), false);
  assert.equal(body.includes("stopOcrWorker"), false);
});
check("ocr: polling reads worker status and clears its timer", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.includes("getOcrWorkerStatus"));
  assert.ok(body.includes("clearInterval"));
  assert.ok(componentSrc.includes("POLL_INTERVAL_MS"));
});
check("ocr: status line is production wording", () => {
  assert.ok(componentSrc.includes("Status: {state}"));
  assert.equal(componentSrc.includes("Worker:"), false);
});
check("ocr: single Start/Stop button only", () => {
  assert.ok(componentSrc.includes('running ? "Stop OCR" : "Start OCR"'));
  assert.equal(componentSrc.includes("Start OCR capture"), false);
});
check("ocr: no diagnostic panel content", () => {
  for (const needle of [
    "OCR Diagnostic",
    "OCR DIAGNOSTIC",
    "Session:",
    "Event #",
    "Change gate",
    "Use change-gated OCR",
    "Stable text",
    "Confidence",
    "source seq",
    "Received:",
    "rejected",
    "out-of-order",
    "get_latest_stable_text",
    "getLatestStableText",
    "renderStableText",
    "worker_session_id",
    "transport",
    "pid",
  ]) {
    assert.equal(componentSrc.includes(needle), false, `component contains ${needle}`);
  }
});
check("ocr: never spawns a process itself", () => {
  for (const needle of ["child_process", "exec(", "spawn(", "python3", "renderer", "overlay"]) {
    assert.equal(componentSrc.includes(needle), false, `component contains ${needle}`);
  }
});
check("ocr: no translation call", () => {
  assert.equal(/translat/i.test(componentSrc), false);
  assert.equal(/translat/i.test(indexSrc), false);
});

// -- production persistent overlay control -----------------------------------

check("overlay: RPCs declared once", () => {
  assert.equal(countOccurrences(overlayComponentSrc, '"get_status"'), 1);
  assert.equal(countOccurrences(overlayComponentSrc, '"set_overlay_enabled"'), 1);
  assert.equal(countOccurrences(overlayComponentSrc, "setOverlayEnabled("), 1);
});
check("overlay: explicit toggle only, never on mount", () => {
  const body = effectBlock(overlayComponentSrc);
  assert.ok(body.length > 0, "useEffect block not found");
  assert.equal(body.includes("setOverlayEnabled"), false);
});
check("overlay: shows state and no process debug lines", () => {
  assert.ok(overlayComponentSrc.includes("Status: {state}"));
  for (const needle of ["renderer_pid", "Backend:", "Overlay display", "pid"]) {
    assert.equal(overlayComponentSrc.includes(needle), false, `overlay contains ${needle}`);
  }
});
check("overlay: surfaces actual errors", () => {
  assert.ok(overlayComponentSrc.includes("last_error"));
  assert.ok(overlayComponentSrc.includes("errorStyle"));
});
check("overlay: does not touch OCR lifecycle", () => {
  for (const needle of ["start_ocr_worker", "stop_ocr_worker", "spawn(", "python3"]) {
    assert.equal(overlayComponentSrc.includes(needle), false, `overlay contains ${needle}`);
  }
});

// -- two-page QAM navigation --------------------------------------------------

check("index: two pages via native Tabs", () => {
  assert.ok(indexSrc.includes("Tabs"));
  assert.ok(indexSrc.includes("activeTab={page}"));
  assert.ok(indexSrc.includes("onShowTab"));
});
check("index: opens on Page 1 (OCR)", () => {
  assert.ok(indexSrc.includes('const PAGE_OCR = "ocr"'));
  assert.ok(indexSrc.includes("useState<string>(PAGE_OCR)"));
});
check("index: has a compact fallback page header", () => {
  assert.ok(indexSrc.includes("FallbackPages"));
  assert.ok(indexSrc.includes("pageTabStyle"));
});
check("index: renders the production page sections", () => {
  assert.ok(indexSrc.includes("<OCRControlSection />"));
  assert.ok(indexSrc.includes("<PersistentOverlaySection />"));
  assert.ok(indexSrc.includes("<RegionEditorSection />"));
});
check("index: page switch does not touch OCR/overlay state", () => {
  assert.equal(indexSrc.includes("start_ocr_worker"), false);
  assert.equal(indexSrc.includes("stop_ocr_worker"), false);
  assert.equal(indexSrc.includes("set_overlay_enabled"), false);
});
check("index: authoritative v2 region preview retained", () => {
  assert.ok(indexSrc.includes("subscribeRegionPreview("));
  assert.ok(indexSrc.includes("regionScreenRect("));
  assert.ok(indexSrc.includes("regionPreview.drafts"));
  assert.ok(indexSrc.includes('routerHook.addGlobalComponent("ClarifyDeckOverlay"'));
  assert.equal(indexSrc.includes("clarifydeck-overlay-root"), false);
  assert.equal(indexSrc.includes("document.body.appendChild"), false);
  assert.ok(indexSrc.includes("qamVisible"));
});

// -- legacy / diagnostic UI removed -------------------------------------------

check("legacy: removed headings/actions absent", () => {
  for (const needle of [
    "Legacy Recognition Area (Advanced)",
    "Legacy Regions (Advanced)",
    "Test OCR now",
    "OCR language",
    "OCR tuning",
    "Start OCR capture",
    "Stop OCR capture",
    "Move up",
    "Move down",
    "Persistent Game Overlay (Experimental)",
    "OCR DIAGNOSTIC",
  ]) {
    assert.equal(indexSrc.includes(needle), false, `index still contains ${needle}`);
  }
});
check("legacy: old plugin/ROI/OCR callables removed", () => {
  for (const needle of [
    "start_plugin",
    "stop_plugin",
    "set_ocr_lang",
    "set_ocr_options",
    "run_ocr_now",
    "roi_config_get",
    "roi_config_set",
    "roi_config_reset",
    "ROI_PRESETS",
    "LANGUAGES",
    "CoordinateSlider",
    "validateRoiDraft",
  ]) {
    assert.equal(indexSrc.includes(needle), false, `index still contains ${needle}`);
  }
});
check("legacy: no hard-false feature flags reintroduced", () => {
  for (const needle of ["ENABLE_LEGACY_SUBTITLE_OVERLAY", "ENABLE_NOTIFICATION_KEEPALIVE", "ENABLE_DEBUG_PROBES", "ENABLE_"]) {
    assert.equal(indexSrc.includes(needle), false, `index still contains ${needle}`);
  }
});
check("legacy: removed diagnostic pure helpers gone", () => {
  for (const needle of ["renderStableText", "eventIdentity", "shortSessionId", "isChangeGateToggleDisabled"]) {
    assert.equal(typeof logic[needle], "undefined", `logic still exports ${needle}`);
  }
});

// -- region editor pure logic -------------------------------------------------

const r = regionLogic;
const R = (over = {}) => ({ region_id: "r1", x: 0.1, y: 0.1, w: 0.2, h: 0.2, enabled: true, ...over });

check("region: primary is first enabled", () =>
  assert.equal(r.primaryRegionId([R({ region_id: "a" }), R({ region_id: "b" })]), "a"),
);
check("region: primary skips disabled", () =>
  assert.equal(r.primaryRegionId([R({ region_id: "a", enabled: false }), R({ region_id: "b" })]), "b"),
);
check("region: primary none when all disabled", () =>
  assert.equal(r.primaryRegionId([R({ enabled: false })]), null),
);
check("region: add cap", () => {
  const eight = Array.from({ length: r.MAX_REGIONS }, (_, i) => R({ region_id: `r${i}` }));
  assert.equal(r.canAddRegion(eight), false);
  assert.equal(r.canAddRegion(eight.slice(0, 7)), true);
});
check("region: new draft is valid and unique", () => {
  const a = r.newRegionDraft(0);
  const b = r.newRegionDraft(1);
  assert.notEqual(a.region_id, b.region_id);
  assert.equal(a.enabled, true);
  assert.equal(r.validateRegionDraft(a), "");
});
check("region: validate rejects bad geometry", () => {
  assert.notEqual(r.validateRegionDraft(R({ w: 0 })), "");
  assert.notEqual(r.validateRegionDraft(R({ x: 0.9, w: 0.2 })), "");
  assert.notEqual(r.validateRegionDraft(R({ x: Number.NaN })), "");
  assert.equal(r.validateRegionDraft(R({ x: 0.1, y: 0.1, w: 0.2, h: 0.2 })), "");
});
check("region: geometry edit keeps id", () => {
  const next = r.updateRegionGeometry([R({ region_id: "a" })], "a", { x: 0.5 });
  assert.equal(next[0].region_id, "a");
  assert.equal(next[0].x, 0.5);
});
check("region: enable/name edits", () => {
  assert.equal(r.setRegionEnabled([R()], "r1", false)[0].enabled, false);
  assert.equal(r.setRegionName([R()], "r1", "Dialogue")[0].name, "Dialogue");
});
check("region: remove and next selection", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b" }), R({ region_id: "c" })];
  assert.deepEqual(r.removeRegion(regions, "b").map((x) => x.region_id), ["a", "c"]);
  assert.equal(r.nextSelectionAfterRemove(regions, "b"), "c");
  assert.equal(r.nextSelectionAfterRemove([R({ region_id: "a" })], "a"), null);
});
check("region: scope app id mapping", () => {
  assert.equal(r.scopeAppId("global", "app1"), null);
  assert.equal(r.scopeAppId("per_game", "app1"), "app1");
});
check("region: inherited drafts strip ids on apply", () => {
  const effective = [R({ region_id: "legacy-primary" })];
  assert.equal(r.draftsForApply(effective, false)[0].region_id, "");
  assert.equal(r.draftsForApply(effective, true)[0].region_id, "legacy-primary");
});
check("region: new draft id stripped on apply", () =>
  assert.equal(r.draftsForApply([R({ region_id: "new-1" })], true)[0].region_id, ""),
);
check("region: label marks primary", () =>
  assert.ok(r.regionLabel(R({ region_id: "a", name: "Dialogue" }), 0, "a").includes("Primary")),
);

// -- Set as Primary -----------------------------------------------------------

check("primary: enabled selected becomes Primary", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b" }), R({ region_id: "c" })];
  const next = r.setPrimaryRegion(regions, "c");
  assert.deepEqual(next.map((x) => x.region_id), ["c", "a", "b"]);
  assert.equal(r.primaryRegionId(next), "c");
});
check("primary: other regions retain relative order", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b" }), R({ region_id: "c" }), R({ region_id: "d" })];
  const next = r.setPrimaryRegion(regions, "c");
  assert.deepEqual(next.map((x) => x.region_id), ["c", "a", "b", "d"]);
});
check("primary: already-primary is stable", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b" })];
  assert.deepEqual(r.setPrimaryRegion(regions, "a"), regions);
});
check("primary: disabled selected is not silently enabled", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b", enabled: false })];
  const next = r.setPrimaryRegion(regions, "b");
  assert.equal(next[1].enabled, false);
  assert.equal(r.primaryRegionId(next), "a");
});
check("primary: disabled-first ordering preserved", () => {
  const regions = [R({ region_id: "a", enabled: false }), R({ region_id: "b" }), R({ region_id: "c" })];
  const next = r.setPrimaryRegion(regions, "c");
  assert.deepEqual(next.map((x) => x.region_id), ["a", "c", "b"]);
  assert.equal(next[0].enabled, false);
  assert.equal(r.primaryRegionId(next), "c");
});
check("primary: unknown region id is a no-op", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b" })];
  assert.deepEqual(r.setPrimaryRegion(regions, "zzz"), regions);
});

// -- region editor component --------------------------------------------------

check("region editor: RPCs declared once", () => {
  for (const needle of ["region_config_get", "region_config_set"]) {
    assert.equal(countOccurrences(regionComponentSrc, `"${needle}"`), 1, needle);
  }
  assert.equal(regionComponentSrc.includes("region_config_reset"), false);
});
check("region editor: no OCR/renderer lifecycle calls", () => {
  for (const needle of ["start_ocr_worker", "stop_ocr_worker", "capture_producer", "set_overlay_enabled", "spawn(", "python3"]) {
    assert.equal(regionComponentSrc.includes(needle), false, `component contains ${needle}`);
  }
});
check("region editor: states next-OCR-start notice", () =>
  assert.ok(regionComponentSrc.includes("Changes apply on next OCR start")),
);
check("region editor: surfaces backend validation error", () => {
  assert.ok(regionComponentSrc.includes("result.detail"));
  assert.ok(regionComponentSrc.includes("result.error"));
});
check("region editor: developer metadata removed", () => {
  for (const needle of [
    "Scope: Global",
    "Source:",
    "This Game editing is unavailable",
    "No saved regions for this scope yet",
    "First enabled region is the Primary",
    "describeSource",
    "scopeOptions",
  ]) {
    assert.equal(regionComponentSrc.includes(needle), false, `region editor contains ${needle}`);
  }
});
check("region editor: production section headings", () => {
  for (const needle of ["PROFILE", "REGION", "AREA (% OF SCREEN)", "APPEARANCE"]) {
    assert.ok(regionComponentSrc.includes(needle), `missing ${needle}`);
  }
});
check("region editor: Set as Primary replaces Move up/down", () => {
  assert.ok(regionComponentSrc.includes("Set as Primary"));
  assert.ok(regionComponentSrc.includes("Primary Region"));
  assert.ok(regionComponentSrc.includes("setPrimaryRegion("));
  assert.equal(regionComponentSrc.includes("Move up"), false);
  assert.equal(regionComponentSrc.includes("Move down"), false);
});
check("region editor: Reset removed, Save Changes wraps both saves", () => {
  assert.equal(regionComponentSrc.includes("regionConfigReset"), false);
  assert.equal(regionComponentSrc.includes("onClick={reset}"), false);
  assert.ok(regionComponentSrc.includes("Save Changes"));
  assert.equal(countOccurrences(regionComponentSrc, "regionConfigSet("), 1);
  assert.equal(countOccurrences(regionComponentSrc, "regionAppearanceSave("), 1);
  assert.ok(regionComponentSrc.includes("validateRegions(drafts)"));
});
check("region editor: compact +/- controls unchanged", () => {
  assert.equal(countOccurrences(regionComponentSrc, "style={compactButtonStyle("), 4);
  assert.equal(countOccurrences(regionComponentSrc, "<button"), 4);
  assert.ok(regionComponentSrc.includes('width: "30px"'));
  assert.ok(regionComponentSrc.includes('height: "30px"'));
  assert.ok(regionComponentSrc.includes("minmax(0, 1fr) 30px 30px"));
  assert.ok(regionComponentSrc.includes('aria-label="Add Region Set"'));
  assert.ok(regionComponentSrc.includes('aria-label="Delete Region Set"'));
  assert.ok(regionComponentSrc.includes('aria-label="Add Region"'));
  assert.ok(regionComponentSrc.includes('aria-label="Delete Region"'));
});
check("region editor: dropdowns use profile_id/region_id", () => {
  assert.ok(regionComponentSrc.includes("data: profile.profile_id"));
  assert.ok(regionComponentSrc.includes("selectedOption={activeProfileId}"));
  assert.ok(regionComponentSrc.includes("data: region.region_id"));
  assert.ok(regionComponentSrc.includes("selectedOption={selectedId}"));
});
check("region editor: preview, enabled, area, appearance retained", () => {
  assert.ok(regionComponentSrc.includes("Show Region Preview"));
  assert.ok(regionComponentSrc.includes("[x] Enabled"));
  assert.ok(regionComponentSrc.includes('label="X%"'));
  assert.ok(regionComponentSrc.includes('label="Y%"'));
  assert.ok(regionComponentSrc.includes('label="W%"'));
  assert.ok(regionComponentSrc.includes('label="H%"'));
  assert.ok(regionComponentSrc.includes("panelStyleLabel"));
  assert.ok(regionComponentSrc.includes("regionFontSizeSet(selectedId, size)"));
});
check("region editor: dropdown value normalization retained", () => {
  assert.equal(r.dropdownOptionValue({ data: "abc" }), "abc");
  assert.equal(r.dropdownOptionValue("abc"), "abc");
  assert.equal(r.dropdownOptionValue({ data: 2 }), "2");
  assert.equal(r.dropdownOptionValue(undefined), null);
  assert.equal(r.dropdownOptionValue({}), null);
});
check("region editor: editor session survives transient remount", () => {
  r.resetRegionEditorSession();
  r.rememberRegionSelection("r2");
  r.rememberRegionPreview(true);
  assert.equal(r.getRegionEditorSession().selectedId, "r2");
  assert.equal(r.getRegionEditorSession().previewOn, true);
  r.resetRegionEditorSession();
  assert.equal(r.getRegionEditorSession().selectedId, null);
  assert.equal(r.getRegionEditorSession().previewOn, false);
});
check("region editor: no persistence/scroll/touch UI", () => {
  for (const needle of ["overlay_presentation", "localStorage", "scroll_offset", "touch", "ShapeInput", "XInput2"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});

// -- region preview -----------------------------------------------------------

check("preview: normalized geometry maps to screen rect", () => {
  const rect = r.regionScreenRect(R({ x: 0.13, y: 0.74, w: 0.16, h: 0.06 }), 1280, 800);
  assert.ok(Math.abs(rect.left - 166.4) < 0.001);
  assert.ok(Math.abs(rect.top - 592) < 0.001);
  assert.ok(Math.abs(rect.width - 204.8) < 0.001);
  assert.ok(Math.abs(rect.height - 48) < 0.001);
});
check("preview: store tracks drafts/selection/primary", () => {
  const drafts = [R({ region_id: "a" }), R({ region_id: "b", enabled: false })];
  r.setRegionPreview({ drafts, selectedId: "a", primaryId: r.primaryRegionId(drafts) });
  const state = r.getRegionPreview();
  assert.equal(state.drafts.length, 2);
  assert.equal(state.selectedId, "a");
  assert.equal(state.primaryId, "a");
});
check("preview: clear removes all drafts", () => {
  r.setRegionPreview({ drafts: [R()], selectedId: "r1", primaryId: "r1" });
  r.clearRegionPreview();
  assert.deepEqual(r.getRegionPreview().drafts, []);
});
check("preview: payload reflects primary and selection", () => {
  const moved = r.setPrimaryRegion([R({ region_id: "a" }), R({ region_id: "b" })], "b");
  const payload = r.regionPreviewPayload(moved, "b");
  assert.equal(payload[0].primary, true);
  assert.equal(payload[0].selected, true);
  assert.equal(payload[0].label, "Primary · Region 1");
});
check("preview: store has no backend writes", () => {
  const source = fs.readFileSync(REGION_LOGIC, "utf8");
  assert.equal(/callable|region_config_set|fetch\(/.test(source), false);
});
check("preview: component syncs store and clears on unmount", () => {
  assert.ok(regionComponentSrc.includes("setRegionPreview("));
  assert.ok(regionComponentSrc.includes("clearRegionPreview("));
  assert.ok(regionComponentSrc.includes("setRegionPreviewEnabled(previewOn)"));
  assert.ok(regionComponentSrc.includes("setRegionPreviewRegions("));
  assert.ok(regionComponentSrc.includes("clearRegionPreviewRegions()"));
});
check("preview: reloads persisted config when QAM opens", () =>
  assert.ok(regionComponentSrc.includes("useQuickAccessVisible")),
);
check("preview: no text input / editable names", () => {
  assert.equal(regionComponentSrc.includes('type="text"'), false);
  assert.equal(regionComponentSrc.includes("setRegionName"), false);
});
check("preview: panel/font runtime helpers retained", () => {
  assert.equal(r.PANEL_STYLE_WHITE_ON_BLACK, "white_on_black");
  assert.equal(r.PANEL_STYLE_BLACK_ON_WHITE, "black_on_white");
  assert.equal(r.DEFAULT_PANEL_STYLE, "white_on_black");
  assert.equal(r.panelStyleLabel("white_on_black"), "Dark panel");
  assert.equal(r.panelStyleLabel("black_on_white"), "Light panel");
  assert.equal(r.MIN_REGION_FONT_SIZE, 14);
  assert.equal(r.MAX_REGION_FONT_SIZE, 48);
  assert.equal(r.REGION_FONT_SIZE_STEP, 2);
  assert.equal(r.clampRegionFontSize(13), 14);
  assert.equal(r.clampRegionFontSize(49), 48);
});

if (process.exitCode) {
  console.error(`\nfrontend QAM production harness FAILED (${passed} passed)`);
} else {
  console.log(`\nfrontend QAM production harness PASSED (${passed} checks)`);
}
