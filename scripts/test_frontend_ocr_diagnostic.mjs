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

async function loadLogic() {
  const source = fs.readFileSync(LOGIC, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const tmp = path.join(os.tmpdir(), `clarifydeck-ocrdiag-${process.pid}.mjs`);
  fs.writeFileSync(tmp, output, "utf8");
  try {
    return await import(url.pathToFileURL(tmp).href);
  } finally {
    fs.unlinkSync(tmp);
  }
}

const logic = await loadLogic();
const componentSrc = fs.readFileSync(COMPONENT, "utf8");
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

if (process.exitCode) {
  console.error(`\nfrontend OCR diagnostic harness FAILED (${passed} passed)`);
} else {
  console.log(`\nfrontend OCR diagnostic harness PASSED (${passed} checks)`);
}
