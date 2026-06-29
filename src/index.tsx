
import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  toaster,
} from "@decky/api";
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import { FaSearchPlus } from "react-icons/fa";
import { createPortal } from "react-dom";

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

type BackendStatus = {
  enabled: boolean;
  box_count: number;
  capture_running: boolean;
  last_error: string;
  last_capture_at: number;
  last_ocr_at: number;
  ocr_lang: string;
  mse_threshold: number;
  tesseract: string;
  tessdata?: string;
  screen_width: number;
  screen_height: number;
};

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

let globalSelectedBoxId: string | undefined;
const selectionEvents = new EventTarget();

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
          <div>Canvas: {sliderMaxWidth}x{sliderMaxHeight}</div>
          {status?.last_error ? <div style={errorStyle}>{status.last_error}</div> : null}
        </div>
      </PanelSectionRow>

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

function Overlay() {
  const { boxes } = useClarifyDeckState({ pollStatus: false });
  const selectedBoxId = useSelectedBoxId();

  // 用于动态捕获 Steam 真实游戏画面的根节点，逃离 QAM 的 Transform 牢笼
  const [portalTarget, setPortalTarget] = useState<Element | null>(null);

  useEffect(() => {
    const target = document.querySelector('.app') || document.body;
    setPortalTarget(target);
  }, []);

  if (!portalTarget) return null;

  return createPortal(
    <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, right: 0, zIndex: 2147483647, pointerEvents: "none", overflow: "visible" }}>
      {boxes.map((box, index) => {
        const selected = box.id === selectedBoxId || (!selectedBoxId && index === 0);
        return (
          <div
            key={`region-${box.id}`}
            style={{
              ...regionBoxStyle,
              ...(selected ? selectedRegionBoxStyle : idleRegionBoxStyle),
              left: box.x,
              top: box.y,
              width: box.w,
              height: box.h,
            }}
          >
            <div style={regionLabelStyle}>
              {selected ? "SELECTED" : `REGION ${index + 1}`} | X {box.x} | Y {box.y} | W {box.w} | H {box.h}
            </div>
          </div>
        );
      })}

      {boxes
        .filter((box) => box.text.trim().length > 0)
        .map((box) => (
          <div
            key={`subtitle-${box.id}`}
            style={{
              ...subtitleBoxStyle,
              left: box.x,
              top: box.y,
              width: box.w,
              minHeight: box.h,
            }}
          >
            {box.text}
          </div>
        ))}
    </div>,
    portalTarget
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
  pointerEvents: "none",
  position: "fixed",
  top: 0,
  width: "100%",
  zIndex: 99999,
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

const idleRegionBoxStyle: CSSProperties = {
  border: "2px dashed rgba(0, 229, 255, 0.82)",
  boxShadow: "0 0 0 1px rgba(0, 0, 0, 0.75)",
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

const subtitleBoxStyle: CSSProperties = {
  alignItems: "center",
  backdropFilter: "blur(6px)",
  background: "rgba(0, 0, 0, 0.72)",
  border: "1px solid rgba(255, 255, 255, 0.22)",
  borderRadius: "10px",
  boxShadow: "0 10px 24px rgba(0, 0, 0, 0.35)",
  boxSizing: "border-box",
  color: "#ffffff",
  display: "flex",
  fontSize: "24px",
  fontWeight: 700,
  justifyContent: "center",
  lineHeight: 1.25,
  overflow: "hidden",
  padding: "8px 12px",
  position: "absolute",
  textAlign: "center",
  textShadow: "0 2px 4px rgba(0, 0, 0, 0.85)",
  whiteSpace: "pre-wrap",
};

export default definePlugin(() => {
  console.log("ClarifyDeck initializing");

  return {
    name: "ClarifyDeck",
    titleView: <div className={staticClasses.Title}>ClarifyDeck</div>,
    content: <Content />,
    icon: <FaSearchPlus />,
    alwaysRender: <Overlay />,
    onDismount() {
      console.log("ClarifyDeck unloaded");
    },
  };
});
