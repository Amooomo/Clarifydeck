// Phase 2I.3 frontend harness: pure OCR-diagnostic logic + static safety guards.
// No new test framework: transpiles the pure TS module with the existing
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
const LOGIC = path.join(ROOT, "src", "ocrDiagnostic.ts");
const COMPONENT = path.join(ROOT, "src", "components", "OCRDiagnostic.tsx");
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

const logic = await loadTs(LOGIC, "ocrdiag");
const regionLogic = await loadTs(REGION_LOGIC, "region");
const componentSrc = fs.readFileSync(COMPONENT, "utf8");
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

// -- pure logic --------------------------------------------------------------

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

check("text: waiting when no event", () =>
  assert.equal(logic.renderStableText(undefined), "(waiting for stable text)"),
);
check("text: preserves newlines", () =>
  assert.equal(logic.renderStableText({ kind: "text", text: "line1\nline2" }), "line1\nline2"),
);
check("text: clear renders cleared", () =>
  assert.equal(logic.renderStableText({ kind: "clear", text: "" }), "(no stable text)"),
);
check("text: empty text waits", () => assert.equal(logic.renderStableText({ kind: "text", text: "" }), "(waiting for stable text)"));

check("identity: stable per session/event", () =>
  assert.equal(logic.eventIdentity({ worker_session_id: "s1", last_event_seq: 3 }), "s1:3"),
);
check("identity: repeated same session/event unchanged", () => {
  const a = logic.eventIdentity({ worker_session_id: "s1", last_event_seq: 3 });
  const b = logic.eventIdentity({ worker_session_id: "s1", last_event_seq: 3 });
  assert.equal(a, b);
});
check("identity: new session differs", () => {
  const a = logic.eventIdentity({ worker_session_id: "s1", last_event_seq: 3 });
  const b = logic.eventIdentity({ worker_session_id: "s2", last_event_seq: 1 });
  assert.notEqual(a, b);
});
check("identity: null without session", () => assert.equal(logic.eventIdentity({ last_event_seq: 3 }), null));

check("default change gate OFF", () => assert.equal(logic.DEFAULT_CHANGE_GATE, false));
check("poll cadence 1Hz", () => assert.equal(logic.POLL_INTERVAL_MS, 1000));

check("start disabled while RUNNING", () => assert.equal(logic.isStartDisabled("RUNNING", false), true));
check("start enabled when STOPPED", () => assert.equal(logic.isStartDisabled("STOPPED", false), false));
check("start disabled while busy", () => assert.equal(logic.isStartDisabled("STOPPED", true), true));
check("stop disabled when STOPPED", () => assert.equal(logic.isStopDisabled("STOPPED", false), true));
check("change gate toggle disabled while RUNNING", () =>
  assert.equal(logic.isChangeGateToggleDisabled("RUNNING"), true),
);
check("short session id", () => assert.equal(logic.shortSessionId("abcdef1234567890"), "abcdef12"));
check("state FAILED renders", () => assert.equal(logic.describeWorkerState({ state: "FAILED" }), "FAILED"));
check("state default STOPPED", () => assert.equal(logic.describeWorkerState(undefined), "STOPPED"));

// -- static guards -----------------------------------------------------------

check("component declares start_ocr_worker once", () => assert.equal(countOccurrences(componentSrc, '"start_ocr_worker"'), 1));
check("component calls start once (explicit handler)", () => assert.equal(countOccurrences(componentSrc, "startOcrWorker("), 1));
check("component calls stop once (explicit handler)", () => assert.equal(countOccurrences(componentSrc, "stopOcrWorker("), 1));

check("mount/effect does not start or stop OCR", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.length > 0, "useEffect block not found");
  assert.equal(body.includes("startOcrWorker"), false);
  assert.equal(body.includes("stopOcrWorker"), false);
});
check("polling uses read-only RPCs and clears its timer", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.includes("getOcrWorkerStatus"));
  assert.ok(body.includes("getLatestStableText"));
  assert.ok(body.includes("clearInterval"));
});
check("component does not poll at frame rate", () => {
  assert.ok(componentSrc.includes("POLL_INTERVAL_MS"));
  assert.equal(componentSrc.includes("setInterval(() => {"), true);
});
check("no translation call", () => {
  assert.equal(/translat/i.test(componentSrc), false);
  assert.equal(/translat/i.test(indexSrc), false);
});
check("no overlay/renderer spawn in the component", () => {
  for (const needle of ["renderer", "overlay", "spawn", "setsid", "python3"]) {
    assert.equal(componentSrc.includes(needle), false, `component contains ${needle}`);
  }
});
check("index renders the diagnostic section", () => assert.ok(indexSrc.includes("<OCRDiagnosticSection />")));
check("index does not reference start_ocr_worker", () => assert.equal(indexSrc.includes("start_ocr_worker"), false));
check("component never spawns a process itself", () => {
  for (const needle of ["child_process", "exec(", "spawn(", "python3"]) {
    assert.equal(componentSrc.includes(needle), false, `component contains ${needle}`);
  }
});

// -- post-2I.3 cleanup C1: temporary capture diagnostic UI removed -----------

check("cleanup: temporary capture section removed", () =>
  assert.equal(componentSrc.includes("Capture Diagnostic (Temporary)"), false),
);
check("cleanup: capture RPC bindings removed", () => {
  for (const needle of ["capture_producer_start", "capture_producer_stop", "capture_producer_status"]) {
    assert.equal(componentSrc.includes(needle), false, `component still declares ${needle}`);
  }
});
check("cleanup: capture-only component helpers/state removed", () => {
  for (const needle of [
    "startCaptureProducer",
    "stopCaptureProducer",
    "getCaptureProducerStatus",
    "captureBusy",
    "CAPTURE_DIAGNOSTIC_FPS",
  ]) {
    assert.equal(componentSrc.includes(needle), false, `component still references ${needle}`);
  }
});
check("cleanup: capture helpers removed from pure logic", () => {
  for (const needle of [
    "describeCaptureState",
    "isCaptureStartDisabled",
    "isCaptureStopDisabled",
    "renderCaptureStatus",
    "mapCaptureError",
    "CAPTURE_DIAGNOSTIC_FPS",
  ]) {
    assert.equal(typeof logic[needle], "undefined", `logic still exports ${needle}`);
  }
});
check("cleanup: polling still reads OCR worker status and latest text", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.length > 0, "useEffect block not found");
  assert.ok(body.includes("getOcrWorkerStatus"));
  assert.ok(body.includes("getLatestStableText"));
});
check("cleanup: polling clears its one interval", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.includes("clearInterval"));
});
check("cleanup: polling does not start or stop OCR", () => {
  const body = effectBlock(componentSrc);
  assert.equal(body.includes("startOcrWorker"), false);
  assert.equal(body.includes("stopOcrWorker"), false);
});
check("cleanup: OCR Start remains one explicit backend request", () =>
  assert.equal(countOccurrences(componentSrc, "startOcrWorker("), 1),
);
check("cleanup: OCR Stop remains one explicit backend request", () =>
  assert.equal(countOccurrences(componentSrc, "stopOcrWorker("), 1),
);
check("cleanup: capture_conflict error mapping retained", () =>
  assert.equal(logic.mapOcrWorkerError("capture_conflict"), "Stop the backend capture producer before starting OCR."),
);
check("cleanup: frontend does not synthesize capture_conflict", () =>
  assert.equal(componentSrc.includes("capture_conflict"), false),
);
check("cleanup: no process spawn introduced", () => {
  for (const needle of ["child_process", "exec(", "spawn(", "python3"]) {
    assert.equal(componentSrc.includes(needle), false, `component contains ${needle}`);
  }
});

// -- post-2I.3 cleanup C2: legacy frontend overlay/debug paths removed --------

check("c2: hard-false legacy feature flags removed", () => {
  for (const needle of ["ENABLE_LEGACY_SUBTITLE_OVERLAY", "ENABLE_NOTIFICATION_KEEPALIVE", "ENABLE_DEBUG_PROBES"]) {
    assert.equal(indexSrc.includes(needle), false, `index still contains ${needle}`);
  }
});
check("c2: no replacement feature flags introduced", () => {
  assert.equal(indexSrc.includes("ENABLE_"), false);
});
check("c2: obsolete debug probes removed", () => {
  for (const needle of ["CD raw", "CD probe", "CD overlay"]) {
    assert.equal(indexSrc.includes(needle), false, `index still contains ${needle}`);
  }
});
check("c2: legacy subtitle/keepalive controls removed", () => {
  for (const needle of ["Subtitle color", "Subtitle size", "Keep overlay visible", "mountOverlayKeepAlive", "subtitleBoxStyle"]) {
    assert.equal(indexSrc.includes(needle), false, `index still contains ${needle}`);
  }
});
check("c2: legacy settings plumbing removed", () => {
  for (const needle of ["globalTextColor", "globalFontSize", "globalToastEnabled", "settingsEvents", "useTextColor", "useFontSize", "useToastEnabled"]) {
    assert.equal(indexSrc.includes(needle), false, `index still contains ${needle}`);
  }
});
check("c2: OCR diagnostic section retained", () => assert.ok(indexSrc.includes("<OCRDiagnosticSection />")));
check("c2: recognition ROI retained", () => {
  for (const needle of ["Recognition Area", "roi_config_get", "roi_config_set", "roi_config_reset"]) {
    assert.ok(indexSrc.includes(needle), `index missing ${needle}`);
  }
});
check("c2: persistent backend overlay control retained", () => {
  assert.ok(indexSrc.includes("Persistent Game Overlay (Experimental)"));
  assert.equal(countOccurrences(indexSrc, "set_overlay_enabled"), 1);
  assert.equal(countOccurrences(indexSrc, "setOverlayEnabled("), 1);
});
check("c2: QAM region preview retained", () => {
  for (const needle of ["regionBoxStyle", "selectedRegionBoxStyle", "qamVisible"]) {
    assert.ok(indexSrc.includes(needle), `index missing ${needle}`);
  }
});

// -- post-2L.7 multi-region Recognition Region editor -------------------------

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
check("region: reorder changes primary", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b" })];
  const moved = r.moveRegion(regions, "b", -1);
  assert.deepEqual(moved.map((x) => x.region_id), ["b", "a"]);
  assert.equal(r.primaryRegionId(moved), "b");
});
check("region: scope app id mapping", () => {
  assert.equal(r.scopeAppId("global", "app1"), null);
  assert.equal(r.scopeAppId("per_game", "app1"), "app1");
});
check("region: this game disabled when no app id", () => {
  const options = r.scopeOptions(false);
  assert.equal(options[1].disabled, true);
  assert.equal(r.scopeOptions(true)[1].disabled, false);
});
check("region: inherited drafts strip ids on apply", () => {
  const effective = [R({ region_id: "legacy-primary" })];
  assert.equal(r.draftsForApply(effective, false)[0].region_id, "");
  assert.equal(r.draftsForApply(effective, true)[0].region_id, "legacy-primary");
});
check("region: new draft id stripped on apply", () =>
  assert.equal(r.draftsForApply([R({ region_id: "new-1" })], true)[0].region_id, ""),
);
check("region: describe source", () => {
  assert.equal(r.describeSource("global"), "Global regions");
  assert.equal(r.describeSource("legacy"), "Imported single region");
});
check("region: label marks primary", () =>
  assert.ok(r.regionLabel(R({ region_id: "a", name: "Dialogue" }), 0, "a").includes("Primary")),
);

check("region editor: RPCs declared once", () => {
  for (const needle of ["region_config_get", "region_config_set", "region_config_reset"]) {
    assert.equal(countOccurrences(regionComponentSrc, `"${needle}"`), 1, needle);
  }
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
check("region editor: shows inherited/effective note", () =>
  assert.ok(regionComponentSrc.includes("effective regions")),
);
check("index renders the region editor", () => assert.ok(indexSrc.includes("<RegionEditorSection />")));
check("index no longer presents legacy Regions as production editor", () => {
  assert.equal(indexSrc.includes('<PanelSection title="Regions">'), false);
  assert.ok(indexSrc.includes("Legacy Regions (Advanced)"));
});

// -- Phase 2L.8 live v2 region preview ----------------------------------------

check("preview: normalized geometry maps to screen rect", () => {
  const rect = r.regionScreenRect(R({ x: 0.13, y: 0.74, w: 0.16, h: 0.06 }), 1280, 800);
  assert.ok(Math.abs(rect.left - 166.4) < 0.001);
  assert.ok(Math.abs(rect.top - 592) < 0.001);
  assert.ok(Math.abs(rect.width - 204.8) < 0.001);
  assert.ok(Math.abs(rect.height - 48) < 0.001);
});
check("preview: second sanity example", () => {
  const rect = r.regionScreenRect(R({ x: 0.23, y: 0.8, w: 0.2, h: 0.12 }), 1280, 800);
  assert.ok(Math.abs(rect.left - 294.4) < 0.001);
  assert.ok(Math.abs(rect.top - 640) < 0.001);
  assert.ok(Math.abs(rect.width - 256) < 0.001);
  assert.ok(Math.abs(rect.height - 96) < 0.001);
});
check("preview: multiple regions render", () => {
  const rects = r.regionScreenRects([R({ region_id: "a" }), R({ region_id: "b", x: 0.5 })], 1000, 500);
  assert.equal(rects.length, 2);
  assert.equal(rects[1].left, 500);
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
check("preview: geometry change reflects immediately", () => {
  const moved = r.updateRegionGeometry([R({ region_id: "a", x: 0.1 })], "a", { x: 0.5 });
  r.setRegionPreview({ drafts: moved, selectedId: "a", primaryId: "a" });
  const rect = r.regionScreenRect(r.getRegionPreview().drafts[0], 1000, 500);
  assert.equal(rect.left, 500);
});
check("preview: add produces a rect", () => {
  const draft = r.newRegionDraft(0);
  const rect = r.regionScreenRect(draft, 1000, 500);
  assert.ok(rect.width > 0 && rect.height > 0);
});
check("preview: remove drops the rect", () => {
  const rects = r.regionScreenRects(r.removeRegion([R({ region_id: "a" }), R({ region_id: "b" })], "a"), 1000, 500);
  assert.equal(rects.length, 1);
});
check("preview: reorder changes primary without moving geometry", () => {
  const regions = [R({ region_id: "a", x: 0.1 }), R({ region_id: "b", x: 0.5 })];
  const moved = r.moveRegion(regions, "b", -1);
  assert.equal(r.primaryRegionId(moved), "b");
  assert.equal(moved[0].x, 0.5); // geometry unchanged, only order
  assert.equal(moved[1].x, 0.1);
});
check("preview: disabled region labeled and not primary", () => {
  const disabled = R({ region_id: "a", enabled: false, name: "Dialogue" });
  assert.ok(r.regionLabel(disabled, 0, r.primaryRegionId([disabled])).includes("off"));
  assert.equal(r.primaryRegionId([disabled]), null);
});

check("preview: store has no backend writes", () => {
  const source = fs.readFileSync(REGION_LOGIC, "utf8");
  assert.equal(/callable|region_config_set|fetch\(/.test(source), false);
});
check("preview: component syncs store and clears on unmount", () => {
  assert.ok(regionComponentSrc.includes("setRegionPreview("));
  assert.ok(regionComponentSrc.includes("clearRegionPreview("));
});
check("preview: only Apply calls region_config_set", () =>
  assert.equal(countOccurrences(regionComponentSrc, "regionConfigSet("), 1),
);
check("preview: overlay renders v2 drafts from the store", () => {
  assert.ok(indexSrc.includes("subscribeRegionPreview("));
  assert.ok(indexSrc.includes("regionScreenRect("));
  assert.ok(indexSrc.includes("regionPreview.drafts"));
});
check("preview: reloads persisted config when QAM opens", () =>
  assert.ok(regionComponentSrc.includes("useQuickAccessVisible")),
);
check("preview: rendered in Steam UI tree, not body-mounted", () => {
  assert.ok(indexSrc.includes('routerHook.addGlobalComponent("ClarifyDeckOverlay"'));
  assert.equal(indexSrc.includes("clarifydeck-overlay-root"), false);
  assert.equal(indexSrc.includes("document.body.appendChild"), false);
});
check("preview: QAM close clears preview", () => {
  assert.ok(regionComponentSrc.includes("clearRegionPreview"));
  assert.ok(indexSrc.includes("qamVisible"));
});
check("name: production editor has no text input", () => {
  assert.equal(regionComponentSrc.includes('type="text"'), false);
  assert.equal(regionComponentSrc.includes("setRegionName"), false);
});
check("name: auto labels Region N", () => {
  assert.equal(r.regionLabel(R({ region_id: "a", name: "Dialogue" }), 0, "a"), "Region 1 [Primary]");
  assert.equal(r.regionLabel(R({ region_id: "b" }), 1, null), "Region 2");
});
check("name: disabled label", () =>
  assert.equal(r.regionLabel(R({ region_id: "a", enabled: false }), 0, null), "Region 1 (off)"),
);
check("name: reorder renumbers by order, ids unchanged", () => {
  const regions = [R({ region_id: "a" }), R({ region_id: "b" })];
  const moved = r.moveRegion(regions, "b", -1);
  assert.deepEqual(moved.map((x) => x.region_id), ["b", "a"]);
  assert.equal(r.regionLabel(moved[0], 0, r.primaryRegionId(moved)), "Region 1 [Primary]");
});

// -- Phase 2L.8.2 explicit renderer-based region preview ----------------------

check("f1: preview default OFF and explicit control", () => {
  assert.ok(regionComponentSrc.includes("useState<boolean>("));
  assert.ok(regionComponentSrc.includes("getRegionEditorSession().previewOn"));
  assert.ok(regionComponentSrc.includes("setPreviewOn"));
  r.resetRegionEditorSession();
  assert.equal(r.getRegionEditorSession().previewOn, false);
});
check("f2: preview ON drives backend preview RPCs", () => {
  assert.ok(regionComponentSrc.includes("setRegionPreviewEnabled(previewOn)"));
  assert.ok(regionComponentSrc.includes("setRegionPreviewRegions("));
});
check("f3: live geometry reflected in preview payload", () => {
  const payload = r.regionPreviewPayload([R({ region_id: "a", x: 0.5 })], "a");
  assert.equal(payload[0].x, 0.5);
  assert.equal(payload[0].selected, true);
});
check("f4: add/remove update payload", () => {
  assert.equal(r.regionPreviewPayload([R({ region_id: "a" })], "a").length, 1);
  assert.equal(r.regionPreviewPayload(r.removeRegion([R({ region_id: "a" })], "a"), null).length, 0);
});
check("f5: reorder updates Primary and labels", () => {
  const moved = r.moveRegion([R({ region_id: "a" }), R({ region_id: "b" })], "b", -1);
  const payload = r.regionPreviewPayload(moved, null);
  assert.equal(payload[0].primary, true);
  assert.equal(payload[0].label, "Primary · Region 1");
  assert.equal(payload[1].label, "Region 2");
});
check("f6: selected region reflected by region_id", () => {
  const payload = r.regionPreviewPayload([R({ region_id: "a" }), R({ region_id: "b" })], "b");
  assert.equal(payload[0].selected, false);
  assert.equal(payload[1].selected, true);
});
check("f7: preview OFF clears renderer preview", () => {
  assert.ok(regionComponentSrc.includes("setRegionPreviewEnabled(previewOn)"));
  assert.ok(regionComponentSrc.includes("clearRegionPreviewRegions()"));
});
check("f8: QAM close clears preview", () => {
  assert.ok(regionComponentSrc.includes("setRegionPreviewEnabled(false)"));
  assert.ok(regionComponentSrc.includes("clearRegionPreview()"));
});
check("f9: preview does not touch OCR lifecycle", () => {
  for (const needle of ["start_ocr_worker", "stop_ocr_worker", "capture_producer"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});
check("f10: preview changes never write region config", () =>
  assert.equal(countOccurrences(regionComponentSrc, "regionConfigSet("), 1),
);
check("f: disabled preview label", () =>
  assert.equal(r.regionPreviewLabel(R({ region_id: "a", enabled: false }), 1, null), "Region 2 (off)"),
);

// -- Phase 2M.2A per-region translucent text panels ---------------------------

check("panel: two runtime styles only", () => {
  assert.equal(r.PANEL_STYLE_WHITE_ON_BLACK, "white_on_black");
  assert.equal(r.PANEL_STYLE_BLACK_ON_WHITE, "black_on_white");
  assert.equal(r.DEFAULT_PANEL_STYLE, "white_on_black");
  assert.equal(r.isPanelStyle("white_on_black"), true);
  assert.equal(r.isPanelStyle("black_on_white"), true);
  assert.equal(r.isPanelStyle("neon"), false);
  assert.equal(r.isPanelStyle(undefined), false);
});
check("panel: labels", () => {
  assert.equal(r.panelStyleLabel("white_on_black"), "Dark panel");
  assert.equal(r.panelStyleLabel("black_on_white"), "Light panel");
  assert.equal(r.panelStyleLabel(undefined), "Dark panel");
});
check("panel: style RPCs declared once", () => {
  for (const needle of ["region_panel_style_get", "region_panel_style_set"]) {
    assert.equal(countOccurrences(regionComponentSrc, `"${needle}"`), 1, needle);
  }
});
check("panel: selector targets selected region_id", () => {
  assert.ok(regionComponentSrc.includes("regionPanelStyleSet(selectedId, style)"));
});
check("panel: style state keyed by region_id", () => {
  assert.ok(regionComponentSrc.includes("styleByRegion"));
  assert.ok(regionComponentSrc.includes("styleByRegion[selectedId]"));
});
check("panel: style is runtime until explicitly saved", () => {
  assert.equal(regionComponentSrc.includes("overlay_presentation"), false);
  assert.equal(regionComponentSrc.includes("localStorage"), false);
  assert.ok(regionComponentSrc.includes("Text panel style"));
});
check("panel: bounded font-size range, no custom font controls", () => {
  assert.equal(r.MIN_REGION_FONT_SIZE, 14);
  assert.equal(r.MAX_REGION_FONT_SIZE, 48);
  for (const needle of ["fontFamily", "font_family", "fontWeight slider", "line-height"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});
check("panel: no scroll control added", () => {
  for (const needle of ["scroll_offset", "Scroll Up", "Scroll Down", "region_scroll"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});
check("panel: no opacity control added", () => {
  assert.equal(regionComponentSrc.includes("opacity"), false);
});
check("panel: no OCR/renderer lifecycle in style path", () => {
  for (const needle of ["start_ocr_worker", "stop_ocr_worker", "capture_producer", "set_overlay_enabled", "spawn(", "python3"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});
check("panel: editable Region Name not reintroduced", () => {
  assert.equal(regionComponentSrc.includes('type="text"'), false);
  assert.equal(regionComponentSrc.includes("setRegionName"), false);
});

// -- Phase 2M.2B per-region font size -----------------------------------------

check("font: runtime constants", () => {
  assert.equal(r.DEFAULT_REGION_FONT_SIZE, 20);
  assert.equal(r.MIN_REGION_FONT_SIZE, 14);
  assert.equal(r.MAX_REGION_FONT_SIZE, 48);
  assert.equal(r.REGION_FONT_SIZE_STEP, 2);
});
check("font: validation and clamping", () => {
  assert.equal(r.isRegionFontSize(20), true);
  assert.equal(r.isRegionFontSize(14), true);
  assert.equal(r.isRegionFontSize(48), true);
  assert.equal(r.isRegionFontSize(13), false);
  assert.equal(r.isRegionFontSize(49), false);
  assert.equal(r.isRegionFontSize(20.5), false);
  assert.equal(r.isRegionFontSize("20"), false);
  assert.equal(r.clampRegionFontSize(13), 14);
  assert.equal(r.clampRegionFontSize(49), 48);
  assert.equal(r.clampRegionFontSize(21), 22);
  assert.equal(r.clampRegionFontSize(Number.NaN), 20);
  assert.equal(r.clampRegionFontSize("x"), 20);
});
check("font: size RPCs declared once", () => {
  for (const needle of ["region_font_size_get", "region_font_size_set"]) {
    assert.equal(countOccurrences(regionComponentSrc, `"${needle}"`), 1, needle);
  }
});
check("font: slider min/max/step wired", () => {
  assert.ok(regionComponentSrc.includes("min={MIN_REGION_FONT_SIZE}"));
  assert.ok(regionComponentSrc.includes("max={MAX_REGION_FONT_SIZE}"));
  assert.ok(regionComponentSrc.includes("step={REGION_FONT_SIZE_STEP}"));
  assert.ok(regionComponentSrc.includes("value={selectedFontSize}"));
});
check("font: targets selected region_id", () => {
  assert.ok(regionComponentSrc.includes("regionFontSizeSet(selectedId, size)"));
});
check("font: state keyed by region_id", () => {
  assert.ok(regionComponentSrc.includes("fontByRegion"));
  assert.ok(regionComponentSrc.includes("fontByRegion[selectedId]"));
});
check("font: reloads on selection change", () => {
  assert.ok(regionComponentSrc.includes("regionFontSizeGet(selectedId)"));
});
check("font: style selector preserved", () => {
  assert.ok(regionComponentSrc.includes("regionPanelStyleSet(selectedId, style)"));
  assert.ok(regionComponentSrc.includes("panelStyleLabel"));
});
check("font: no persistence", () => {
  assert.equal(regionComponentSrc.includes("overlay_presentation"), false);
  assert.equal(regionComponentSrc.includes("localStorage"), false);
});
check("font: no scroll/touch controls", () => {
  for (const needle of ["scroll_offset", "Scroll Up", "Scroll Down", "region_scroll", "touch"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});

// -- Phase 2M.2C Region Profile ("Region Set") management + dropdown CRUD -------

check("profiles: RPCs declared once", () => {
  for (const needle of [
    "region_profiles_get",
    "region_profile_select",
    "region_profile_add",
    "region_profile_delete",
  ]) {
    assert.equal(countOccurrences(regionComponentSrc, `"${needle}"`), 1, needle);
  }
});
check("profiles: Region Set dropdown uses profile_id", () => {
  assert.ok(regionComponentSrc.includes("profileOptions"));
  assert.ok(regionComponentSrc.includes("data: profile.profile_id"));
  assert.ok(regionComponentSrc.includes("selectedOption={activeProfileId}"));
});
check("profiles: upper +/- controls with guards", () => {
  assert.ok(regionComponentSrc.includes("onClick={() => void addProfile()}"));
  assert.ok(regionComponentSrc.includes("onClick={() => void deleteProfile()}"));
  assert.ok(regionComponentSrc.includes("profiles.length <= 1"));
  assert.ok(regionComponentSrc.includes("profiles.length >= maxProfiles"));
});
check("profiles: switch reloads regions and selects fallback", () => {
  assert.ok(regionComponentSrc.includes("regionProfileSelect(profileId)"));
  assert.ok(regionComponentSrc.includes("await loadActiveRegions()"));
});
check("regions: dropdown uses region_id", () => {
  assert.ok(regionComponentSrc.includes("regionOptions"));
  assert.ok(regionComponentSrc.includes("data: region.region_id"));
  assert.ok(regionComponentSrc.includes("selectedOption={selectedId}"));
});
check("regions: lower +/- controls", () => {
  assert.ok(regionComponentSrc.includes("onClick={addRegion}"));
  assert.ok(regionComponentSrc.includes("onClick={removeSelected}"));
});
check("regions: add/delete selection fallback", () => {
  assert.ok(regionComponentSrc.includes("selectRegion(draft.region_id)"));
  assert.ok(regionComponentSrc.includes("nextSelectionAfterRemove"));
});
check("regions: draft Apply writes active profile only", () => {
  assert.equal(countOccurrences(regionComponentSrc, "regionConfigSet("), 1);
});
check("profiles: no persistence/scroll/touch UI", () => {
  for (const needle of ["overlay_presentation", "localStorage", "scroll_offset", "touch"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});
check("profiles: style and font selectors preserved", () => {
  assert.ok(regionComponentSrc.includes("regionPanelStyleSet(selectedId, style)"));
  assert.ok(regionComponentSrc.includes("regionFontSizeSet(selectedId, size)"));
});

// -- Phase 2M.2C.1 device remediation ------------------------------------------

check("remediation: four compact +/- controls", () => {
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
check("remediation: dropdown value normalization", () => {
  assert.equal(r.dropdownOptionValue({ data: "abc" }), "abc");
  assert.equal(r.dropdownOptionValue("abc"), "abc");
  assert.equal(r.dropdownOptionValue({ data: 2 }), "2");
  assert.equal(r.dropdownOptionValue(undefined), null);
  assert.equal(r.dropdownOptionValue({}), null);
});
check("remediation: Region selection explicit and remembered", () => {
  assert.ok(regionComponentSrc.includes("selectRegion(dropdownOptionValue(option))"));
  assert.ok(regionComponentSrc.includes("selectedIdRef"));
  assert.ok(regionComponentSrc.includes("rememberRegionSelection"));
});
check("remediation: editor session survives transient remount", () => {
  r.resetRegionEditorSession();
  r.rememberRegionSelection("r2");
  r.rememberRegionPreview(true);
  assert.equal(r.getRegionEditorSession().selectedId, "r2");
  assert.equal(r.getRegionEditorSession().previewOn, true);
  r.resetRegionEditorSession();
  assert.equal(r.getRegionEditorSession().selectedId, null);
  assert.equal(r.getRegionEditorSession().previewOn, false);
});
check("remediation: preview not disabled on transient remount", () => {
  assert.ok(regionComponentSrc.includes("qamVisibleRef"));
  assert.ok(regionComponentSrc.includes("getRegionEditorSession().previewOn"));
  assert.ok(regionComponentSrc.includes("rememberRegionPreview"));
  assert.ok(regionComponentSrc.includes("resetRegionEditorSession"));
});
check("remediation: no renderer/input changes in editor", () => {
  for (const needle of ["ShapeInput", "XInput2", "touch_scroll", "scroll_offset"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});

// -- Phase 2M.2D per-region presentation persistence --------------------------

check("persistence: save appearance RPC declared once", () => {
  assert.equal(countOccurrences(regionComponentSrc, '"region_appearance_save"'), 1);
});
check("persistence: save targets selected region_id", () => {
  assert.ok(regionComponentSrc.includes("regionAppearanceSave(selectedId)"));
  assert.ok(regionComponentSrc.includes("Save appearance"));
});
check("persistence: explicit save only (no auto-save)", () => {
  assert.equal(countOccurrences(regionComponentSrc, "regionAppearanceSave("), 1);
});
check("persistence: save failure surfaced", () => {
  assert.ok(regionComponentSrc.includes("Save appearance failed"));
});
check("persistence: no scroll/touch UI", () => {
  for (const needle of ["scroll_offset", "touch"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});

if (process.exitCode) {
  console.error(`\nfrontend OCR diagnostic harness FAILED (${passed} passed)`);
} else {
  console.log(`\nfrontend OCR diagnostic harness PASSED (${passed} checks)`);
}
