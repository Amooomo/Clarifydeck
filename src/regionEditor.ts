// Phase 2L.7 — pure multi-region editor logic (no React, no @decky/api).
// Kept dependency-free so the lightweight Node harness can unit-test it.

export type RegionDraft = {
  region_id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  enabled: boolean;
  name?: string | null;
};

export type RegionConfigPayload = {
  ok?: boolean;
  version?: number;
  scope?: string;
  app_id?: string | null;
  configured?: boolean;
  configured_regions?: RegionDraft[];
  effective_regions?: RegionDraft[];
  source?: string;
  max_regions?: number;
  last_error?: string | null;
  config_path?: string;
  error?: string;
  detail?: string;
};

export const MAX_REGIONS = 8;
export const MIN_REGION_SIZE = 0.02;
export const NEW_REGION_PREFIX = "new-";

export function primaryRegionId(regions: RegionDraft[]): string | null {
  for (const region of regions) {
    if (region.enabled) {
      return region.region_id;
    }
  }
  return null;
}

export function canAddRegion(regions: RegionDraft[]): boolean {
  return regions.length < MAX_REGIONS;
}

export function newRegionDraft(index: number): RegionDraft {
  const y = Math.min(0.1 + 0.12 * index, 0.8);
  return {
    region_id: `${NEW_REGION_PREFIX}${Date.now().toString(36)}-${index}`,
    x: 0.1,
    y,
    w: 0.8,
    h: 0.15,
    enabled: true,
    name: "",
  };
}

export function validateRegionDraft(region: RegionDraft): string {
  const values = [region.x, region.y, region.w, region.h];
  if (values.some((value) => !Number.isFinite(value))) {
    return "Values must be numbers";
  }
  if (region.x < 0 || region.y < 0 || region.x >= 1 || region.y >= 1) {
    return "X and Y must be between 0% and 99%";
  }
  if (region.w < MIN_REGION_SIZE || region.h < MIN_REGION_SIZE) {
    return "Width and height must be at least 2%";
  }
  if (region.w > 1 || region.h > 1) {
    return "Width and height must be at most 100%";
  }
  if (region.x + region.w > 1.0001 || region.y + region.h > 1.0001) {
    return "Area must stay inside the frame";
  }
  return "";
}

export function validateRegions(regions: RegionDraft[]): string {
  if (regions.length > MAX_REGIONS) {
    return `At most ${MAX_REGIONS} regions are allowed`;
  }
  for (const region of regions) {
    const message = validateRegionDraft(region);
    if (message) {
      return message;
    }
  }
  return "";
}

export function updateRegionGeometry(
  regions: RegionDraft[],
  regionId: string,
  patch: Partial<Pick<RegionDraft, "x" | "y" | "w" | "h">>,
): RegionDraft[] {
  return regions.map((region) => (region.region_id === regionId ? { ...region, ...patch } : region));
}

export function setRegionEnabled(regions: RegionDraft[], regionId: string, enabled: boolean): RegionDraft[] {
  return regions.map((region) => (region.region_id === regionId ? { ...region, enabled } : region));
}

export function setRegionName(regions: RegionDraft[], regionId: string, name: string): RegionDraft[] {
  return regions.map((region) => (region.region_id === regionId ? { ...region, name } : region));
}

export function removeRegion(regions: RegionDraft[], regionId: string): RegionDraft[] {
  return regions.filter((region) => region.region_id !== regionId);
}

export function moveRegion(regions: RegionDraft[], regionId: string, delta: number): RegionDraft[] {
  const index = regions.findIndex((region) => region.region_id === regionId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= regions.length) {
    return regions;
  }
  const next = regions.slice();
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
}

// Primary is defined by the backend as "the first enabled region in order".
// "Set as Primary" reorders the selected enabled region to the first enabled
// slot while preserving the relative order of every other region. It never
// silently enables a disabled region, and it is a no-op for an unknown id or an
// already-primary selection. Persistence stays in the explicit Save Changes flow.
export function setPrimaryRegion(regions: RegionDraft[], regionId: string): RegionDraft[] {
  const index = regions.findIndex((region) => region.region_id === regionId);
  if (index < 0) {
    return regions;
  }
  if (!regions[index].enabled) {
    return regions;
  }
  const firstEnabled = regions.findIndex((region) => region.enabled);
  if (firstEnabled < 0 || firstEnabled === index) {
    return regions;
  }
  return moveRegion(regions, regionId, firstEnabled - index);
}

export function nextSelectionAfterRemove(regions: RegionDraft[], removedId: string): string | null {
  const index = regions.findIndex((region) => region.region_id === removedId);
  if (index < 0) {
    return regions[0]?.region_id ?? null;
  }
  const remaining = regions.filter((region) => region.region_id !== removedId);
  if (remaining.length === 0) {
    return null;
  }
  return (remaining[Math.min(index, remaining.length - 1)] ?? remaining[0]).region_id;
}

export function scopeAppId(scope: string, appId: string | null): string | null {
  return scope === "per_game" ? appId : null;
}

// Inherited (not-yet-configured) regions must not be persisted merely by opening
// the editor; on explicit Apply their IDs are stripped so the backend assigns
// fresh stable IDs (adoption).
export function draftsForApply(regions: RegionDraft[], configured: boolean): RegionDraft[] {
  return regions.map((region) => {
    const existing = configured && region.region_id && !region.region_id.startsWith(NEW_REGION_PREFIX);
    return existing ? region : { ...region, region_id: "" };
  });
}

// Steam Deck QAM text entry is impractical, so labels are order-derived
// (`Region N`); the optional persisted `name` field is retained but not shown.
export function regionLabel(region: RegionDraft, index: number, primaryId: string | null): string {
  const label = `Region ${index + 1}`;
  const primary = region.region_id === primaryId ? " [Primary]" : "";
  const disabled = region.enabled ? "" : " (off)";
  return `${label}${primary}${disabled}`;
}

// -- Phase 2L.8 live preview --------------------------------------------------

export type RegionPreviewRegion = {
  region_id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  selected: boolean;
  primary: boolean;
  enabled: boolean;
  label: string;
};

export function regionPreviewLabel(region: RegionDraft, index: number, primaryId: string | null): string {
  const base = `Region ${index + 1}`;
  const prefix = region.region_id === primaryId ? "Primary · " : "";
  const suffix = region.enabled ? "" : " (off)";
  return `${prefix}${base}${suffix}`;
}

export function regionPreviewPayload(regions: RegionDraft[], selectedId: string | null): RegionPreviewRegion[] {
  const primaryId = primaryRegionId(regions);
  return regions.map((region, index) => ({
    region_id: region.region_id,
    x: region.x,
    y: region.y,
    w: region.w,
    h: region.h,
    selected: region.region_id === selectedId,
    primary: region.region_id === primaryId,
    enabled: region.enabled,
    label: regionPreviewLabel(region, index, primaryId),
  }));
}

export type ScreenRect = { left: number; top: number; width: number; height: number };

export function regionScreenRect(region: RegionDraft, viewportWidth: number, viewportHeight: number): ScreenRect {
  return {
    left: region.x * viewportWidth,
    top: region.y * viewportHeight,
    width: region.w * viewportWidth,
    height: region.h * viewportHeight,
  };
}

export function regionScreenRects(regions: RegionDraft[], viewportWidth: number, viewportHeight: number): ScreenRect[] {
  return regions.map((region) => regionScreenRect(region, viewportWidth, viewportHeight));
}

export type RegionPreviewState = {
  drafts: RegionDraft[];
  selectedId: string | null;
  primaryId: string | null;
};

const EMPTY_PREVIEW: RegionPreviewState = { drafts: [], selectedId: null, primaryId: null };
let previewState: RegionPreviewState = EMPTY_PREVIEW;
const previewEvents = new EventTarget();

export function setRegionPreview(state: RegionPreviewState): void {
  previewState = state;
  previewEvents.dispatchEvent(new Event("region-preview"));
}

export function clearRegionPreview(): void {
  previewState = EMPTY_PREVIEW;
  previewEvents.dispatchEvent(new Event("region-preview"));
}

export function getRegionPreview(): RegionPreviewState {
  return previewState;
}

export function subscribeRegionPreview(handler: () => void): () => void {
  previewEvents.addEventListener("region-preview", handler);
  return () => previewEvents.removeEventListener("region-preview", handler);
}

// -- Phase 2M.2A runtime panel styles (session-only; never persisted) ---------

export const PANEL_STYLE_WHITE_ON_BLACK = "white_on_black";
export const PANEL_STYLE_BLACK_ON_WHITE = "black_on_white";
export const DEFAULT_PANEL_STYLE = PANEL_STYLE_WHITE_ON_BLACK;

export function isPanelStyle(value: unknown): boolean {
  return value === PANEL_STYLE_WHITE_ON_BLACK || value === PANEL_STYLE_BLACK_ON_WHITE;
}

export function panelStyleLabel(style: string | null | undefined): string {
  return style === PANEL_STYLE_BLACK_ON_WHITE ? "Light panel" : "Dark panel";
}

// -- Phase 2M.2B runtime font size (session-only; never persisted) ------------

export const DEFAULT_REGION_FONT_SIZE = 20;
export const MIN_REGION_FONT_SIZE = 14;
export const MAX_REGION_FONT_SIZE = 48;
export const REGION_FONT_SIZE_STEP = 2;

export function isRegionFontSize(value: unknown): boolean {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= MIN_REGION_FONT_SIZE &&
    value <= MAX_REGION_FONT_SIZE
  );
}

export function clampRegionFontSize(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return DEFAULT_REGION_FONT_SIZE;
  }
  const stepped = Math.round(value / REGION_FONT_SIZE_STEP) * REGION_FONT_SIZE_STEP;
  return Math.min(MAX_REGION_FONT_SIZE, Math.max(MIN_REGION_FONT_SIZE, stepped));
}

// -- Phase 2M.2C.1 editor session + Dropdown value normalization ---------------
// Steam's QAM content can remount while a Dropdown context menu is open. These
// module-level values keep the editor's Region selection and Preview intent
// stable across such transient remounts, and are reset when the QAM closes.

export type RegionEditorSession = {
  selectedId: string | null;
  previewOn: boolean;
};

let editorSession: RegionEditorSession = { selectedId: null, previewOn: false };

export function getRegionEditorSession(): RegionEditorSession {
  return { ...editorSession };
}

export function rememberRegionSelection(selectedId: string | null): void {
  editorSession = { ...editorSession, selectedId };
}

export function rememberRegionPreview(previewOn: boolean): void {
  editorSession = { ...editorSession, previewOn };
}

export function resetRegionEditorSession(): void {
  editorSession = { selectedId: null, previewOn: false };
}

// Accept both a Decky Dropdown option object ({ data }) and a raw value.
export function dropdownOptionValue(option: unknown): string | null {
  if (typeof option === "string") {
    return option;
  }
  if (option && typeof option === "object" && "data" in (option as Record<string, unknown>)) {
    const value = (option as { data?: unknown }).data;
    if (typeof value === "string") {
      return value;
    }
    if (value === null || value === undefined) {
      return null;
    }
    return String(value);
  }
  return null;
}
