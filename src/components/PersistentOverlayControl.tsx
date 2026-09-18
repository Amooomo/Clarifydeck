// Production persistent overlay control. Uses the existing proven backend RPC.
// It never starts OCR and never enables the overlay on mount or QAM open.

import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { callable } from "@decky/api";
import { type CSSProperties, useEffect, useRef, useState } from "react";

type OverlayStatus = {
  ok?: boolean;
  enabled: boolean;
  state: string;
  last_error: string | null;
  error?: string;
  detail?: string;
};

type BackendStatus = {
  overlay?: OverlayStatus | null;
  last_error?: string;
};

const getStatus = callable<[], BackendStatus>("get_status");
const setOverlayEnabled = callable<[enabled: boolean], OverlayStatus>("set_overlay_enabled");

const POLL_INTERVAL_MS = 3000;

export function PersistentOverlaySection() {
  const [status, setStatus] = useState<BackendStatus | undefined>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const refresh = async () => {
      try {
        const nextStatus = await getStatus();
        if (mounted.current) {
          setStatus(nextStatus);
          setError("");
        }
      } catch (err) {
        if (mounted.current) {
          setError(`Overlay status unavailable: ${String(err)}`);
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

  const overlay = status?.overlay;
  const enabled = overlay?.enabled ?? false;
  const state = overlay?.state ?? "DISABLED";
  const overlayError = overlay?.last_error || status?.last_error || "";

  const toggle = async () => {
    if (busy) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await setOverlayEnabled(!enabled);
      if (result && result.ok === false) {
        setError(result.detail || result.error || "Overlay request failed");
      }
      const nextStatus = await getStatus();
      if (mounted.current) {
        setStatus(nextStatus);
      }
    } catch (err) {
      setError(`Overlay request failed: ${String(err)}`);
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  return (
    <PanelSection title="Overlay">
      <PanelSectionRow>
        <div style={statusStyle}>Status: {state}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={() => void toggle()}>
          {enabled ? "Disable Overlay" : "Enable Overlay"}
        </ButtonItem>
      </PanelSectionRow>
      {error ? (
        <PanelSectionRow>
          <div style={errorStyle}>{error}</div>
        </PanelSectionRow>
      ) : null}
      {overlayError ? (
        <PanelSectionRow>
          <div style={errorStyle}>{overlayError}</div>
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
