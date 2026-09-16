// Phase 2I.3 — pure OCR-diagnostic UI logic (no React, no @decky/api).
// Kept dependency-free so it can be unit-tested by a lightweight Node harness.

export type OCRWorkerTransport = {
  last_event_seq?: number;
  last_kind?: string | null;
  last_text?: string;
  last_confidence?: number | null;
  last_source_seq?: number | null;
  transport_messages_received?: number;
  transport_messages_rejected?: number;
  transport_out_of_order?: number;
  transport_text_events?: number;
  transport_clear_events?: number;
};

export type OCRWorkerStatus = {
  ok?: boolean;
  state?: string;
  pid?: number | null;
  worker_session_id?: string | null;
  change_gate_enabled?: boolean;
  exit_code?: number | null;
  last_error?: string | null;
  transport?: OCRWorkerTransport;
  error?: string;
  detail?: string;
};

export type LatestStableText = {
  ok?: boolean;
  worker_session_id?: string | null;
  last_event_seq?: number;
  kind?: string | null;
  text?: string;
  confidence?: number | null;
  source_seq?: number | null;
  error?: string;
};

// Phase 2I.3.2 temporary capture-conflict diagnostic status.
export type CaptureProducerStatus = {
  ok?: boolean;
  state?: string;
  target_fps?: number | null;
  frames_attempted?: number;
  frames_succeeded?: number;
  frames_failed?: number;
  last_error?: string | null;
  last_error_category?: string | null;
  error?: string;
  detail?: string;
};

export const DEFAULT_CHANGE_GATE = false;
export const POLL_INTERVAL_MS = 1000;
export const WORKER_STATES = ["STOPPED", "STARTING", "RUNNING", "FAILED", "STOPPING"] as const;
// Temporary Phase 2I.3.2 capture diagnostic cadence: explicit 1 FPS only.
export const CAPTURE_DIAGNOSTIC_FPS = 1.0;

const ERROR_MESSAGES: Record<string, string> = {
  not_leader: "OCR worker can only be started by the backend leader.",
  capture_conflict: "Stop the backend capture diagnostic before starting OCR.",
  ocr_worker_unavailable: "OCR worker backend is unavailable.",
  model_missing: "OCR model files are missing.",
  forbidden_interpreter: "Unsafe Python interpreter was rejected.",
  invalid_parent_pid: "OCR worker rejected the backend parent pid.",
  disconnected: "Backend unavailable (plugin reloading?).",
};

export function mapOcrWorkerError(code?: string | null, detail?: string | null): string {
  if (!code) {
    return detail ? `OCR error: ${detail}` : "OCR request failed.";
  }
  const known = ERROR_MESSAGES[code];
  if (known) {
    return known;
  }
  return detail ? `${code}: ${detail}` : `OCR error: ${code}`;
}

export function renderStableText(latest?: LatestStableText | null): string {
  if (!latest) {
    return "(waiting for stable text)";
  }
  if (latest.kind === "text" && typeof latest.text === "string" && latest.text.length > 0) {
    return latest.text;
  }
  if (latest.kind === "clear") {
    return "(no stable text)";
  }
  return "(waiting for stable text)";
}

export function eventIdentity(latest?: LatestStableText | null): string | null {
  if (!latest || !latest.worker_session_id) {
    return null;
  }
  if (typeof latest.last_event_seq !== "number" || latest.last_event_seq <= 0) {
    return null;
  }
  return `${latest.worker_session_id}:${latest.last_event_seq}`;
}

export function describeWorkerState(status?: OCRWorkerStatus | null): string {
  if (!status || !status.state) {
    return "STOPPED";
  }
  return status.state;
}

export function shortSessionId(sessionId?: string | null): string {
  if (!sessionId) {
    return "-";
  }
  return sessionId.length > 8 ? sessionId.slice(0, 8) : sessionId;
}

export function isStartDisabled(state: string, busy: boolean): boolean {
  return busy || state === "STARTING" || state === "RUNNING" || state === "STOPPING";
}

export function isStopDisabled(state: string, busy: boolean): boolean {
  return busy || state === "STOPPED";
}

export function isChangeGateToggleDisabled(state: string): boolean {
  // the toggle only affects the next explicit start; lock it while RUNNING
  return state === "RUNNING" || state === "STARTING";
}

// -- Phase 2I.3.2 temporary capture diagnostic helpers -----------------------

export function describeCaptureState(status?: CaptureProducerStatus | null): string {
  if (!status || !status.state) {
    return "STOPPED";
  }
  return status.state;
}

export function isCaptureStartDisabled(state: string, busy: boolean): boolean {
  return busy || state === "STARTING" || state === "RUNNING" || state === "STOPPING";
}

export function isCaptureStopDisabled(state: string, busy: boolean): boolean {
  return busy || state === "STOPPED";
}

export function renderCaptureStatus(status?: CaptureProducerStatus | null): string {
  const state = describeCaptureState(status);
  const fps = typeof status?.target_fps === "number" ? `${status.target_fps} fps` : "fps -";
  const succeeded = status?.frames_succeeded ?? 0;
  const failed = status?.frames_failed ?? 0;
  const base = `${state} | ${fps} | ok ${succeeded} | failed ${failed}`;
  if (state === "FAILED" || status?.error) {
    const detail = status?.last_error ?? status?.error ?? "capture producer error";
    return `${base} | ${detail}`;
  }
  return base;
}

export function mapCaptureError(code?: string | null, detail?: string | null): string {
  if (code === "producer_unavailable") {
    return "Capture producer backend is unavailable.";
  }
  if (code === "not_leader") {
    return "Capture producer can only run on the backend leader.";
  }
  if (!code) {
    return detail ? `Capture error: ${detail}` : "Capture request failed.";
  }
  return detail ? `${code}: ${detail}` : `Capture error: ${code}`;
}
