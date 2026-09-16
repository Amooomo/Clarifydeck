// Phase 2L.7 — persistent multi-region Recognition Region editor.
// Uses the authoritative backend v2 region config; the backend remains the
// source of truth. Saving never starts/stops/restarts OCR or the renderer, and
// changes apply on the next explicit OCR start.

import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { callable } from "@decky/api";
import { type CSSProperties, useEffect, useRef, useState } from "react";
import {
  MAX_REGIONS,
  canAddRegion,
  describeSource,
  draftsForApply,
  moveRegion,
  newRegionDraft,
  nextSelectionAfterRemove,
  primaryRegionId,
  regionLabel,
  removeRegion,
  scopeAppId,
  scopeOptions,
  setRegionEnabled,
  setRegionName,
  updateRegionGeometry,
  validateRegions,
  type RegionConfigPayload,
  type RegionDraft,
} from "../regionEditor";

const regionConfigGet = callable<[appId: string | null], RegionConfigPayload>("region_config_get");
const regionConfigSet = callable<[regions: RegionDraft[], appId: string | null], RegionConfigPayload>("region_config_set");
const regionConfigReset = callable<[appId: string | null], RegionConfigPayload>("region_config_reset");

// No app_id source exists in the QAM yet, so "This Game" is unavailable.
const CURRENT_APP_ID: string | null = null;

export function RegionEditorSection() {
  const [config, setConfig] = useState<RegionConfigPayload | undefined>();
  const [drafts, setDrafts] = useState<RegionDraft[]>([]);
  const [configured, setConfigured] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);

  const applyPayload = (result: RegionConfigPayload) => {
    setConfig(result);
    const configuredNow = !!result.configured;
    setConfigured(configuredNow);
    const source = configuredNow ? result.configured_regions ?? [] : result.effective_regions ?? [];
    setDrafts(source.map((region) => ({ ...region })));
    setSelectedId((current) =>
      current && source.some((region) => region.region_id === current)
        ? current
        : source[0]?.region_id ?? null,
    );
  };

  const load = async () => {
    try {
      const result = await regionConfigGet(scopeAppId("global", CURRENT_APP_ID));
      if (mounted.current) {
        applyPayload(result);
        setError(result.ok === false ? describeSource(result.error) : "");
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Could not load regions: ${String(err)}`);
      }
    }
  };

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
    };
  }, []);

  const selected = drafts.find((region) => region.region_id === selectedId) ?? null;
  const primaryId = primaryRegionId(drafts);

  const addRegion = () => {
    if (!canAddRegion(drafts)) {
      return;
    }
    const draft = newRegionDraft(drafts.length);
    setDrafts((current) => [...current, draft]);
    setSelectedId(draft.region_id);
  };

  const removeSelected = () => {
    if (!selected) {
      return;
    }
    setSelectedId((current) => nextSelectionAfterRemove(drafts, current ?? selected.region_id));
    setDrafts((current) => removeRegion(current, selected.region_id));
  };

  const apply = async () => {
    const message = validateRegions(drafts);
    if (message) {
      setError(message);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await regionConfigSet(draftsForApply(drafts, configured), scopeAppId("global", CURRENT_APP_ID));
      if (mounted.current) {
        if (result.ok === false) {
          setError(result.detail || result.error || "Save failed");
        } else {
          applyPayload(result);
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

  const reset = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await regionConfigReset(scopeAppId("global", CURRENT_APP_ID));
      if (mounted.current) {
        applyPayload(result);
      }
    } catch (err) {
      if (mounted.current) {
        setError(`Reset failed: ${String(err)}`);
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  const scopes = scopeOptions(CURRENT_APP_ID !== null);

  return (
    <PanelSection title="Recognition Regions">
      <PanelSectionRow>
        <div style={statusStyle}>
          <div>Scope: Global</div>
          <div>Source: {describeSource(config?.source)}</div>
          {!configured ? (
            <div style={hintStyle}>
              No saved regions for this scope yet; showing the effective regions. Applying will save
              them here.
            </div>
          ) : null}
          {scopes[1].disabled ? <div style={hintStyle}>This Game editing is unavailable here.</div> : null}
          <div>First enabled region is the Primary region shown in the overlay.</div>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={listStyle}>
          {drafts.length === 0 ? <div style={hintStyle}>No regions. Add one below.</div> : null}
          {drafts.map((region, index) => (
            <ButtonItem
              key={region.region_id}
              layout="below"
              onClick={() => setSelectedId(region.region_id)}
            >
              {region.region_id === selectedId ? "[x] " : "[ ] "}
              {regionLabel(region, index, primaryId)}
            </ButtonItem>
          ))}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={rowActionsStyle}>
          <ButtonItem layout="below" disabled={!canAddRegion(drafts) || busy} onClick={addRegion}>
            Add region
          </ButtonItem>
          <ButtonItem layout="below" disabled={!selected || busy} onClick={removeSelected}>
            Remove selected
          </ButtonItem>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={rowActionsStyle}>
          <ButtonItem
            layout="below"
            disabled={!selected || busy}
            onClick={() => selected && setDrafts((current) => moveRegion(current, selected.region_id, -1))}
          >
            Move up
          </ButtonItem>
          <ButtonItem
            layout="below"
            disabled={!selected || busy}
            onClick={() => selected && setDrafts((current) => moveRegion(current, selected.region_id, 1))}
          >
            Move down
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
            <input
              aria-label="Region name"
              onChange={(event) =>
                setDrafts((current) => setRegionName(current, selected.region_id, event.currentTarget.value))
              }
              placeholder="Region name (optional)"
              style={nameStyle}
              type="text"
              value={selected.name ?? ""}
            />
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
        </>
      ) : null}
      <PanelSectionRow>
        <div style={rowActionsStyle}>
          <ButtonItem layout="below" disabled={busy} onClick={apply}>
            Apply
          </ButtonItem>
          <ButtonItem layout="below" disabled={busy} onClick={reset}>
            Reset
          </ButtonItem>
        </div>
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
};

function GeometrySlider({ label, min, max, value, onChange }: GeometrySliderProps) {
  return (
    <PanelSectionRow>
      <label style={sliderLabelStyle}>
        <span style={sliderTitleStyle}>{label}</span>
        <input
          max={max}
          min={min}
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

const rowActionsStyle: CSSProperties = {
  display: "grid",
  gap: "8px",
  gridTemplateColumns: "1fr 1fr",
  width: "100%",
};

const listStyle: CSSProperties = {
  display: "grid",
  gap: "6px",
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

const nameStyle: CSSProperties = {
  background: "rgba(0, 0, 0, 0.35)",
  border: "1px solid rgba(255, 255, 255, 0.2)",
  borderRadius: "6px",
  color: "#f5f5f5",
  padding: "6px 8px",
  width: "100%",
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
