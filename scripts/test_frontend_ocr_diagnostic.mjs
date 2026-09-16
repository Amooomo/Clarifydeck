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

function blockAfter(source, marker) {
  const start = source.indexOf(marker);
  if (start < 0) {
    return "";
  }
  let depth = 0;
  let opened = false;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") {
      depth += 1;
      opened = true;
    } else if (char === "}") {
      depth -= 1;
      if (opened && depth === 0) {
        return source.slice(start, index + 1);
      }
    }
  }
  return source.slice(start);
}

// -- pure logic --------------------------------------------------------------

check("error: not_leader", () =>
  assert.equal(logic.mapOcrWorkerError("not_leader"), "OCR worker can only be started by the backend leader."),
);
check("error: capture_conflict", () =>
  assert.equal(logic.mapOcrWorkerError("capture_conflict"), "Stop the backend capture diagnostic before starting OCR."),
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

// -- Phase 2I.3.2 temporary capture diagnostic controls ----------------------

check("capture: state helper defaults STOPPED", () =>
  assert.equal(logic.describeCaptureState(undefined), "STOPPED"),
);
check("capture: RUNNING state renders", () => {
  const rendered = logic.renderCaptureStatus({ state: "RUNNING", target_fps: 1.0, frames_succeeded: 3, frames_failed: 0 });
  assert.ok(rendered.includes("RUNNING"));
  assert.ok(rendered.includes("1 fps"));
});
check("capture: STOPPED state renders", () => {
  const rendered = logic.renderCaptureStatus({ state: "STOPPED", target_fps: null, frames_succeeded: 0, frames_failed: 0 });
  assert.ok(rendered.includes("STOPPED"));
});
check("capture: FAILED state renders concise diagnostic text", () => {
  const rendered = logic.renderCaptureStatus({ state: "FAILED", last_error: "worker_exit code=1" });
  assert.ok(rendered.includes("FAILED"));
  assert.ok(rendered.includes("worker_exit code=1"));
});
check("capture: error status renders detail", () => {
  const rendered = logic.renderCaptureStatus({ state: "STOPPED", error: "producer_unavailable" });
  assert.ok(rendered.includes("producer_unavailable"));
});
check("capture: diagnostic cadence is 1 FPS", () => assert.equal(logic.CAPTURE_DIAGNOSTIC_FPS, 1.0));
check("capture: duplicate start blocked while busy", () => {
  assert.equal(logic.isCaptureStartDisabled("STOPPED", true), true);
  assert.equal(logic.isCaptureStartDisabled("RUNNING", false), true);
  assert.equal(logic.isCaptureStartDisabled("STOPPED", false), false);
});
check("capture: duplicate stop blocked while busy", () => {
  assert.equal(logic.isCaptureStopDisabled("RUNNING", true), true);
  assert.equal(logic.isCaptureStopDisabled("STOPPED", false), true);
  assert.equal(logic.isCaptureStopDisabled("RUNNING", false), false);
});
check("capture: error mapping for unavailable producer", () =>
  assert.equal(logic.mapCaptureError("producer_unavailable"), "Capture producer backend is unavailable."),
);

check("capture: RPC declared once each", () => {
  assert.equal(countOccurrences(componentSrc, '"capture_producer_start"'), 1);
  assert.equal(countOccurrences(componentSrc, '"capture_producer_stop"'), 1);
  assert.equal(countOccurrences(componentSrc, '"capture_producer_status"'), 1);
});
check("capture: mount/effect does not start capture", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.length > 0, "useEffect block not found");
  assert.equal(body.includes("startCaptureProducer"), false);
});
check("capture: unmount does not stop capture", () => {
  const body = effectBlock(componentSrc);
  assert.equal(body.includes("stopCaptureProducer"), false);
});
check("capture: polling calls only read-only capture status", () => {
  const body = effectBlock(componentSrc);
  assert.ok(body.includes("getCaptureProducerStatus"));
  assert.equal(body.includes("startCaptureProducer"), false);
  assert.equal(body.includes("stopCaptureProducer"), false);
  assert.ok(body.includes("clearInterval"));
});
check("capture: start RPC called exactly once with 1.0 fps", () => {
  assert.equal(countOccurrences(componentSrc, "startCaptureProducer"), 2); // declaration + one call
  assert.ok(componentSrc.includes("startCaptureProducer(CAPTURE_DIAGNOSTIC_FPS)"));
});
check("capture: stop RPC called exactly once", () => {
  assert.equal(countOccurrences(componentSrc, "stopCaptureProducer"), 2); // declaration + one call
  assert.ok(componentSrc.includes("stopCaptureProducer()"));
});
check("capture: explicit start handler guards duplicate presses", () => {
  const handler = blockAfter(componentSrc, "const startCapture = async () => {");
  assert.ok(handler.includes("captureStartDisabled"));
  assert.ok(handler.includes("startCaptureProducer("));
});
check("capture: explicit stop handler guards duplicate presses", () => {
  const handler = blockAfter(componentSrc, "const stopCapture = async () => {");
  assert.ok(handler.includes("captureStopDisabled"));
  assert.ok(handler.includes("stopCaptureProducer("));
});
check("capture: OCR start remains unchanged and issues backend request", () => {
  assert.equal(countOccurrences(componentSrc, "startOcrWorker("), 1);
  const handler = blockAfter(componentSrc, "const start = async () => {");
  assert.equal(handler.includes("capture"), false, "OCR start handler must not be capture-gated");
  assert.ok(handler.includes("startOcrWorker("));
});
check("capture: frontend does not synthesize capture_conflict", () => {
  assert.equal(componentSrc.includes("capture_conflict"), false);
  const handler = blockAfter(componentSrc, "const start = async () => {");
  assert.ok(handler.includes("result.error"), "OCR start must surface backend error");
});
check("capture: no auto-start path in component", () => {
  const body = effectBlock(componentSrc);
  assert.equal(body.includes("startCaptureProducer"), false);
  assert.equal(componentSrc.includes("setInterval(startCapture"), false);
});

if (process.exitCode) {
  console.error(`\nfrontend OCR diagnostic harness FAILED (${passed} passed)`);
} else {
  console.log(`\nfrontend OCR diagnostic harness PASSED (${passed} checks)`);
}
