// Phase 2I.3 — QAM diagnostic OCR control + live stable text.
// Backend remains the source of truth: this component only issues explicit
// start/stop requests and polls read-only RPCs. It never starts OCR on mount,
// QAM open, or polling, and closing QAM never stops the worker.

import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { callable } from "@decky/api";
import { type CSSProperties, useEffect, useRef, useState } from "react";
import {
  CAPTURE_DIAGNOSTIC_FPS,
  DEFAULT_CHANGE_GATE,
  POLL_INTERVAL_MS,
  describeCaptureState,
  describeWorkerState,
  isCaptureStartDisabled,
  isCaptureStopDisabled,
  isChangeGateToggleDisabled,
  isStartDisabled,
  isStopDisabled,
  mapCaptureError,
  mapOcrWorkerError,
  renderCaptureStatus,
  renderStableText,
  shortSessionId,
  type CaptureProducerStatus,
  type LatestStableText,
  type OCRWorkerStatus,
} from "../ocrDiagnostic";

const startOcrWorker = callable<[changeGate: boolean], OCRWorkerStatus>("start_ocr_worker");
const stopOcrWorker = callable<[], OCRWorkerStatus>("stop_ocr_worker");
const getOcrWorkerStatus = callable<[], OCRWorkerStatus>("get_ocr_worker_status");
const getLatestStableText = callable<[], LatestStableText>("get_latest_stable_text");

// Temporary Phase 2I.3.2 diagnostic controls (capture-conflict validation only).
// Explicit user presses only: never started on mount/QAM open/polling, and never
// stopped on QAM close. Uses the existing capture producer RPCs; no backend change.
const startCaptureProducer = callable<[targetFps: number], CaptureProducerStatus>("capture_producer_start");
const stopCaptureProducer = callable<[], CaptureProducerStatus>("capture_producer_stop");
const getCaptureProducerStatus = callable<[], CaptureProducerStatus>("capture_producer_status");

export function OCRDiagnosticSection() {
  const [status, setStatus] = useState<OCRWorkerStatus | undefined>();
  const [latest, setLatest] = useState<LatestStableText | undefined>();
  const [changeGate, setChangeGate] = useState<boolean>(DEFAULT_CHANGE_GATE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [capture, setCapture] = useState<CaptureProducerStatus | undefined>();
  const [captureBusy, setCaptureBusy] = useState(false);
  const [captureError, setCaptureError] = useState("");
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const refresh = async () => {
      try {
        const nextStatus = await getOcrWorkerStatus();
        const nextLatest = await getLatestStableText();
        const nextCapture = await getCaptureProducerStatus();
        if (mounted.current) {
          setStatus(nextStatus);
          setLatest(nextLatest);
          setCapture(nextCapture);
          setError("");
        }
      } catch (err) {
        if (mounted.current) {
          setError(mapOcrWorkerError("disconnected", String(err)));
        }
      }
    };
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, []);

  const workerState = describeWorkerState(status);
  const running = workerState === "RUNNING";
  const startDisabled = isStartDisabled(workerState, busy);
  const stopDisabled = isStopDisabled(workerState, busy);
  const gateDisabled = isChangeGateToggleDisabled(workerState);

  const start = async () => {
    if (startDisabled) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await startOcrWorker(changeGate);
      if (result && result.ok === false) {
        setError(mapOcrWorkerError(result.error, result.detail));
      }
      if (mounted.current) {
        setStatus(result);
      }
    } catch (err) {
      setError(mapOcrWorkerError("start_failed", String(err)));
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  const stop = async () => {
    if (stopDisabled) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await stopOcrWorker();
      if (result && result.ok === false) {
        setError(mapOcrWorkerError(result.error, result.detail));
      }
      if (mounted.current) {
        setStatus(result);
      }
    } catch (err) {
      setError(mapOcrWorkerError("stop_failed", String(err)));
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  const captureState = describeCaptureState(capture);
  const captureStartDisabled = isCaptureStartDisabled(captureState, captureBusy);
  const captureStopDisabled = isCaptureStopDisabled(captureState, captureBusy);

  const startCapture = async () => {
    if (captureStartDisabled) {
      return;
    }
    setCaptureBusy(true);
    setCaptureError("");
    try {
      const result = await startCaptureProducer(CAPTURE_DIAGNOSTIC_FPS);
      if (result && result.ok === false) {
        setCaptureError(mapCaptureError(result.error, result.detail));
      }
      if (mounted.current) {
        setCapture(result);
      }
    } catch (err) {
      setCaptureError(mapCaptureError("start_failed", String(err)));
    } finally {
      if (mounted.current) {
        setCaptureBusy(false);
      }
    }
  };

  const stopCapture = async () => {
    if (captureStopDisabled) {
      return;
    }
    setCaptureBusy(true);
    setCaptureError("");
    try {
      const result = await stopCaptureProducer();
      if (result && result.ok === false) {
        setCaptureError(mapCaptureError(result.error, result.detail));
      }
      if (mounted.current) {
        setCapture(result);
      }
    } catch (err) {
      setCaptureError(mapCaptureError("stop_failed", String(err)));
    } finally {
      if (mounted.current) {
        setCaptureBusy(false);
      }
    }
  };

  const transport = status?.transport;

  return (
    <PanelSection title="OCR Diagnostic">
      <PanelSectionRow>
        <div style={statusStyle}>
          <div>
            Worker: {workerState}
            {status?.pid ? ` (pid ${status.pid})` : ""}
          </div>
          <div>
            Session: {shortSessionId(status?.worker_session_id)} | Event #
            {transport?.last_event_seq ?? 0}
          </div>
          <div>
            Change gate: {changeGate ? "on" : "off"}
            {running ? " (applies on next start)" : ""}
          </div>
          {error ? <div style={errorStyle}>{error}</div> : null}
          {status?.last_error ? <div style={errorStyle}>{status.last_error}</div> : null}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={rowActionsStyle}>
          <ButtonItem layout="below" disabled={startDisabled} onClick={start}>
            {running ? "Running" : "Start OCR"}
          </ButtonItem>
          <ButtonItem layout="below" disabled={stopDisabled} onClick={stop}>
            Stop OCR
          </ButtonItem>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={gateDisabled}
          onClick={() => setChangeGate((value) => !value)}
        >
          {changeGate ? "[x] Use change-gated OCR" : "[ ] Use change-gated OCR"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={statusStyle}>
          <div>Stable text:</div>
          <div style={stableTextStyle}>{renderStableText(latest)}</div>
          <div>
            Confidence: {latest?.confidence ?? "-"} | source seq: {latest?.source_seq ?? "-"}
          </div>
          <div>
            Received: {transport?.transport_messages_received ?? 0} | rejected:{" "}
            {transport?.transport_messages_rejected ?? 0} | out-of-order:{" "}
            {transport?.transport_out_of_order ?? 0}
          </div>
        </div>
      </PanelSectionRow>
      <PanelSection title="Capture Diagnostic (Temporary)">
        <PanelSectionRow>
          <div style={statusStyle}>
            <div>Capture diagnostic state: {captureState}</div>
            <div>{renderCaptureStatus(capture)}</div>
            {capture?.last_error ? <div style={errorStyle}>{capture.last_error}</div> : null}
            {captureError ? <div style={errorStyle}>{captureError}</div> : null}
            <div style={hintStyle}>Temporary test control for OCR capture-conflict validation.</div>
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={rowActionsStyle}>
            <ButtonItem layout="below" disabled={captureStartDisabled} onClick={startCapture}>
              Start Capture Diagnostic
            </ButtonItem>
            <ButtonItem layout="below" disabled={captureStopDisabled} onClick={stopCapture}>
              Stop Capture Diagnostic
            </ButtonItem>
          </div>
        </PanelSectionRow>
      </PanelSection>
    </PanelSection>
  );
}

const rowActionsStyle: CSSProperties = {
  display: "grid",
  gap: "8px",
  gridTemplateColumns: "1fr 1fr",
  width: "100%",
};

const statusStyle: CSSProperties = {
  color: "#d9d9d9",
  display: "grid",
  fontSize: "12px",
  gap: "4px",
  lineHeight: 1.35,
};

const errorStyle: CSSProperties = {
  color: "#ffb4b4",
  overflowWrap: "anywhere",
};

const hintStyle: CSSProperties = {
  color: "#cfcfcf",
  fontSize: "11px",
  lineHeight: 1.35,
};

const stableTextStyle: CSSProperties = {
  background: "rgba(0, 0, 0, 0.35)",
  borderRadius: "6px",
  color: "#f5f5f5",
  padding: "6px 8px",
  whiteSpace: "pre-wrap",
};
