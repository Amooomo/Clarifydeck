
import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  findModule,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  routerHook,
  toaster,
  useQuickAccessVisible,
} from "@decky/api";
import { type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { FaSearchPlus } from "react-icons/fa";
import { OCRDiagnosticSection } from "./components/OCRDiagnostic";

type BoxState = {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  text: string;
  last_mse?: number | null;
  last_ocr_at?: number;
};

type OcrBroadcast = {
  id: string;
  text: string;
  box?: BoxState;
};

type OverlayStatus = {
  enabled: boolean;
  state: string;
  display: string;
  socket: string | null;
  renderer_pid: number | null;
  connected: boolean;
  visible: boolean;
  last_error: string | null;
};

type BackendStatus = {
  enabled: boolean;
  box_count: number;
  capture_running: boolean;
  last_error: string;
  last_capture_at: number;
  last_ocr_at: number;
  ocr_lang: string;
  ocr_scale?: number;
  ocr_invert?: boolean;
  ocr_psm?: number;
  mse_threshold: number;
  tesseract: string;
  tessdata?: string;
  ocr_langs?: string[];
  screen_width: number;
  screen_height: number;
  frame_cached?: boolean;
  overlay?: OverlayStatus | null;
  backend?: { pid: number; role: string };
};

type OcrTestResult = {
  text: string | null;
  error: string;
  crop_path?: string | null;
};

type RecognitionROI = {
  x: number;
  y: number;
  width: number;
  height: number;
};

type RecognitionROIResult = {
  ok: boolean;
  app_id?: string | null;
  source?: string;
  roi?: RecognitionROI;
  pixel?: { x: number; y: number; width: number; height: number };
  frame?: { width: number; height: number };
  config_path?: string;
  config_error?: string | null;
  error?: string;
  detail?: string;
};

type RoiDraft = { x: number; y: number; width: number; height: number };

const ROI_PRESETS: { label: string; roi: RoiDraft }[] = [
  { label: "Bottom 20%", roi: { x: 8, y: 70, width: 84, height: 20 } },
  { label: "Bottom 30%", roi: { x: 8, y: 62, width: 84, height: 32 } },
  { label: "Center", roi: { x: 20, y: 30, width: 60, height: 40 } },
  { label: "Full frame", roi: { x: 0, y: 0, width: 100, height: 100 } },
];

function validateRoiDraft(draft: RoiDraft): string {
  const roi = { x: draft.x / 100, y: draft.y / 100, width: draft.width / 100, height: draft.height / 100 };
  if (!Number.isFinite(roi.x) || !Number.isFinite(roi.y) || !Number.isFinite(roi.width) || !Number.isFinite(roi.height)) {
    return "Values must be numbers";
  }
  if (roi.x < 0 || roi.y < 0) {
    return "X and Y must be 0% or greater";
  }
  if (roi.width < 0.02 || roi.height < 0.02) {
    return "Width and height must be at least 2%";
  }
  if (roi.x + roi.width > 1.0001 || roi.y + roi.height > 1.0001) {
    return "Area must stay inside the frame";
  }
  return "";
}

const LANGUAGES = [
  { value: "chi_sim+eng", label: "中英 chi_sim+eng" },
  { value: "chi_sim", label: "简体 chi_sim" },
  { value: "chi_tra", label: "繁体 chi_tra" },
  { value: "eng", label: "英文 eng" },
];

const listBoxes = callable<[], BoxState[]>("list_boxes");
const addBox = callable<[], BoxState>("add_box");
const updateBox = callable<
  [boxId: string, x: number, y: number, w: number, h: number],
  BoxState | null
>("update_box");
const removeBox = callable<[boxId: string], boolean>("remove_box");
const startPlugin = callable<[], BackendStatus>("start_plugin");
const stopPlugin = callable<[], BackendStatus>("stop_plugin");
const getStatus = callable<[], BackendStatus>("get_status");
const setOcrLang = callable<[lang: string], BackendStatus>("set_ocr_lang");
const setOcrOptions = callable<
  [invert: boolean, psm: number, scale: number],
  BackendStatus
>("set_ocr_options");
const runOcrNow = callable<[boxId: string], OcrTestResult | null>("run_ocr_now");
const setOverlayEnabled = callable<[enabled: boolean], OverlayStatus>("set_overlay_enabled");
const roiConfigGet = callable<[appId: string | null], RecognitionROIResult>("roi_config_get");
const roiConfigSet = callable<[roi: RecognitionROI, appId: string | null], RecognitionROIResult>("roi_config_set");
const roiConfigReset = callable<[appId: string | null], RecognitionROIResult>("roi_config_reset");

// The persistent Gamescope external overlay renderer (backend) owns caption
// display. The frontend keeps only the QAM-open region preview needed to position
// the OCR ROI.
let globalSelectedBoxId: string | undefined;
const selectionEvents = new EventTarget();

let overlayMounted = false;
let overlayMountMethod = "none";
let overlayBoxCount = 0;
const overlayEvents = new EventTarget();

function setOverlayMounted(value: boolean, boxCount = 0) {
  overlayMounted = value;
  overlayBoxCount = boxCount;
  overlayEvents.dispatchEvent(new Event("clarifydeck-overlay"));
}

function useOverlayMounted() {
  const [state, setState] = useState({
    mounted: overlayMounted,
    method: overlayMountMethod,
    boxes: overlayBoxCount,
  });
  useEffect(() => {
    const handler = () =>
      setState({ mounted: overlayMounted, method: overlayMountMethod, boxes: overlayBoxCount });
    overlayEvents.addEventListener("clarifydeck-overlay", handler);
    return () => overlayEvents.removeEventListener("clarifydeck-overlay", handler);
  }, []);
  return state;
}

let globalOverlayViewport = { w: 0, h: 0 };
const viewportEvents = new EventTarget();

function setGlobalOverlayViewport(w: number, h: number) {
  globalOverlayViewport = { w, h };
  viewportEvents.dispatchEvent(new Event("clarifydeck-viewport"));
}

function useOverlayViewport() {
  const [viewport, setViewport] = useState(globalOverlayViewport);
  useEffect(() => {
    const handler = () => setViewport(globalOverlayViewport);
    viewportEvents.addEventListener("clarifydeck-viewport", handler);
    return () => viewportEvents.removeEventListener("clarifydeck-viewport", handler);
  }, []);
  return viewport;
}

type ReactRootLike = { render: (node: ReactNode) => void; unmount: () => void };
type CreateRootFn = (element: Element) => ReactRootLike;

function resolveCreateRoot(): CreateRootFn | undefined {
  const reactDom = (
    window as unknown as {
      SP_REACTDOM?: {
        createRoot?: CreateRootFn;
        render?: (node: ReactNode, element: Element) => void;
        unmountComponentAtNode?: (element: Element) => void;
      };
    }
  ).SP_REACTDOM;
  if (typeof reactDom?.createRoot === "function") {
    return reactDom.createRoot.bind(reactDom) as CreateRootFn;
  }
  try {
    const client = findModule(
      (module: { createRoot?: unknown; hydrateRoot?: unknown }) =>
        typeof module?.createRoot === "function" && typeof module?.hydrateRoot === "function",
    ) as { createRoot?: CreateRootFn } | undefined;
    if (typeof client?.createRoot === "function") {
      console.log("ClarifyDeck using react-dom/client createRoot");
      return client.createRoot.bind(client) as CreateRootFn;
    }
  } catch (error) {
    console.warn("ClarifyDeck createRoot lookup failed", error);
  }
  if (reactDom?.render && reactDom?.unmountComponentAtNode) {
    return (element: Element) => ({
      render: (node: ReactNode) => reactDom.render?.(node, element),
      unmount: () => reactDom.unmountComponentAtNode?.(element),
    });
  }
  return undefined;
}

function mountOverlay(): () => void {
  const createRoot = resolveCreateRoot();
  if (createRoot) {
    const container = document.createElement("div");
    container.id = "clarifydeck-overlay-root";
    container.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
    document.body.appendChild(container);

    const root = createRoot(container);
    root.render(<Overlay />);
    overlayMountMethod = "createRoot";
    const keepAlive = window.setInterval(() => {
      if (!container.isConnected) {
        document.body.appendChild(container);
      }
    }, 1000);
    return () => {
      window.clearInterval(keepAlive);
      try {
        root.unmount();
      } catch (error) {
        console.warn("ClarifyDeck overlay unmount failed", error);
      }
      container.remove();
    };
  }
  overlayMountMethod = "routerHook";
  routerHook.addGlobalComponent("ClarifyDeckOverlay", Overlay);
  return () => routerHook.removeGlobalComponent("ClarifyDeckOverlay");
}

function setGlobalSelectedBoxId(boxId: string | undefined) {
  globalSelectedBoxId = boxId;
  selectionEvents.dispatchEvent(new CustomEvent<string | undefined>("clarifydeck-selection", { detail: boxId }));
}

function useSelectedBoxId() {
  const [selectedBoxId, setSelectedBoxId] = useState<string | undefined>(globalSelectedBoxId);

  useEffect(() => {
    const onSelection = (event: Event) => {
      setSelectedBoxId((event as CustomEvent<string | undefined>).detail);
    };
    selectionEvents.addEventListener("clarifydeck-selection", onSelection);
    return () => selectionEvents.removeEventListener("clarifydeck-selection", onSelection);
  }, []);

  return selectedBoxId;
}

function useClarifyDeckState(options: { pollStatus?: boolean } = {}) {
  const pollStatus = options.pollStatus ?? true;
  const [boxes, setBoxes] = useState<BoxState[]>([]);
  const [status, setStatus] = useState<BackendStatus | undefined>();

  const refresh = async () => {
    try {
      const freshBoxes = await listBoxes();
      setBoxes(freshBoxes);
      if (pollStatus) {
        setStatus(await getStatus());
      }
    } catch (error) {
      console.warn("ClarifyDeck failed to refresh backend state", error);
    }
  };

  useEffect(() => {
    void refresh();

    const boxesListener = addEventListener<[boxes: BoxState[]]>(
      "boxes_changed",
      (freshBoxes) => setBoxes(freshBoxes),
    );
    const ocrListener = addEventListener<[payload: OcrBroadcast]>(
      "ocr_broadcast",
      (payload) => {
        setBoxes((currentBoxes) => {
          const found = currentBoxes.some((box) => box.id === payload.id);
          if (!found && payload.box) {
            return [...currentBoxes, payload.box];
          }
          return currentBoxes.map((box) => {
            if (box.id !== payload.id) {
              return box;
            }
            return payload.box ?? { ...box, text: payload.text };
          });
        });
      },
    );

    const interval = window.setInterval(() => {
      void refresh();
    }, pollStatus ? 3000 : 500);

    return () => {
      window.clearInterval(interval);
      removeEventListener("boxes_changed", boxesListener);
      removeEventListener("ocr_broadcast", ocrListener);
    };
  }, [pollStatus]);

  return { boxes, setBoxes, status, refresh };
}

function Content() {
  const { boxes, setBoxes, status, refresh } = useClarifyDeckState();
  const [selectedId, setSelectedIdState] = useState<string | undefined>(globalSelectedBoxId);
  const setSelectedId = (boxId: string | undefined) => {
    setSelectedIdState(boxId);
    setGlobalSelectedBoxId(boxId);
  };
  const selectedBox = useMemo(
    () => boxes.find((box) => box.id === selectedId) ?? boxes[0],
    [boxes, selectedId],
  );
  const sliderMaxWidth = status?.screen_width ?? 1920;
  const sliderMaxHeight = status?.screen_height ?? 1200;
  const overlayState = useOverlayMounted();
  const overlayViewport = useOverlayViewport();
  const [roiDraft, setRoiDraft] = useState<RoiDraft>({ x: 8, y: 62, width: 84, height: 32 });
  const [roiSource, setRoiSource] = useState<string>("default");
  const [roiError, setRoiError] = useState<string>("");

  useEffect(() => {
    if (boxes.length === 0) {
      setSelectedId(undefined);
      return;
    }
    if (!selectedId || !boxes.some((box) => box.id === selectedId)) {
      setSelectedId(boxes[0].id);
    }
  }, [boxes, selectedId]);

  const createBox = async () => {
    try {
      const created = await addBox();
      setSelectedId(created.id);
      await refresh();
      toaster.toast({ title: "ClarifyDeck", body: "OCR region added" });
    } catch (error) {
      console.warn("ClarifyDeck failed to add region", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to add OCR region" });
    }
  };

  const deleteSelectedBox = async () => {
    if (!selectedBox) {
      return;
    }
    try {
      const removed = await removeBox(selectedBox.id);
      if (removed) {
        setSelectedId(undefined);
        await refresh();
      }
    } catch (error) {
      console.warn("ClarifyDeck failed to remove region", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to remove OCR region" });
    }
  };

  const togglePlugin = async () => {
    try {
      const nextStatus = status?.enabled ? await stopPlugin() : await startPlugin();
      toaster.toast({
        title: "ClarifyDeck",
        body: nextStatus.enabled ? "OCR capture started" : "OCR capture stopped",
      });
      await refresh();
    } catch (error) {
      console.warn("ClarifyDeck failed to toggle OCR capture", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to toggle OCR capture" });
    }
  };

  const toggleOverlay = async () => {
    const next = !(status?.overlay?.enabled ?? false);
    try {
      const result = await setOverlayEnabled(next);
      toaster.toast({
        title: "ClarifyDeck",
        body: `Persistent overlay: ${result.state}${result.last_error ? ` (${result.last_error})` : ""}`,
      });
      await refresh();
    } catch (error) {
      console.warn("ClarifyDeck failed to toggle persistent overlay", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to toggle persistent overlay" });
    }
  };

  const changeLang = async (lang: string) => {
    try {
      const nextStatus = await setOcrLang(lang);
      toaster.toast({ title: "ClarifyDeck", body: `OCR language: ${nextStatus.ocr_lang}` });
      await refresh();
    } catch (error) {
      console.warn("ClarifyDeck failed to set OCR language", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to set OCR language" });
    }
  };

  const applyOcrOptions = async (overrides: { invert?: boolean; psm?: number; scale?: number }) => {
    const invert = overrides.invert ?? status?.ocr_invert ?? false;
    const psm = overrides.psm ?? status?.ocr_psm ?? 6;
    const scale = overrides.scale ?? status?.ocr_scale ?? 2;
    try {
      await setOcrOptions(invert, psm, scale);
      await refresh();
    } catch (error) {
      console.warn("ClarifyDeck failed to set OCR options", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to set OCR options" });
    }
  };

  const testOcr = async () => {
    if (!selectedBox) {
      return;
    }
    try {
      const result = await runOcrNow(selectedBox.id);
      const body = result?.text
        ? result.text.slice(0, 120)
        : result?.error
          ? `OCR error: ${result.error.slice(0, 100)}`
          : "OCR returned empty text";
      const cropNote = result?.crop_path ? `\ncrop: ${result.crop_path}` : "";
      toaster.toast({ title: "ClarifyDeck OCR", body: `${body}${cropNote}` });
      await refresh();
    } catch (error) {
      console.warn("ClarifyDeck test OCR failed", error);
      toaster.toast({ title: "ClarifyDeck OCR", body: "Test OCR failed" });
    }
  };

  const applyRoiResult = (result: RecognitionROIResult) => {
    if (result.roi) {
      setRoiDraft({
        x: Math.round(result.roi.x * 100),
        y: Math.round(result.roi.y * 100),
        width: Math.round(result.roi.width * 100),
        height: Math.round(result.roi.height * 100),
      });
    }
    setRoiSource(result.source ?? "default");
    setRoiError(result.config_error ?? "");
  };

  const loadRoi = async () => {
    try {
      applyRoiResult(await roiConfigGet(null));
    } catch (error) {
      console.warn("ClarifyDeck failed to load recognition area", error);
    }
  };

  useEffect(() => {
    void loadRoi();
  }, []);

  const applyRoi = async () => {
    const message = validateRoiDraft(roiDraft);
    if (message) {
      setRoiError(message);
      toaster.toast({ title: "ClarifyDeck", body: message });
      return;
    }
    try {
      const result = await roiConfigSet(
        {
          x: roiDraft.x / 100,
          y: roiDraft.y / 100,
          width: roiDraft.width / 100,
          height: roiDraft.height / 100,
        },
        null,
      );
      if (!result.ok) {
        const detail = result.detail || result.error || "Rejected";
        setRoiError(detail);
        toaster.toast({ title: "ClarifyDeck", body: `Recognition area rejected: ${detail}` });
        return;
      }
      applyRoiResult(result);
      toaster.toast({ title: "ClarifyDeck", body: "Recognition area saved" });
    } catch (error) {
      console.warn("ClarifyDeck failed to save recognition area", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to save recognition area" });
    }
  };

  const resetRoi = async () => {
    try {
      applyRoiResult(await roiConfigReset(null));
      toaster.toast({ title: "ClarifyDeck", body: "Recognition area reset" });
    } catch (error) {
      console.warn("ClarifyDeck failed to reset recognition area", error);
      toaster.toast({ title: "ClarifyDeck", body: "Failed to reset recognition area" });
    }
  };

  const captureAge = status?.last_capture_at
    ? Math.max(0, Math.round(Date.now() / 1000 - status.last_capture_at))
    : -1;
  const ocrAge = status?.last_ocr_at
    ? Math.max(0, Math.round(Date.now() / 1000 - status.last_ocr_at))
    : -1;

  const updateSelectedBox = (field: keyof Pick<BoxState, "x" | "y" | "w" | "h">, value: number) => {
    if (!selectedBox) {
      return;
    }
    const nextBox = { ...selectedBox, [field]: value };
    setBoxes((currentBoxes) =>
      currentBoxes.map((box) => (box.id === nextBox.id ? nextBox : box)),
    );
    void updateBox(nextBox.id, nextBox.x, nextBox.y, nextBox.w, nextBox.h).catch((error) => {
      console.warn("ClarifyDeck failed to update region", error);
    });
  };

  return (
    <PanelSection title="ClarifyDeck OCR">
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={togglePlugin}>
          {status?.enabled ? "Stop OCR capture" : "Start OCR capture"}
        </ButtonItem>
      </PanelSectionRow>

      <OCRDiagnosticSection />

      <PanelSection title="Recognition Area">
        <PanelSectionRow>
          <div style={statusStyle}>
            <div>Source: {roiSource}</div>
            <div>
              X {roiDraft.x}% | Y {roiDraft.y}% | W {roiDraft.width}% | H {roiDraft.height}%
            </div>
            {roiError ? <div style={errorStyle}>{roiError}</div> : null}
          </div>
        </PanelSectionRow>
        <CoordinateSlider
          label="X%"
          min={0}
          max={99}
          value={roiDraft.x}
          onChange={(value) => setRoiDraft((draft) => ({ ...draft, x: value }))}
        />
        <CoordinateSlider
          label="Y%"
          min={0}
          max={99}
          value={roiDraft.y}
          onChange={(value) => setRoiDraft((draft) => ({ ...draft, y: value }))}
        />
        <CoordinateSlider
          label="W%"
          min={2}
          max={100}
          value={roiDraft.width}
          onChange={(value) => setRoiDraft((draft) => ({ ...draft, width: value }))}
        />
        <CoordinateSlider
          label="H%"
          min={2}
          max={100}
          value={roiDraft.height}
          onChange={(value) => setRoiDraft((draft) => ({ ...draft, height: value }))}
        />
        <PanelSectionRow>
          <div style={rowActionsStyle}>
            <ButtonItem layout="below" onClick={applyRoi}>
              Apply
            </ButtonItem>
            <ButtonItem layout="below" onClick={resetRoi}>
              Reset
            </ButtonItem>
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={rowActionsStyle}>
            {ROI_PRESETS.map((preset) => (
              <ButtonItem key={preset.label} layout="below" onClick={() => setRoiDraft(preset.roi)}>
                {preset.label}
              </ButtonItem>
            ))}
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={hintStyle}>
            Percent of the game frame. Apply validates and saves; Reset removes the override
            and falls back to the preset.
          </div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Persistent Game Overlay (Experimental)">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={toggleOverlay}>
            {status?.overlay?.enabled ? "Disable persistent overlay" : "Enable persistent overlay"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={statusStyle}>
            <div>
              Backend: {status?.backend?.role ?? "-"} (pid {status?.backend?.pid ?? "-"})
            </div>
            <div>Overlay state: {status?.overlay?.state ?? "DISABLED"}</div>
            <div>Renderer pid: {status?.overlay?.renderer_pid ?? "-"}</div>
            <div>Overlay display: {status?.overlay?.display ?? "-"}</div>
            {status?.overlay?.last_error ? (
              <div style={errorStyle}>{status.overlay.last_error}</div>
            ) : null}
          </div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSectionRow>
        <div style={rowActionsStyle}>
          <ButtonItem layout="below" onClick={createBox}>
            + Add region
          </ButtonItem>
          <ButtonItem layout="below" onClick={deleteSelectedBox}>
            - Remove selected
          </ButtonItem>
        </div>
      </PanelSectionRow>

      <PanelSectionRow>
        <div style={statusStyle}>
          <div>Plugin: {status?.enabled ? "enabled" : "disabled"}</div>
          <div>Backend: {status?.capture_running ? "running" : "stopped"}</div>
          <div>Regions: {status?.box_count ?? boxes.length}</div>
          <div>OCR: {status?.tesseract ? "Tesseract found" : "waiting for Tesseract"}</div>
          <div>Langs: {status?.ocr_langs?.join(", ") ?? "-"}</div>
          <div>
            Lang: {status?.ocr_lang ?? "-"} | x{status?.ocr_scale ?? 1} | PSM{" "}
            {status?.ocr_psm ?? 6} | {status?.ocr_invert ? "invert" : "normal"}
          </div>
          <div>Frame: {status?.frame_cached ? "captured" : "none"}{captureAge >= 0 ? ` (${captureAge}s ago)` : ""}</div>
          <div>Last OCR: {ocrAge >= 0 ? `${ocrAge}s ago` : "never"}</div>
          <div>Overlay: {overlayState.mounted ? `mounted (${overlayState.method}, ${overlayState.boxes} boxes)` : "NOT mounted"}</div>
          <div>Canvas: {sliderMaxWidth}x{sliderMaxHeight}</div>
          <div>
            Overlay viewport: {overlayViewport.w.toFixed(0)}x{overlayViewport.h.toFixed(0)}{" "}
            dpr{window.devicePixelRatio}
          </div>
          <div>
            Scale: {(overlayViewport.w / sliderMaxWidth).toFixed(3)}x
            {(overlayViewport.h / sliderMaxHeight).toFixed(3)}
          </div>
          {status?.last_error ? <div style={errorStyle}>{status.last_error}</div> : null}
        </div>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={testOcr}>
          Test OCR now (selected region)
        </ButtonItem>
      </PanelSectionRow>

      <PanelSection title="OCR language">
        <PanelSectionRow>
          <div style={rowActionsStyle}>
            {LANGUAGES.map((lang) => (
              <ButtonItem key={lang.value} layout="below" onClick={() => changeLang(lang.value)}>
                {status?.ocr_lang === lang.value ? `[x] ${lang.label}` : `[ ] ${lang.label}`}
              </ButtonItem>
            ))}
          </div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="OCR tuning">
        <PanelSectionRow>
          <div style={rowActionsStyle}>
            <ButtonItem
              layout="below"
              onClick={() => applyOcrOptions({ invert: !(status?.ocr_invert ?? false) })}
            >
              {status?.ocr_invert ? "[x] Invert" : "[ ] Invert"}
            </ButtonItem>
            {[6, 4, 3, 11].map((option) => (
              <ButtonItem key={option} layout="below" onClick={() => applyOcrOptions({ psm: option })}>
                {status?.ocr_psm === option ? `[x] PSM ${option}` : `[ ] PSM ${option}`}
              </ButtonItem>
            ))}
            {[1, 2, 3].map((option) => (
              <ButtonItem key={option} layout="below" onClick={() => applyOcrOptions({ scale: option })}>
                {status?.ocr_scale === option ? `[x] x${option}` : `[ ] x${option}`}
              </ButtonItem>
            ))}
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={hintStyle}>
            Use "Test OCR now" after each change to compare. PSM 6 = block, 4 = column,
            3 = auto, 11 = sparse. Invert for dark text on light background.
          </div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Regions">
        {boxes.length === 0 ? (
          <PanelSectionRow>
            <div style={hintStyle}>Press "+ Add region" to create a default 300x60 OCR area.</div>
          </PanelSectionRow>
        ) : (
          boxes.map((box, index) => (
            <PanelSectionRow key={box.id}>
              <ButtonItem layout="below" onClick={() => setSelectedId(box.id)}>
                {box.id === selectedBox?.id ? "[x]" : "[ ]"} Region {index + 1}: x{box.x} y{box.y} {box.w}x{box.h}
              </ButtonItem>
            </PanelSectionRow>
          ))
        )}
      </PanelSection>

      {selectedBox ? (
        <PanelSection title={`Adjust ${selectedBox.id}`}>
          <CoordinateSlider label="X" min={0} max={sliderMaxWidth} value={selectedBox.x} onChange={(value) => updateSelectedBox("x", value)} />
          <CoordinateSlider label="Y" min={0} max={sliderMaxHeight} value={selectedBox.y} onChange={(value) => updateSelectedBox("y", value)} />
          <CoordinateSlider label="W" min={10} max={sliderMaxWidth} value={selectedBox.w} onChange={(value) => updateSelectedBox("w", value)} />
          <CoordinateSlider label="H" min={10} max={sliderMaxHeight} value={selectedBox.h} onChange={(value) => updateSelectedBox("h", value)} />
        </PanelSection>
      ) : null}
    </PanelSection>
  );
}

type CoordinateSliderProps = {
  label: string;
  min: number;
  max: number;
  value: number;
  onChange: (value: number) => void;
};

function CoordinateSlider({ label, min, max, value, onChange }: CoordinateSliderProps) {
  return (
    <PanelSectionRow>
      <label style={sliderLabelStyle}>
        <span style={sliderTitleStyle}>{label}</span>
        <input
          min={min}
          max={max}
          onChange={(event) => onChange(Number(event.currentTarget.value))}
          style={sliderStyle}
          type="range"
          value={value}
        />
        <span style={sliderValueStyle}>{value}</span>
      </label>
    </PanelSectionRow>
  );
}

function useCaptureScale() {
  const [capture, setCapture] = useState({ w: 1280, h: 800 });
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const fresh = await getStatus();
        if (active) {
          setCapture({ w: fresh.screen_width || 1280, h: fresh.screen_height || 800 });
        }
      } catch (error) {
        console.warn("ClarifyDeck capture scale lookup failed", error);
      }
    };
    void load();
    const id = window.setInterval(load, 3000);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, []);
  return capture;
}

function Overlay() {
  const { boxes } = useClarifyDeckState({ pollStatus: false });
  const selectedBoxId = useSelectedBoxId();
  const qamVisible = useQuickAccessVisible();
  const capture = useCaptureScale();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [viewport, setViewport] = useState({ w: 1280, h: 800 });

  useEffect(() => {
    const measure = () => {
      const rect = rootRef.current?.getBoundingClientRect();
      if (rect && rect.width > 2 && rect.height > 2) {
        setViewport({ w: rect.width, h: rect.height });
        setGlobalOverlayViewport(rect.width, rect.height);
      }
    };
    measure();
    const id = window.setInterval(measure, 2000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    setOverlayMounted(true, boxes.length);
    return () => setOverlayMounted(false, 0);
  }, [boxes.length]);

  const sx = capture.w ? viewport.w / capture.w : 1;
  const sy = capture.h ? viewport.h / capture.h : 1;

  return (
    <div ref={rootRef} style={overlayRootStyle}>
      {qamVisible
        ? boxes.map((box, index) => {
            const selected = box.id === selectedBoxId || (!selectedBoxId && index === 0);
            if (!selected) {
              return null;
            }
            return (
              <div
                key={`region-${box.id}`}
                style={{
                  ...regionBoxStyle,
                  ...selectedRegionBoxStyle,
                  left: box.x * sx,
                  top: box.y * sy,
                  width: box.w * sx,
                  height: box.h * sy,
                }}
              >
                <div style={regionLabelStyle}>
                  X {box.x} | Y {box.y} | {box.w}x{box.h}
                </div>
              </div>
            );
          })
        : null}
    </div>
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
  fontSize: "13px",
  lineHeight: 1.35,
};

const sliderLabelStyle: CSSProperties = {
  alignItems: "center",
  display: "grid",
  gap: "8px",
  gridTemplateColumns: "28px 1fr 56px",
  width: "100%",
};

const sliderTitleStyle: CSSProperties = {
  color: "#f5f5f5",
  fontWeight: 700,
};

const sliderStyle: CSSProperties = {
  accentColor: "#67d4ff",
  width: "100%",
};

const sliderValueStyle: CSSProperties = {
  color: "#d7f3ff",
  fontVariantNumeric: "tabular-nums",
  textAlign: "right",
};

const overlayRootStyle: CSSProperties = {
  height: "100%",
  left: 0,
  overflow: "visible",
  pointerEvents: "none",
  position: "fixed",
  top: 0,
  width: "100%",
  zIndex: 2147483000,
};

const regionBoxStyle: CSSProperties = {
  background: "rgba(0, 0, 0, 0.08)",
  borderRadius: "6px",
  boxSizing: "border-box",
  position: "absolute",
};

const selectedRegionBoxStyle: CSSProperties = {
  border: "3px solid rgba(255, 214, 10, 0.98)",
  boxShadow: "0 0 0 2px rgba(0, 0, 0, 0.9), 0 0 18px rgba(255, 214, 10, 0.85)",
};

const regionLabelStyle: CSSProperties = {
  background: "rgba(0, 0, 0, 0.82)",
  borderRadius: "4px",
  color: "#ffffff",
  fontSize: "13px",
  fontWeight: 800,
  left: "0",
  lineHeight: 1.15,
  maxWidth: "100%",
  overflow: "hidden",
  padding: "3px 6px",
  position: "absolute",
  textOverflow: "ellipsis",
  top: "-24px",
  whiteSpace: "nowrap",
};

export default definePlugin(() => {
  console.log("ClarifyDeck initializing");
  const disposeOverlay = mountOverlay();

  return {
    name: "ClarifyDeck",
    titleView: <div className={staticClasses.Title}>ClarifyDeck</div>,
    content: <Content />,
    icon: <FaSearchPlus />,
    onDismount() {
      disposeOverlay();
      console.log("ClarifyDeck unloaded");
    },
  };
});
