// Production OCR worker control. The backend remains the source of truth: this
// component issues explicit start/stop requests and polls a read-only status
// RPC. It never starts OCR on mount, QAM open, or polling, and closing the QAM
// never stops the worker.

import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { callable } from "@decky/api";
import { type CSSProperties, useEffect, useRef, useState } from "react";
import {
  DEFAULT_CHANGE_GATE,
  POLL_INTERVAL_MS,
  describeWorkerState,
  isRunning,
  isStartDisabled,
  isStopDisabled,
  mapOcrWorkerError,
  type OCRWorkerStatus,
} from "../ocrControl";

const startOcrWorker = callable<[changeGate: boolean], OCRWorkerStatus>("start_ocr_worker");
const stopOcrWorker = callable<[], OCRWorkerStatus>("stop_ocr_worker");
const getOcrWorkerStatus = callable<[], OCRWorkerStatus>("get_ocr_worker_status");

export function OCRControlSection() {
  const [status, setStatus] = useState<OCRWorkerStatus | undefined>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const refresh = async () => {
      try {
        const nextStatus = await getOcrWorkerStatus();
        if (mounted.current) {
          setStatus(nextStatus);
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

  const state = describeWorkerState(status);
  const running = isRunning(state);
  const startDisabled = isStartDisabled(state, busy);
  const stopDisabled = isStopDisabled(state, busy);
  const disabled = running ? stopDisabled : startDisabled;

  const start = async () => {
    if (startDisabled) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await startOcrWorker(DEFAULT_CHANGE_GATE);
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

  return (
    <PanelSection title="OCR">
      <PanelSectionRow>
        <div style={statusStyle}>Status: {state}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={disabled}
          onClick={() => (running ? void stop() : void start())}
        >
          {running ? "Stop OCR" : "Start OCR"}
        </ButtonItem>
      </PanelSectionRow>
      {error ? (
        <PanelSectionRow>
          <div style={errorStyle}>{error}</div>
        </PanelSectionRow>
      ) : null}
      {status?.last_error ? (
        <PanelSectionRow>
          <div style={errorStyle}>{status.last_error}</div>
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
}

const statusStyle: CSSProperties = {
  color: "#d9d9d9",
  fontSize: "13px",
  fontWeight: 600,
};

const errorStyle: CSSProperties = {
  color: "#ffb4b4",
  fontSize: "12px",
  overflowWrap: "anywhere",
};
