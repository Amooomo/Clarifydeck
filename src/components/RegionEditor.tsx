// Phase 2P.1 — production multi-region Recognition Region editor.
// Uses the authoritative backend v2 region config; the backend remains the
// source of truth. Saving never starts/stops/restarts OCR or the renderer, and
// changes apply on the next explicit OCR start.

import { ButtonItem, Dropdown, PanelSection, PanelSectionRow } from "@decky/ui";
import { callable, useQuickAccessVisible } from "@decky/api";
import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import {
  DEFAULT_PANEL_STYLE,
  DEFAULT_REGION_FONT_SIZE,
  MAX_REGIONS,
  MAX_REGION_FONT_SIZE,
  MIN_REGION_FONT_SIZE,
  PANEL_STYLE_BLACK_ON_WHITE,
  PANEL_STYLE_WHITE_ON_BLACK,
  REGION_FONT_SIZE_STEP,
  canAddRegion,
  clampRegionFontSize,
  clearRegionPreview,
  dropdownOptionValue,
  draftsForApply,
  getRegionEditorDraftState,
  getRegionEditorSession,
  isPanelStyle,
  isRegionFontSize,
  newRegionDraft,
  nextSelectionAfterRemove,
  panelStyleLabel,
  primaryRegionId,
  regionLabel,
  regionPreviewPayload,
  rememberRegionEditorDraftState,
  rememberRegionPreview,
  rememberRegionSelection,
  removeRegion,
  resetRegionEditorDraftState,
  resetRegionEditorSession,
  scopeAppId,
  setPrimaryRegion,
  setRegionEnabled,
  setRegionPreview,
  updateRegionGeometry,
  validateRegions,
  type RegionConfigPayload,
  type RegionDraft,
  type RegionEditorProfileInfo,
  type RegionPreviewRegion,
} from "../regionEditor";

const regionConfigGet = callable<[appId: string | null], RegionConfigPayload>("region_config_get");
const regionConfigSet = callable<[regions: RegionDraft[], appId: string | null], RegionConfigPayload>("region_config_set");

// Phase 2M.2C Region Profile ("Region Set") management. Each Region Set is an
// independently persisted Region JSON file selected by stable profile_id.
type RegionProfilesPayload = {
  ok?: boolean;
  error?: string;
  detail?: string;
  active_profile_id?: string;
  profiles?: RegionEditorProfileInfo[];
  max_profiles?: number;
  last_error?: string | null;
};
const regionProfilesGet = callable<[], RegionProfilesPayload>("region_profiles_get");
const regionProfileSelect = callable<[profileId: string], RegionProfilesPayload>("region_profile_select");
const regionProfileAdd = callable<[], RegionProfilesPayload>("region_profile_add");
const regionProfileDelete = callable<[profileId: string], RegionProfilesPayload>("region_profile_delete");

type OverlayControlResult = { ok?: boolean; error?: string; detail?: string; state?: string; renderer_pid?: number | null };

// Explicit, UI-local (not persisted) region-preview control. Reuses the proven
// Python/X11 renderer; independent from the persistent text overlay.
const setRegionPreviewEnabled = callable<[enabled: boolean], OverlayControlResult>("set_region_preview_enabled");
const setRegionPreviewRegions = callable<[regions: RegionPreviewRegion[]], OverlayControlResult>("set_region_preview");
const clearRegionPreviewRegions = callable<[], OverlayControlResult>("clear_region_preview");

// Phase 2M.2A runtime-only per-region panel style. Not persisted.
type PanelStyleResult = {
  ok?: boolean;
  error?: string;
  detail?: string;
  region_id?: string;
  style?: string;
};
const regionPanelStyleGet = callable<[regionId: string], PanelStyleResult>("region_panel_style_get");
const regionPanelStyleSet = callable<[regionId: string, style: string], PanelStyleResult>("region_panel_style_set");

// Phase 2M.2B runtime-only per-region font size. Not persisted.
type FontSizeResult = {
  ok?: boolean;
  error?: string;
  detail?: string;
  region_id?: string;
  font_size?: number;
};
const regionFontSizeGet = callable<[regionId: string], FontSizeResult>("region_font_size_get");
const regionFontSizeSet = callable<[regionId: string, fontSize: number], FontSizeResult>("region_font_size_set");

// Phase 2M.2D explicit persistence of the selected region's style + font size.
type AppearanceSaveResult = {
  ok?: boolean;
  error?: string;
  detail?: string;
  region_id?: string;
  style?: string;
  font_size?: number;
};
const regionAppearanceSave = callable<[regionId: string], AppearanceSaveResult>("region_appearance_save");

// No app_id source exists in the QAM yet, so "This Game" is unavailable.
const CURRENT_APP_ID: string | null = null;

export function RegionEditorSection() {
  // Resume the in-progress editor session if the QAM is still open (tab switch
  // remount); otherwise hydrate fresh from backend on first load.
  const initialDraft = useRef(getRegionEditorDraftState()).current;
  const resume = initialDraft.active;
  const [config, setConfig] = useState<RegionConfigPayload | undefined>();
  const [drafts, setDrafts] = useState<RegionDraft[]>(() => (resume ? initialDraft.drafts : []));
  const [configured, setConfigured] = useState(() => (resume ? initialDraft.configured : false));
  const [selectedId, setSelectedIdState] = useState<string | null>(
    () => getRegionEditorSession().selectedId,
  );
  const [profiles, setProfiles] = useState<RegionEditorProfileInfo[]>(
    () => (resume ? initialDraft.profiles : []),
  );
  const [activeProfileId, setActiveProfileId] = useState<string | null>(
    () => (resume ? initialDraft.activeProfileId : null),
  );
  const [maxProfiles, setMaxProfiles] = useState(() => (resume ? initialDraft.maxProfiles : 8));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [previewOn, setPreviewOnState] = useState<boolean>(
    () => getRegionEditorSession().previewOn,
  );
  const [styleByRegion, setStyleByRegion] = useState<Record<string, string>>(
    () => (resume ? initialDraft.styleByRegion : {}),
  );
  const [fontByRegion, setFontByRegion] = useState<Record<string, number>>(
    () => (resume ? initialDraft.fontByRegion : {}),
  );
  const mounted = useRef(true);
  const previewOnRef = useRef(false);
  const selectedIdRef = useRef<string | null>(selectedId);
  const qamVisible = useQuickAccessVisible();
  const qamVisibleRef = useRef(qamVisible);
  const prevQamVisibleRef = useRef(qamVisible);

  const selectRegion = (regionId: string | null) => {
    selectedIdRef.current = regionId;
    setSelectedIdState(regionId);
    rememberRegionSelection(regionId);
  };

  const setPreviewOn = (value: boolean) => {
    setPreviewOnState(value);
    rememberRegionPreview(value);
  };

  const applyPayload = (result: RegionConfigPayload) => {
    setConfig(result);
    const configuredNow = !!result.configured;
    setConfigured(configuredNow);
    const source = configuredNow ? result.configured_regions ?? [] : result.effective_regions ?? [];
    setDrafts(source.map((region) => ({ ...region })));
    const current = selectedIdRef.current;
    const next =
      current && source.some((region) => region.region_id === current)
        ? current
        : source[0]?.region_id ?? null;
    selectRegion(next);
  };

  const applyProfilesPayload = (result: RegionProfilesPayload) => {
    setProfiles(result.profiles ?? []);
    setActiveProfileId(result.active_profile_id ?? null);
    if (typeof result.max_profiles === "number") {
      setMaxProfiles(result.max_profiles);
    }
  };

  const loadActiveRegions = async () => {
    try {
      const result = await regionConfigGet(scopeAppId("global", CURRENT_APP_ID));
      if (mounted.current) {
        applyPayload(result);
        setError(result.ok === false ? result.error || result.detail || "Could not load regions" : "");
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Could not load regions: ${String(err)}`);
      }
    }
  };

  const load = async () => {
    try {
      const result = await regionProfilesGet();
      if (!mounted.current) {
        return;
      }
      if (result.ok === false) {
        setError(result.detail || result.error || "Could not load Region Sets");
        return;
      }
      applyProfilesPayload(result);
    } catch (err) {
      if (mounted.current) {
        setError(`Could not load Region Sets: ${String(err)}`);
      }
      return;
    }
    await loadActiveRegions();
  };

  useEffect(() => {
    mounted.current = true;
    if (!resume) {
      void load();
    }
    return () => {
      mounted.current = false;
    };
  }, []);

  // Keep the in-progress editor session alive across OCR <-> Regions switching.
  useEffect(() => {
    if (!mounted.current) {
      return;
    }
    rememberRegionEditorDraftState({
      active: true,
      activeProfileId,
      profiles,
      maxProfiles,
      configured,
      drafts,
      styleByRegion,
      fontByRegion,
    });
  }, [activeProfileId, profiles, maxProfiles, configured, drafts, styleByRegion, fontByRegion]);

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    qamVisibleRef.current = qamVisible;
  }, [qamVisible]);

  const selected = drafts.find((region) => region.region_id === selectedId) ?? null;
  const primaryId = primaryRegionId(drafts);
  const selectedStyle = (selectedId && styleByRegion[selectedId]) || DEFAULT_PANEL_STYLE;
  const selectedFontSize = (selectedId && fontByRegion[selectedId]) || DEFAULT_REGION_FONT_SIZE;

  // Load the selected region's runtime panel style (session-only, no persistence).
  useEffect(() => {
    if (!selectedId) {
      return;
    }
    void (async () => {
      try {
        const result = await regionPanelStyleGet(selectedId);
        if (mounted.current && result && result.ok !== false) {
          const style = isPanelStyle(result.style) ? (result.style as string) : DEFAULT_PANEL_STYLE;
          setStyleByRegion((current) => ({ ...current, [selectedId]: style }));
        }
      } catch (err) {
        if (mounted.current) {
          setError(`Panel style failed: ${String(err)}`);
        }
      }
    })();
  }, [selectedId]);

  const choosePanelStyle = async (style: string) => {
    if (!selectedId) {
      return;
    }
    setStyleByRegion((current) => ({ ...current, [selectedId]: style }));
    try {
      const result = await regionPanelStyleSet(selectedId, style);
      if (mounted.current && result && result.ok === false) {
        setError(result.detail || result.error || "Panel style failed");
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Panel style failed: ${String(err)}`);
      }
    }
  };

  // Load the selected region's runtime font size (session-only, no persistence).
  useEffect(() => {
    if (!selectedId) {
      return;
    }
    void (async () => {
      try {
        const result = await regionFontSizeGet(selectedId);
        if (mounted.current && result && result.ok !== false) {
          const size = isRegionFontSize(result.font_size)
            ? (result.font_size as number)
            : DEFAULT_REGION_FONT_SIZE;
          setFontByRegion((current) => ({ ...current, [selectedId]: size }));
        }
      } catch (err) {
        if (mounted.current) {
          setError(`Text size failed: ${String(err)}`);
        }
      }
    })();
  }, [selectedId]);

  const changeFontSize = (value: number) => {
    if (!selectedId) {
      return;
    }
    const size = clampRegionFontSize(value);
    setFontByRegion((current) => ({ ...current, [selectedId]: size }));
    void (async () => {
      try {
        const result = await regionFontSizeSet(selectedId, size);
        if (mounted.current && result && result.ok === false) {
          setError(result.detail || result.error || "Text size failed");
        }
      } catch (err) {
        if (mounted.current) {
          setError(`Text size failed: ${String(err)}`);
        }
      }
    })();
  };

  // In-QAM draft preview store (also feeds the renderer preview below).
  useEffect(() => {
    setRegionPreview({ drafts, selectedId, primaryId });
  }, [drafts, selectedId, primaryId]);

  // Explicit renderer preview lifecycle. Default OFF; QAM open never enables it.
  useEffect(() => {
    previewOnRef.current = previewOn;
    rememberRegionPreview(previewOn);
    void (async () => {
      try {
        await setRegionPreviewEnabled(previewOn);
      } catch (err) {
        if (mounted.current) {
          setError(`Preview failed: ${String(err)}`);
        }
      }
    })();
  }, [previewOn]);

  // Push live draft geometry to the renderer preview (no save required).
  useEffect(() => {
    if (!previewOn) {
      return;
    }
    void (async () => {
      try {
        await setRegionPreviewRegions(regionPreviewPayload(drafts, selectedId));
      } catch (err) {
        if (mounted.current) {
          setError(`Preview failed: ${String(err)}`);
        }
      }
    })();
  }, [previewOn, drafts, selectedId, primaryId]);

  // Editor close. If the QAM is still visible this is a transient remount (e.g.
  // a Dropdown context menu): keep the renderer preview and the editor session
  // so Preview/selection survive. A genuine QAM close is handled below.
  useEffect(
    () => () => {
      if (qamVisibleRef.current) {
        return;
      }
      if (previewOnRef.current) {
        void setRegionPreviewEnabled(false);
        void clearRegionPreviewRegions();
      }
      clearRegionPreview();
      resetRegionEditorSession();
      resetRegionEditorDraftState();
    },
    [],
  );

  useEffect(() => {
    const wasVisible = prevQamVisibleRef.current;
    prevQamVisibleRef.current = qamVisible;
    qamVisibleRef.current = qamVisible;
    if (qamVisible) {
      if (!wasVisible) {
        // QAM reopened while this component stayed mounted: hydrate fresh.
        void load();
      }
      return;
    }
    if (!wasVisible) {
      // Initial render before visibility is reported: nothing to tear down.
      return;
    }
    // QAM genuinely closed: clear the renderer preview and forget the session.
    previewOnRef.current = false;
    setPreviewOnState(false);
    resetRegionEditorSession();
    resetRegionEditorDraftState();
    void setRegionPreviewEnabled(false);
    void clearRegionPreviewRegions();
    clearRegionPreview();
  }, [qamVisible]);

  const addRegion = () => {
    if (!canAddRegion(drafts)) {
      return;
    }
    const draft = newRegionDraft(drafts.length);
    setDrafts((current) => [...current, draft]);
    selectRegion(draft.region_id);
  };

  const removeSelected = () => {
    if (!selected) {
      return;
    }
    selectRegion(nextSelectionAfterRemove(drafts, selectedIdRef.current ?? selected.region_id));
    setDrafts((current) => removeRegion(current, selected.region_id));
  };

  // Reorder the selected enabled region to the first enabled slot (Primary).
  // Disabled regions are never silently enabled; already-primary is a no-op.
  const setPrimary = () => {
    if (!selected) {
      return;
    }
    setDrafts((current) => setPrimaryRegion(current, selected.region_id));
  };

  // Region Set (profile) operations persist immediately. They never start or
  // restart OCR / the renderer; a running OCR session keeps its Start snapshot.
  const selectProfile = async (profileId: string) => {
    if (!profileId || profileId === activeProfileId) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await regionProfileSelect(profileId);
      if (mounted.current) {
        if (result.ok === false) {
          setError(result.detail || result.error || "Select Region Set failed");
        } else {
          applyProfilesPayload(result);
          await loadActiveRegions();
        }
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Select Region Set failed: ${String(err)}`);
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  const addProfile = async () => {
    if (profiles.length >= maxProfiles) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await regionProfileAdd();
      if (mounted.current) {
        if (result.ok === false) {
          setError(result.detail || result.error || "Add Region Set failed");
        } else {
          applyProfilesPayload(result);
          await loadActiveRegions();
        }
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Add Region Set failed: ${String(err)}`);
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  const deleteProfile = async () => {
    if (!activeProfileId || profiles.length <= 1) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await regionProfileDelete(activeProfileId);
      if (mounted.current) {
        if (result.ok === false) {
          setError(result.detail || result.error || "Delete Region Set failed");
        } else {
          applyProfilesPayload(result);
          await loadActiveRegions();
        }
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Delete Region Set failed: ${String(err)}`);
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  // One explicit production save: region drafts + the selected region's
  // appearance. Never starts/stops/restarts OCR or the renderer. A partial
  // failure is surfaced, not swallowed.
  const saveChanges = async () => {
    const message = validateRegions(drafts);
    if (message) {
      setError(message);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await regionConfigSet(draftsForApply(drafts, configured), scopeAppId("global", CURRENT_APP_ID));
      if (result.ok === false) {
        setError(result.detail || result.error || "Save failed");
        return;
      }
      applyPayload(result);
      if (selectedId) {
        const appearance = await regionAppearanceSave(selectedId);
        if (appearance && appearance.ok === false) {
          setError(appearance.detail || appearance.error || "Save appearance failed");
          return;
        }
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Save failed: ${String(err)}`);
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  const profileOptions = useMemo(
    () => profiles.map((profile) => ({ data: profile.profile_id, label: profile.label })),
    [profiles],
  );
  const regionOptions = useMemo(
    () =>
      drafts.map((region, index) => ({
        data: region.region_id,
        label: regionLabel(region, index, primaryId),
      })),
    [drafts, primaryId],
  );

  const selectedIsPrimary = !!selected && selected.region_id === primaryId;
  const primaryDisabled = !selected || !selected.enabled || selectedIsPrimary || busy;

  return (
    <PanelSection title="Regions">
      <PanelSectionRow>
        <div style={sectionHeadingStyle}>PROFILE</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={selectorRowStyle}>
          <Dropdown
            rgOptions={profileOptions}
            selectedOption={activeProfileId}
            disabled={busy || profileOptions.length === 0}
            onChange={(option) => void selectProfile(dropdownOptionValue(option) ?? "")}
          />
          <button
            type="button"
            aria-label="Add Region Set"
            disabled={busy || profiles.length >= maxProfiles}
            onClick={() => void addProfile()}
            style={compactButtonStyle(busy || profiles.length >= maxProfiles)}
          >
            +
          </button>
          <button
            type="button"
            aria-label="Delete Region Set"
            disabled={busy || profiles.length <= 1}
            onClick={() => void deleteProfile()}
            style={compactButtonStyle(busy || profiles.length <= 1)}
          >
            -
          </button>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={sectionHeadingStyle}>REGION</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={selectorRowStyle}>
          <Dropdown
            rgOptions={regionOptions}
            selectedOption={selectedId}
            disabled={busy || regionOptions.length === 0}
            onChange={(option) => selectRegion(dropdownOptionValue(option))}
          />
          <button
            type="button"
            aria-label="Add Region"
            disabled={!canAddRegion(drafts) || busy}
            onClick={addRegion}
            style={compactButtonStyle(!canAddRegion(drafts) || busy)}
          >
            +
          </button>
          <button
            type="button"
            aria-label="Delete Region"
            disabled={!selected || busy}
            onClick={removeSelected}
            style={compactButtonStyle(!selected || busy)}
          >
            -
          </button>
        </div>
      </PanelSectionRow>
      {drafts.length === 0 ? (
        <PanelSectionRow>
          <div style={hintStyle}>No regions. Add one with + above.</div>
        </PanelSectionRow>
      ) : null}
      <PanelSectionRow>
        <div style={rowActionsStyle}>
          <ButtonItem layout="below" disabled={primaryDisabled} onClick={setPrimary}>
            {selectedIsPrimary ? "Primary Region" : "Set as Primary"}
          </ButtonItem>
          <ButtonItem layout="below" disabled={busy} onClick={() => setPreviewOn(!previewOn)}>
            {previewOn ? "[x] Show Region Preview" : "[ ] Show Region Preview"}
          </ButtonItem>
        </div>
      </PanelSectionRow>
      {selected ? (
        <>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() =>
                setDrafts((current) => setRegionEnabled(current, selected.region_id, !selected.enabled))
              }
            >
              {selected.enabled ? "[x] Enabled" : "[ ] Enabled"}
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={sectionHeadingStyle}>AREA (% OF SCREEN)</div>
          </PanelSectionRow>
          <GeometrySlider
            label="X%"
            min={0}
            max={99}
            value={Math.round(selected.x * 100)}
            onChange={(value) =>
              setDrafts((current) => updateRegionGeometry(current, selected.region_id, { x: value / 100 }))
            }
          />
          <GeometrySlider
            label="Y%"
            min={0}
            max={99}
            value={Math.round(selected.y * 100)}
            onChange={(value) =>
              setDrafts((current) => updateRegionGeometry(current, selected.region_id, { y: value / 100 }))
            }
          />
          <GeometrySlider
            label="W%"
            min={2}
            max={100}
            value={Math.round(selected.w * 100)}
            onChange={(value) =>
              setDrafts((current) => updateRegionGeometry(current, selected.region_id, { w: value / 100 }))
            }
          />
          <GeometrySlider
            label="H%"
            min={2}
            max={100}
            value={Math.round(selected.h * 100)}
            onChange={(value) =>
              setDrafts((current) => updateRegionGeometry(current, selected.region_id, { h: value / 100 }))
            }
          />
          <PanelSectionRow>
            <div style={sectionHeadingStyle}>APPEARANCE</div>
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={rowActionsStyle}>
              <ButtonItem
                layout="below"
                disabled={busy}
                onClick={() => void choosePanelStyle(PANEL_STYLE_WHITE_ON_BLACK)}
              >
                {selectedStyle === PANEL_STYLE_WHITE_ON_BLACK ? "[x] " : "[ ] "}
                {panelStyleLabel(PANEL_STYLE_WHITE_ON_BLACK)}
              </ButtonItem>
              <ButtonItem
                layout="below"
                disabled={busy}
                onClick={() => void choosePanelStyle(PANEL_STYLE_BLACK_ON_WHITE)}
              >
                {selectedStyle === PANEL_STYLE_BLACK_ON_WHITE ? "[x] " : "[ ] "}
                {panelStyleLabel(PANEL_STYLE_BLACK_ON_WHITE)}
              </ButtonItem>
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={hintStyle}>Text size</div>
          </PanelSectionRow>
          <GeometrySlider
            label="Size"
            min={MIN_REGION_FONT_SIZE}
            max={MAX_REGION_FONT_SIZE}
            step={REGION_FONT_SIZE_STEP}
            value={selectedFontSize}
            onChange={changeFontSize}
          />
        </>
      ) : null}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={() => void saveChanges()}>
          Save Changes
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={statusStyle}>
          {error ? <div style={errorStyle}>{error}</div> : null}
          {config?.last_error ? <div style={errorStyle}>{config.last_error}</div> : null}
          <div style={hintStyle}>
            Changes apply on next OCR start. Up to {MAX_REGIONS} regions.
          </div>
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
}

type GeometrySliderProps = {
  label: string;
  min: number;
  max: number;
  value: number;
  onChange: (value: number) => void;
  step?: number;
};

function GeometrySlider({ label, min, max, value, onChange, step }: GeometrySliderProps) {
  return (
    <PanelSectionRow>
      <label style={sliderLabelStyle}>
        <span style={sliderTitleStyle}>{label}</span>
        <input
          max={max}
          min={min}
          onChange={(event) => onChange(Number(event.currentTarget.value))}
          step={step}
          style={sliderStyle}
          type="range"
          value={value}
        />
        <span style={sliderValueStyle}>{value}</span>
      </label>
    </PanelSectionRow>
  );
}

const rowActionsStyle: CSSProperties = {
  display: "grid",
  gap: "8px",
  gridTemplateColumns: "1fr 1fr",
  width: "100%",
};

const selectorRowStyle: CSSProperties = {
  alignItems: "center",
  display: "grid",
  gap: "6px",
  gridTemplateColumns: "minmax(0, 1fr) 30px 30px",
  width: "100%",
};

const compactButtonStyle = (disabled: boolean): CSSProperties => ({
  alignItems: "center",
  background: disabled ? "#2a2e36" : "#3d4450",
  border: "1px solid #5a6270",
  borderRadius: "4px",
  color: disabled ? "#8a8f98" : "#f5f5f5",
  cursor: disabled ? "default" : "pointer",
  display: "flex",
  flex: "0 0 auto",
  fontSize: "18px",
  fontWeight: 700,
  height: "30px",
  justifyContent: "center",
  lineHeight: 1,
  minWidth: 0,
  padding: 0,
  width: "30px",
});

const statusStyle: CSSProperties = {
  color: "#d9d9d9",
  display: "grid",
  fontSize: "12px",
  gap: "4px",
  lineHeight: 1.35,
};

const sectionHeadingStyle: CSSProperties = {
  color: "#9fb3c8",
  fontSize: "11px",
  fontWeight: 700,
  letterSpacing: "0.08em",
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
