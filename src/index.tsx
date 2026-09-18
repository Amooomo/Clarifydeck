import { staticClasses, Tabs } from "@decky/ui";
import * as DeckyUiNS from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  routerHook,
  useQuickAccessVisible,
} from "@decky/api";
import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FaSearchPlus } from "react-icons/fa";
import { OCRControlSection } from "./components/OCRControl";
import { PersistentOverlaySection } from "./components/PersistentOverlayControl";
import { RegionEditorSection } from "./components/RegionEditor";
import {
  getRegionPreview,
  regionLabel,
  regionScreenRect,
  subscribeRegionPreview,
  type RegionPreviewState,
} from "./regionEditor";

// -- Production QAM pages -----------------------------------------------------
// Page 1 (OCR) is the day-to-day operating page; Page 2 (Regions) is the
// authoritative region/profile editor. L1/R1 page switching is provided by the
// native Steam/Decky `Tabs` component with explicit focus ownership (see the
// FreeDeck-proven pattern): a scoped root, `autoFocusContents={false}`, and
// re-focusing the tab row around `onShowTab`.

const PAGE_OCR = "ocr";
const PAGE_REGIONS = "regions";

interface GamepadTabClassMap {
  TabsRowScroll?: string;
  TabRowTabs?: string;
  Tab?: string;
  Active?: string;
  Selected?: string;
}

function getGamepadTabClassMap(): GamepadTabClassMap | null {
  const map = (DeckyUiNS as unknown as { gamepadTabbedPageClasses?: GamepadTabClassMap })
    .gamepadTabbedPageClasses;
  if (!map || typeof map !== "object") {
    return null;
  }
  return map;
}

// Disable tab-row transition/scroll animation only inside ClarifyDeck's root so
// shoulder-button navigation is not destabilized by smooth scrolling.
const TAB_STABILITY_CSS = `
  .clarifydeck-qam-root [class*="TabRowTabs"],
  .clarifydeck-qam-root [class*="TabsRowScroll"],
  .clarifydeck-qam-root [class*="TabRow"] {
    transition: none !important;
    animation: none !important;
    scroll-behavior: auto !important;
  }
  .clarifydeck-qam-root [role="tablist"] {
    scroll-behavior: auto !important;
  }
`;

function OCRPage() {
  return (
    <>
      <OCRControlSection />
      <PersistentOverlaySection />
    </>
  );
}

function RegionsPage() {
  return <RegionEditorSection />;
}

function Content() {
  const [page, setPage] = useState<string>(PAGE_OCR);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const tabs = useMemo(
    () => [
      { id: PAGE_OCR, title: "OCR", content: <OCRPage /> },
      { id: PAGE_REGIONS, title: "Regions", content: <RegionsPage /> },
    ],
    [],
  );

  // Focus the tab row (or a specific tab) within ClarifyDeck's own root only.
  const focusTabRow = useCallback(
    (tabId?: string) => {
      const classMap = getGamepadTabClassMap();
      const root = rootRef.current;
      if (!classMap || !root) {
        return;
      }
      const tabClass = classMap.Tab;
      if (!tabClass) {
        return;
      }
      let target: HTMLElement | null = null;
      if (tabId) {
        const title = String(tabs.find((tab) => tab.id === tabId)?.title ?? "").trim();
        if (title) {
          for (const element of Array.from(root.querySelectorAll(`.${tabClass}`))) {
            const item = element as HTMLElement;
            if (String(item.textContent ?? "").trim() === title) {
              target = item;
              break;
            }
          }
        }
      }
      if (!target) {
        const activeClass = classMap.Active || classMap.Selected;
        const selector = activeClass ? `.${tabClass}.${activeClass}` : `.${tabClass}`;
        target = root.querySelector(selector) as HTMLElement | null;
      }
      target?.focus?.();
    },
    [tabs],
  );

  const onShowTab = useCallback(
    (tabId: string) => {
      focusTabRow();
      setPage(tabId);
      window.requestAnimationFrame(() => focusTabRow(tabId));
    },
    [focusTabRow],
  );

  // Give the tab row focus on mount so L1/R1 work immediately.
  useEffect(() => {
    const handle = window.requestAnimationFrame(() => focusTabRow());
    return () => window.cancelAnimationFrame(handle);
  }, [focusTabRow]);

  // Reset tab-row horizontal scroll after tab changes (stability).
  useEffect(() => {
    const classMap = getGamepadTabClassMap();
    if (!classMap) {
      return;
    }
    const rowClass = classMap.TabsRowScroll || classMap.TabRowTabs;
    if (!rowClass) {
      return;
    }
    const handle = window.requestAnimationFrame(() => {
      const row = rootRef.current?.querySelector(`.${rowClass}`) as HTMLElement | null;
      if (row) {
        row.style.scrollBehavior = "auto";
        row.scrollLeft = 0;
      }
    });
    return () => window.cancelAnimationFrame(handle);
  }, [page]);

  if (typeof Tabs === "function") {
    return (
      <div ref={rootRef} className="clarifydeck-qam-root" style={qamRootStyle}>
        <style>{TAB_STABILITY_CSS}</style>
        <Tabs tabs={tabs} activeTab={page} onShowTab={onShowTab} autoFocusContents={false} />
      </div>
    );
  }
  return <FallbackPages page={page} onSelect={setPage} />;
}

function FallbackPages({ page, onSelect }: { page: string; onSelect: (id: string) => void }) {
  return (
    <>
      <div style={pageHeaderStyle}>
        <button type="button" onClick={() => onSelect(PAGE_OCR)} style={pageTabStyle(page === PAGE_OCR)}>
          OCR
        </button>
        <button
          type="button"
          onClick={() => onSelect(PAGE_REGIONS)}
          style={pageTabStyle(page === PAGE_REGIONS)}
        >
          Regions
        </button>
      </div>
      {page === PAGE_REGIONS ? <RegionsPage /> : <OCRPage />}
    </>
  );
}

// -- Legacy BoxState types kept only for the QAM region-preview overlay path ---

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
  screen_width: number;
  screen_height: number;
  overlay?: OverlayStatus | null;
};

const listBoxes = callable<[], BoxState[]>("list_boxes");
const getStatus = callable<[], BackendStatus>("get_status");

let globalSelectedBoxId: string | undefined;
const selectionEvents = new EventTarget();

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

// Phase 2L.8.1: the region preview MUST live in the Steam UI app tree. Phase 1C
// proved a plain DOM node appended to `document.body` is invisible over the game
// even while the QAM is open; only `routerHook.addGlobalComponent` (the Steam UI
// layer) is composited over the game while the QAM is active, which is exactly
// the region-editing context. The body-mounted createRoot path was removed.
function mountOverlay(): () => void {
  routerHook.addGlobalComponent("ClarifyDeckOverlay", Overlay);
  return () => routerHook.removeGlobalComponent("ClarifyDeckOverlay");
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
  const [regionPreview, setRegionPreviewState] = useState<RegionPreviewState>(getRegionPreview());

  useEffect(() => subscribeRegionPreview(() => setRegionPreviewState(getRegionPreview())), []);

  // Device-focused diagnostics: log the preview target viewport and computed
  // pixel rects once per draft change (helps verify the on-screen mapping).
  const previewSignature = regionPreview.drafts
    .map((region) => `${region.region_id}:${region.x},${region.y},${region.w},${region.h},${region.enabled}`)
    .join("|");
  const lastPreviewSignature = useRef("");
  useEffect(() => {
    if (!qamVisible || regionPreview.drafts.length === 0 || previewSignature === lastPreviewSignature.current) {
      return;
    }
    lastPreviewSignature.current = previewSignature;
    console.log("[region-preview]", {
      viewport: `${Math.round(viewport.w)}x${Math.round(viewport.h)}`,
      rects: regionPreview.drafts.map((region) => {
        const rect = regionScreenRect(region, viewport.w, viewport.h);
        return {
          id: region.region_id,
          normalized: [region.x, region.y, region.w, region.h],
          pixel: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
        };
      }),
    });
  }, [previewSignature, qamVisible, viewport.w, viewport.h, regionPreview.drafts]);

  useEffect(() => {
    const measure = () => {
      const rect = rootRef.current?.getBoundingClientRect();
      if (rect && rect.width > 2 && rect.height > 2) {
        setViewport({ w: rect.width, h: rect.height });
      }
    };
    measure();
    const id = window.setInterval(measure, 2000);
    return () => window.clearInterval(id);
  }, []);

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

      {qamVisible
        ? regionPreview.drafts.map((region, index) => {
            const rect = regionScreenRect(region, viewport.w, viewport.h);
            const selected = region.region_id === regionPreview.selectedId;
            return (
              <div
                key={`v2-region-${region.region_id}`}
                style={{
                  ...regionBoxStyle,
                  ...(selected ? selectedRegionBoxStyle : {}),
                  left: rect.left,
                  top: rect.top,
                  width: rect.width,
                  height: rect.height,
                  opacity: region.enabled ? 1 : 0.45,
                  borderStyle: region.enabled ? "solid" : "dashed",
                }}
              >
                <div style={regionLabelStyle}>{regionLabel(region, index, regionPreview.primaryId)}</div>
              </div>
            );
          })
        : null}
    </div>
  );
}

const qamRootStyle: CSSProperties = {
  boxSizing: "border-box",
  overflowX: "hidden",
  width: "100%",
};

const pageHeaderStyle: CSSProperties = {
  display: "flex",
  gap: "8px",
  padding: "4px 0 8px",
};

const pageTabStyle = (active: boolean): CSSProperties => ({
  background: active ? "#3d4450" : "#23262e",
  border: "1px solid #5a6270",
  borderRadius: "4px",
  color: active ? "#ffffff" : "#b8bcc4",
  cursor: "pointer",
  flex: "1 1 0",
  fontSize: "13px",
  fontWeight: 700,
  padding: "8px 0",
});

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
