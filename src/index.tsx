import { Focusable, GamepadButton, staticClasses, type GamepadEvent } from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  routerHook,
  useQuickAccessVisible,
} from "@decky/api";
import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { FaSearchPlus } from "react-icons/fa";
import { OCRControlSection } from "./components/OCRControl";
import { PersistentOverlaySection } from "./components/PersistentOverlayControl";
import { RegionEditorSection } from "./components/RegionEditor";
import {
  getNextPageIndex,
  getPreviousPageIndex,
  getQamPageSession,
  pageIndexById,
  rememberQamPageSession,
  resetQamPageSession,
  resolveSessionPageId,
  type QamPage,
} from "./qamPages";
import {
  getRegionPreview,
  regionLabel,
  regionScreenRect,
  subscribeRegionPreview,
  type RegionPreviewState,
} from "./regionEditor";

// -- Production QAM pages -----------------------------------------------------
// Page state is owned by ClarifyDeck (not the Decky `Tabs` component). Page
// headers are clickable/tappable; L1/R1 = previous/next page via the scoped
// `Focusable` gamepad button handler, which only acts while ClarifyDeck's QAM
// content owns focus and yields naturally to Dropdown/Modal contexts.
// Shoulder navigation is data-driven and non-wrapping (see `./qamPages`).

const PAGES: QamPage[] = [
  { id: "ocr", title: "OCR" },
  { id: "regions", title: "Regions" },
];

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

function PageHeader({
  pages,
  activeId,
  onSelect,
}: {
  pages: QamPage[];
  activeId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <div style={pageHeaderStyle}>
      {pages.map((page) => (
        <button
          key={page.id}
          type="button"
          aria-pressed={page.id === activeId}
          onClick={() => onSelect(page.id)}
          style={pageTabStyle(page.id === activeId)}
        >
          {page.title}
        </button>
      ))}
    </div>
  );
}

function Content() {
  // Active page is session-backed so it survives transient remounts. The page
  // changes ONLY on an explicit navigation action (header click / L1 / R1).
  // Dropdown/context-menu/visibility/focus/remount events never change it.
  const [activeId, setActiveId] = useState<string>(() =>
    resolveSessionPageId(PAGES, getQamPageSession()),
  );

  useEffect(() => {
    rememberQamPageSession(activeId);
  }, [activeId]);

  const goToPage = useCallback((id: string) => {
    setActiveId((current) => (PAGES.some((page) => page.id === id) ? id : current));
  }, []);

  const goPreviousPage = useCallback(() => {
    setActiveId((current) => {
      const index = getPreviousPageIndex(pageIndexById(PAGES, current), PAGES.length);
      return PAGES[index].id;
    });
  }, []);

  const goNextPage = useCallback(() => {
    setActiveId((current) => {
      const index = getNextPageIndex(pageIndexById(PAGES, current), PAGES.length);
      return PAGES[index].id;
    });
  }, []);

  // Scoped shoulder handling: only fires while ClarifyDeck's content owns focus
  // (Focusable button events bubble from the focused descendant). Dropdown/Modal
  // contexts own focus elsewhere, so this yields to them.
  const onButtonDown = useCallback(
    (evt: GamepadEvent) => {
      const button = evt?.detail?.button;
      if (button === GamepadButton.BUMPER_LEFT) {
        evt.stopPropagation();
        goPreviousPage();
      } else if (button === GamepadButton.BUMPER_RIGHT) {
        evt.stopPropagation();
        goNextPage();
      }
    },
    [goPreviousPage, goNextPage],
  );

  const content = (
    <>
      <PageHeader pages={PAGES} activeId={activeId} onSelect={goToPage} />
      {activeId === "regions" ? <RegionsPage /> : <OCRPage />}
    </>
  );

  if (typeof Focusable === "function") {
    return (
      <Focusable onButtonDown={onButtonDown} style={qamRootStyle} flow-children="vertical">
        {content}
      </Focusable>
    );
  }
  return <div style={qamRootStyle}>{content}</div>;
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
      resetQamPageSession();
      disposeOverlay();
      console.log("ClarifyDeck unloaded");
    },
  };
});
