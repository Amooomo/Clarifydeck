// Production OCR worker control pure logic (no React, no @decky/api).
// Kept dependency-free so the lightweight Node harness can unit-test it.

export type OCRWorkerStatus = {
  ok?: boolean;
  state?: string;
  last_error?: string | null;
  error?: string;
  detail?: string;
};

// Production OCR always starts with the change gate disabled.
export const DEFAULT_CHANGE_GATE = false;
export const POLL_INTERVAL_MS = 1000;

const ERROR_MESSAGES: Record<string, string> = {
  not_leader: "OCR worker can only be started by the backend leader.",
  capture_conflict: "Stop the backend capture producer before starting OCR.",
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

export function describeWorkerState(status?: OCRWorkerStatus | null): string {
  if (!status || !status.state) {
    return "STOPPED";
  }
  return status.state;
}

export function isRunning(state: string): boolean {
  return state === "RUNNING";
}

export function isStartDisabled(state: string, busy: boolean): boolean {
  return busy || state === "STARTING" || state === "RUNNING" || state === "STOPPING";
}

export function isStopDisabled(state: string, busy: boolean): boolean {
  return busy || state !== "RUNNING";
}
