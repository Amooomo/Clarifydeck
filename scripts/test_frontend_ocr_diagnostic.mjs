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
const QAM_PAGES = path.join(ROOT, "src", "qamPages.ts");

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
const qamPages = await loadTs(QAM_PAGES, "qampages");
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

// -- ClarifyDeck-owned N-page navigation --------------------------------------

check("index: page state owned by ClarifyDeck (no Decky Tabs)", () => {
  assert.ok(indexSrc.includes("const PAGES: QamPage[]"));
  assert.ok(indexSrc.includes('{ id: "ocr", title: "OCR" }'));
  assert.ok(indexSrc.includes('{ id: "regions", title: "Regions" }'));
  assert.ok(indexSrc.includes("const [activeId, setActiveId] = useState<string>(() =>"));
  assert.ok(indexSrc.includes("resolveSessionPageId(PAGES, getQamPageSession())"));
  assert.equal(indexSrc.includes("from \"@decky/ui\""), true);
  assert.equal(/\bTabs\b/.test(indexSrc.replace(/\/\/.*$/gm, "")), false);
});
check("index: single page-switch state for click and shoulder", () => {
  assert.ok(indexSrc.includes("const goToPage = useCallback"));
  assert.ok(indexSrc.includes("const goPreviousPage = useCallback"));
  assert.ok(indexSrc.includes("const goNextPage = useCallback"));
  assert.ok(indexSrc.includes("onSelect={goToPage}"));
  assert.equal((indexSrc.match(/setActiveId\(/g) || []).length >= 1, true);
});
check("index: compact page header controls", () => {
  assert.ok(indexSrc.includes("function PageHeader("));
  assert.ok(indexSrc.includes("pageTabStyle(page.id === activeId)"));
  assert.ok(indexSrc.includes("onClick={() => onSelect(page.id)}"));
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

// -- Scoped shoulder navigation ------------------------------------------------

check("shoulder: scoped Focusable handler", () => {
  assert.ok(indexSrc.includes("Focusable"));
  assert.ok(indexSrc.includes("onButtonDown={onButtonDown}"));
  assert.ok(indexSrc.includes("flow-children=\"vertical\""));
  assert.ok(indexSrc.includes("typeof Focusable === \"function\""));
});
check("shoulder: L1/R1 map to previous/next page", () => {
  assert.ok(indexSrc.includes("GamepadButton.BUMPER_LEFT"));
  assert.ok(indexSrc.includes("GamepadButton.BUMPER_RIGHT"));
  assert.ok(indexSrc.includes("goPreviousPage()"));
  assert.ok(indexSrc.includes("goNextPage()"));
});
check("shoulder: handler stops propagation", () => {
  assert.ok(indexSrc.includes("evt.stopPropagation()"));
});
check("shoulder: no global controller/key listeners", () => {
  for (const needle of [
    "window.addEventListener",
    "document.addEventListener",
    "keydown",
    "SteamClient.Input",
    "RegisterForControllerInputMessages",
    "navigator.getGamepads",
  ]) {
    assert.equal(indexSrc.includes(needle), false, `index contains ${needle}`);
  }
});

// -- N-page pure navigation logic ----------------------------------------------

const P = (id, title) => ({ id, title });

check("pages: previous at first page clamps", () => {
  assert.equal(qamPages.getPreviousPageIndex(0, 2), 0);
  assert.equal(qamPages.getPreviousPageIndex(0, 4), 0);
});
check("pages: next at last page clamps", () => {
  assert.equal(qamPages.getNextPageIndex(1, 2), 1);
  assert.equal(qamPages.getNextPageIndex(3, 4), 3);
});
check("pages: two-page previous/next", () => {
  assert.equal(qamPages.getPreviousPageIndex(1, 2), 0);
  assert.equal(qamPages.getNextPageIndex(0, 2), 1);
});
check("pages: future four-page simulation, no wraparound", () => {
  const pages = [P("ocr", "OCR"), P("regions", "Regions"), P("translate", "Translate"), P("display", "Display")];
  assert.equal(qamPages.pageIndexById(pages, "translate"), 2);
  assert.equal(qamPages.getNextPageIndex(2, pages.length), 3);
  assert.equal(qamPages.getPreviousPageIndex(2, pages.length), 1);
  assert.equal(qamPages.getNextPageIndex(3, pages.length), 3);
  assert.equal(qamPages.getPreviousPageIndex(0, pages.length), 0);
});
check("pages: unknown id falls back safely", () => {
  const pages = [P("ocr", "OCR"), P("regions", "Regions")];
  assert.equal(qamPages.pageIndexById(pages, "missing"), 0);
  assert.equal(qamPages.getNextPageIndex(qamPages.pageIndexById(pages, "missing"), pages.length), 1);
});
check("pages: empty page list is safe", () => {
  assert.equal(qamPages.getPreviousPageIndex(0, 0), 0);
  assert.equal(qamPages.getNextPageIndex(0, 0), 0);
  assert.equal(qamPages.clampPageIndex(5, 2), 1);
});
check("pages: click and shoulder share one page index", () => {
  const pages = [P("ocr", "OCR"), P("regions", "Regions")];
  // Click "regions" -> index 1; then L1 -> index 0; then R1 -> index 1.
  const clicked = qamPages.pageIndexById(pages, "regions");
  assert.equal(pages[clicked].id, "regions");
  assert.equal(pages[qamPages.getPreviousPageIndex(clicked, pages.length)].id, "ocr");
  assert.equal(pages[qamPages.getNextPageIndex(0, pages.length)].id, "regions");
});

// -- QAM page session (transient-remount lifetime) ----------------------------

check("page session: fresh session defaults to OCR", () => {
  qamPages.resetQamPageSession();
  const pages = [P("ocr", "OCR"), P("regions", "Regions")];
  assert.equal(qamPages.getQamPageSession(), null);
  assert.equal(qamPages.resolveSessionPageId(pages, qamPages.getQamPageSession()), "ocr");
});
check("page session: switch to Regions is stored", () => {
  qamPages.resetQamPageSession();
  qamPages.rememberQamPageSession("regions");
  assert.equal(qamPages.getQamPageSession(), "regions");
});
check("page session: transient remount restores Regions", () => {
  const pages = [P("ocr", "OCR"), P("regions", "Regions")];
  qamPages.resetQamPageSession();
  qamPages.rememberQamPageSession("regions");
  // Simulate the owner remounting: re-read the session for the initial state.
  assert.equal(qamPages.resolveSessionPageId(pages, qamPages.getQamPageSession()), "regions");
});
check("page session: genuine close resets next open to OCR", () => {
  const pages = [P("ocr", "OCR"), P("regions", "Regions")];
  qamPages.rememberQamPageSession("regions");
  qamPages.resetQamPageSession();
  assert.equal(qamPages.resolveSessionPageId(pages, qamPages.getQamPageSession()), "ocr");
});
check("page session: stale/unknown id falls back to OCR", () => {
  const pages = [P("ocr", "OCR"), P("regions", "Regions")];
  assert.equal(qamPages.resolveSessionPageId(pages, "translate"), "ocr");
  assert.equal(qamPages.resolveSessionPageId(pages, ""), "ocr");
  qamPages.resetQamPageSession();
});
check("page session: index content is session-backed", () => {
  assert.ok(indexSrc.includes("getQamPageSession()"));
  assert.ok(indexSrc.includes("resolveSessionPageId(PAGES, getQamPageSession())"));
  assert.ok(indexSrc.includes("rememberQamPageSession(activeId)"));
  assert.ok(indexSrc.includes("resetQamPageSession()"));
});
check("page: no QAM-visibility-driven page reset", () => {
  // The Decky visibility signal blips during context-menu/preview activity; it
  // must never reset the active page.
  assert.equal(indexSrc.includes("wasVisible && !qamVisible"), false);
  assert.equal(indexSrc.includes("setActiveId(PAGE_OCR)"), false);
});
check("page: only explicit navigation changes the page", () => {
  // setActiveId is used only by goToPage / goPreviousPage / goNextPage.
  assert.equal(countOccurrences(indexSrc, "setActiveId("), 3);
  assert.ok(indexSrc.includes("onSelect={goToPage}"));
  assert.ok(indexSrc.includes("GamepadButton.BUMPER_LEFT"));
  assert.ok(indexSrc.includes("GamepadButton.BUMPER_RIGHT"));
});
check("page: preview/region editor never touch the active page", () => {
  for (const needle of ["setActiveId", "goToPage", "goPreviousPage", "goNextPage", "rememberQamPageSession"]) {
    assert.equal(regionComponentSrc.includes(needle), false, `region editor contains ${needle}`);
  }
});
check("page session: reset on plugin dismount", () => {
  const dismount = indexSrc.slice(indexSrc.indexOf("onDismount()"));
  assert.ok(dismount.includes("resetQamPageSession()"));
});
check("page session: dropdown handlers never force a page", () => {
  assert.equal(regionComponentSrc.includes("setActiveId"), false);
  assert.equal(regionComponentSrc.includes("goToPage"), false);
  assert.equal(regionComponentSrc.includes("rememberQamPageSession"), false);
});

// -- region draft session lifetime --------------------------------------------

check("session: region draft state round-trips", () => {
  regionLogic.resetRegionEditorDraftState();
  assert.equal(regionLogic.getRegionEditorDraftState().active, false);
  const drafts = [{ region_id: "a", x: 0.5, y: 0.25, w: 0.2, h: 0.2, enabled: true }];
  regionLogic.rememberRegionEditorDraftState({
    active: true,
    activeProfileId: "p1",
    profiles: [{ profile_id: "p1", label: "Profile 1" }],
    maxProfiles: 8,
    configured: true,
    drafts,
    styleByRegion: { a: "black_on_white" },
    fontByRegion: { a: 24 },
  });
  const restored = regionLogic.getRegionEditorDraftState();
  assert.equal(restored.active, true);
  assert.equal(restored.activeProfileId, "p1");
  assert.equal(restored.drafts[0].x, 0.5);
  assert.equal(restored.drafts[0].y, 0.25);
  assert.equal(restored.styleByRegion.a, "black_on_white");
  assert.equal(restored.fontByRegion.a, 24);
});
check("session: region draft state resets on close", () => {
  regionLogic.rememberRegionEditorDraftState({
    active: true,
    activeProfileId: "p1",
    profiles: [],
    maxProfiles: 8,
    configured: true,
    drafts: [{ region_id: "a", x: 0.1, y: 0.1, w: 0.2, h: 0.2, enabled: true }],
    styleByRegion: {},
    fontByRegion: {},
  });
  regionLogic.resetRegionEditorDraftState();
  const reset = regionLogic.getRegionEditorDraftState();
  assert.equal(reset.active, false);
  assert.deepEqual(reset.drafts, []);
  assert.equal(reset.activeProfileId, null);
});
check("session: region editor hydrates and persists draft state", () => {
  assert.ok(regionComponentSrc.includes("getRegionEditorDraftState()"));
  assert.ok(regionComponentSrc.includes("rememberRegionEditorDraftState("));
  assert.ok(regionComponentSrc.includes("const resume = initialDraft.active"));
});
check("session: region editor never clears drafts on visibility/unmount", () => {
  assert.equal(regionComponentSrc.includes("resetRegionEditorDraftState"), false);
  assert.equal(regionComponentSrc.includes("resetRegionEditorSession"), false);
  assert.equal(regionComponentSrc.includes("useQuickAccessVisible"), false);
});
check("session: resume skips the initial backend reload", () => {
  assert.ok(regionComponentSrc.includes("if (!resume)"));
});

// -- Phase 2P.1H profile selection synchronization ----------------------------

check("profile: synchronous authoritative selection helpers", () => {
  // The session exposes synchronous profile-selection updates.
  regionLogic.resetRegionEditorDraftState();
  regionLogic.rememberActiveProfileId("p1");
  assert.equal(regionLogic.getRegionEditorDraftState().activeProfileId, "p1");
  regionLogic.rememberDraftsProfileId("p1");
  assert.equal(regionLogic.getRegionEditorDraftState().draftsProfileId, "p1");
  regionLogic.resetRegionEditorDraftState();
  assert.equal(regionLogic.getRegionEditorDraftState().activeProfileId, null);
  assert.equal(regionLogic.getRegionEditorDraftState().draftsProfileId, null);
});
check("profile: select synchronously sets active profile + session", () => {
  const body = regionComponentSrc.slice(
    regionComponentSrc.indexOf("const selectProfile"),
    regionComponentSrc.indexOf("const addProfile"),
  );
  // Controlled Dropdown prop and the module session are updated before the RPC.
  assert.ok(body.includes("setActiveProfileId(profileId)"));
  assert.ok(body.includes("rememberActiveProfileId(profileId)"));
  const setIndex = body.indexOf("setActiveProfileId(profileId)");
  const rpcIndex = body.indexOf("regionProfileSelect(profileId)");
  assert.ok(setIndex >= 0 && rpcIndex > setIndex, "selection must precede the async RPC");
});
check("profile: stale controlled-state regression guard", () => {
  // Simulate: P1 -> + -> P2 -> + -> P3, then select P1.
  regionLogic.resetRegionEditorDraftState();
  regionLogic.rememberRegionEditorDraftState({
    active: true,
    activeProfileId: "p3",
    draftsProfileId: "p3",
    profiles: [
      { profile_id: "p1", label: "Profile 1" },
      { profile_id: "p2", label: "Profile 2" },
      { profile_id: "p3", label: "Profile 3" },
    ],
    maxProfiles: 8,
    configured: true,
    drafts: [{ region_id: "r3", x: 0.1, y: 0.1, w: 0.2, h: 0.2, enabled: true }],
    styleByRegion: {},
    fontByRegion: {},
  });
  // User selects Profile 1 (synchronous session update, as selectProfile does).
  regionLogic.rememberActiveProfileId("p1");
  const state = regionLogic.getRegionEditorDraftState();
  assert.equal(state.activeProfileId, "p1");
  assert.notEqual(state.activeProfileId, "p3");
  // The drafts still belong to P3 until the regions reload completes.
  assert.equal(state.draftsProfileId, "p3");
  assert.notEqual(state.draftsProfileId, state.activeProfileId);
  regionLogic.resetRegionEditorDraftState();
});
check("profile: remount reloads regions when drafts profile is stale", () => {
  assert.ok(regionComponentSrc.includes("initialDraft.draftsProfileId !== initialDraft.activeProfileId"));
  assert.ok(regionComponentSrc.includes("void loadActiveRegions()"));
});
check("profile: add/delete sync the authoritative ref", () => {
  assert.ok(regionComponentSrc.includes("activeProfileIdRef.current = nextProfile"));
  assert.ok(regionComponentSrc.includes("setActiveProfileId(nextProfile)"));
});
check("profile: selection failure resyncs from backend", () => {
  const body = regionComponentSrc.slice(
    regionComponentSrc.indexOf("const selectProfile"),
    regionComponentSrc.indexOf("const addProfile"),
  );
  assert.ok(body.includes("await load()"));
});
check("profile: operations never change the page", () => {
  for (const needle of ["setActiveId", "goToPage", "goPreviousPage", "goNextPage", "rememberQamPageSession"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
});
check("profile: selection does not toggle preview", () => {
  const body = regionComponentSrc.slice(
    regionComponentSrc.indexOf("const selectProfile"),
    regionComponentSrc.indexOf("const addProfile"),
  );
  assert.equal(body.includes("setPreviewOn"), false);
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
  assert.ok(regionComponentSrc.includes("Hide Region Preview"));
  assert.ok(regionComponentSrc.includes("[x] Enabled"));
  assert.ok(regionComponentSrc.includes('label="X%"'));
  assert.ok(regionComponentSrc.includes('label="Y%"'));
  assert.ok(regionComponentSrc.includes('label="W%"'));
  assert.ok(regionComponentSrc.includes('label="H%"'));
  assert.ok(regionComponentSrc.includes("panelStyleLabel"));
  assert.ok(regionComponentSrc.includes("regionFontSizeSet(selectedId, size)"));
});
check("region editor: primary + preview are stacked equal-width rows", () => {
  // Two consecutive full-width PanelSectionRow ButtonItems (no 2-col grid row).
  const primary = regionComponentSrc.indexOf('{selectedIsPrimary ? "Primary Region" : "Set as Primary"}');
  const preview = regionComponentSrc.indexOf('{previewOn ? "Hide Region Preview" : "Show Region Preview"}');
  assert.ok(primary >= 0 && preview > primary);
  const between = regionComponentSrc.slice(primary, preview);
  assert.ok(between.includes("</PanelSectionRow>"));
  assert.ok(between.includes("<PanelSectionRow>"));
  assert.ok(between.includes("</ButtonItem>"));
  assert.ok(between.includes("<ButtonItem"));
  assert.equal(between.includes("rowActionsStyle"), false);
});
check("region editor: dropdown selection does not mutate page state", () => {
  for (const needle of ["setActiveId", "goToPage", "goPreviousPage", "goNextPage", "activePage", "activeId"]) {
    assert.equal(regionComponentSrc.includes(needle), false, `region editor contains ${needle}`);
  }
});
check("region editor: no per-dropdown forced page navigation", () => {
  for (const needle of ['setPage("regions")', "setPage('regions')", 'setActiveTab("regions")', "onShowTab"]) {
    assert.equal(regionComponentSrc.includes(needle), false, needle);
  }
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
check("preview: component syncs store; explicit Show/Hide only", () => {
  assert.ok(regionComponentSrc.includes("setRegionPreview("));
  assert.ok(regionComponentSrc.includes("setRegionPreviewEnabled(previewOn)"));
  assert.ok(regionComponentSrc.includes("setRegionPreviewRegions("));
  // No visibility/unmount-driven preview teardown (that caused device resets).
  assert.equal(regionComponentSrc.includes("clearRegionPreview("), false);
  assert.equal(regionComponentSrc.includes("clearRegionPreviewRegions"), false);
  assert.equal(regionComponentSrc.includes("useQuickAccessVisible"), false);
});
check("preview: state is session-backed (survives remount)", () => {
  assert.ok(regionComponentSrc.includes("getRegionEditorSession().previewOn"));
  assert.ok(regionComponentSrc.includes("rememberRegionPreview(previewOn)"));
});
check("preview: explicit Show/Hide updates the session", () => {
  r.resetRegionEditorSession();
  assert.equal(r.getRegionEditorSession().previewOn, false);
  r.rememberRegionPreview(true);
  assert.equal(r.getRegionEditorSession().previewOn, true);
  r.rememberRegionPreview(false);
  assert.equal(r.getRegionEditorSession().previewOn, false);
  r.resetRegionEditorSession();
});
check("preview: follows selected region while ON", () => {
  const drafts = [R({ region_id: "a", x: 0.1 }), R({ region_id: "b", x: 0.5 })];
  const payloadA = r.regionPreviewPayload(drafts, "a");
  const payloadB = r.regionPreviewPayload(drafts, "b");
  assert.equal(payloadA[0].selected, true);
  assert.equal(payloadB[1].selected, true);
  assert.equal(payloadB[1].x, 0.5);
});
check("preview: dropdown selection does not toggle preview", () => {
  // The dropdown handlers only select/load; they never call setPreviewOn.
  const selectProfile = regionComponentSrc.slice(
    regionComponentSrc.indexOf("const selectProfile"),
    regionComponentSrc.indexOf("const addProfile"),
  );
  const selectRegion = regionComponentSrc.slice(
    regionComponentSrc.indexOf("const selectRegion"),
    regionComponentSrc.indexOf("const setPreviewOn"),
  );
  assert.equal(selectProfile.includes("setPreviewOn"), false);
  assert.equal(selectRegion.includes("setPreviewOn"), false);
});
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
