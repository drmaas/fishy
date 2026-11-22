import { formatDepth, type DepthUnit, type LakeInfo } from "@fishy/core";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DepthLegend } from "./DepthLegend.tsx";
import { FishMap } from "./FishMap.tsx";
import {
  fetchLakeDetail,
  fetchNearbyLakes,
  lakeInfoFromDetail,
  type GeoJsonFeatureCollection,
  type LakeDetail,
  type LakeExtent,
  type LakeSearchHit,
} from "./lakeApi.ts";
import { LakePanel } from "./LakePanel.tsx";
import { LakeSearch } from "./LakeSearch.tsx";
import { locationHint, type LocationHintState } from "./locationHint.ts";
import { STATES, stateById } from "./states.ts";

interface Config {
  basemap: "openfreemap" | "maptiler";
  maptiler: boolean;
  contoursUrl: string;
  terrainUrl: string;
  hillshadeUrl: string;
}

const FALLBACK: Config = {
  basemap: "openfreemap",
  maptiler: false,
  contoursUrl: "/samples/sample-pond.pmtiles",
  terrainUrl: "/samples/sample-pond-terrain.pmtiles",
  hillshadeUrl: "/samples/sample-pond-hillshade.pmtiles",
};

function readRoute(): { stateId: string; lakeId: string | null } {
  if (typeof window === "undefined") return { stateId: "mn", lakeId: null };
  const params = new URLSearchParams(window.location.search);
  const state = stateById(params.get("state"));
  const lake = params.get("lake");
  return { stateId: state.id, lakeId: lake && /^[0-9]{8}$/.test(lake) ? lake : null };
}

function writeRoute(stateId: string, lakeId: string | null) {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams();
  params.set("state", stateId);
  if (lakeId) params.set("lake", lakeId);
  const next = `${window.location.pathname}?${params.toString()}`;
  window.history.replaceState(null, "", next);
}

function readOpenFlag(key: string, fallback: boolean): boolean {
  if (typeof window === "undefined") return fallback;
  const stored = window.localStorage.getItem(key);
  if (stored === "0") return false;
  if (stored === "1") return true;
  return fallback;
}

function writeOpenFlag(key: string, open: boolean) {
  window.localStorage.setItem(key, open ? "1" : "0");
}

export function App() {
  const initial = useMemo(() => readRoute(), []);
  const [config, setConfig] = useState<Config>(FALLBACK);
  const [basemap, setBasemap] = useState<"openfreemap" | "none" | "maptiler">("openfreemap");
  const [unit, setUnit] = useState<DepthUnit>("ft");
  const [stateId, setStateId] = useState(initial.stateId);
  const [lake, setLake] = useState<LakeInfo | null>(null);
  const [selected, setSelected] = useState<LakeDetail | null>(null);
  const [nearby, setNearby] = useState<GeoJsonFeatureCollection | null>(null);
  const [creditsOpen, setCreditsOpen] = useState(false);
  const [loadingLake, setLoadingLake] = useState(false);
  const [locationState, setLocationState] = useState<LocationHintState>({ status: "idle" });
  const [chromeOpen, setChromeOpen] = useState(() => readOpenFlag("fishy-chrome-open", true));
  const [panelOpen, setPanelOpen] = useState(() => readOpenFlag("fishy-panel-open", true));
  const nearbyTimer = useRef<number | null>(null);
  const pendingLake = useRef<string | null>(initial.lakeId);

  const state = stateById(stateId);

  useEffect(() => {
    const stored = window.localStorage.getItem("fishy-units");
    if (stored === "ft" || stored === "m") setUnit(stored);
    void fetch("/api/config")
      .then((response) => (response.ok ? response.json() : null))
      .then((body: Config | null) => {
        if (!body) return;
        setConfig(body);
        if (body.maptiler) setBasemap("maptiler");
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (selected) pendingLake.current = null;
    writeRoute(stateId, selected?.dowlknum ?? pendingLake.current);
  }, [stateId, selected]);

  const selectLake = useCallback(
    async (dowlknum: string, revealPanel = true) => {
      pendingLake.current = dowlknum;
      setLoadingLake(true);
      const detail = await fetchLakeDetail(stateId, dowlknum);
      setLoadingLake(false);
      if (!detail) {
        pendingLake.current = null;
        return;
      }
      setSelected(detail);
      setLake(lakeInfoFromDetail(detail));
      if (revealPanel) {
        setPanelOpen(true);
        writeOpenFlag("fishy-panel-open", true);
      }
    },
    [stateId],
  );

  useEffect(() => {
    if (!initial.lakeId) return;
    void selectLake(initial.lakeId, false);
  }, [initial.lakeId, selectLake]);

  function chooseUnit(next: DepthUnit) {
    setUnit(next);
    window.localStorage.setItem("fishy-units", next);
  }

  function onSearchSelect(hit: LakeSearchHit) {
    void selectLake(hit.dowlknum, true);
  }

  function onCloseLake() {
    pendingLake.current = null;
    setLake(null);
    setSelected(null);
  }

  function setChrome(open: boolean) {
    setChromeOpen(open);
    writeOpenFlag("fishy-chrome-open", open);
  }

  function setPanel(open: boolean) {
    setPanelOpen(open);
    writeOpenFlag("fishy-panel-open", open);
  }

  function onStateChange(next: string) {
    pendingLake.current = null;
    setStateId(next);
    setNearby(null);
    setSelected(null);
    setLake(null);
  }

  const onExtent = useCallback(
    (extent: LakeExtent, zoom: number) => {
      if (nearbyTimer.current !== null) window.clearTimeout(nearbyTimer.current);
      const spanLon = extent.east - extent.west;
      const spanLat = extent.north - extent.south;
      if (zoom < 9 || spanLon > 0.8 || spanLat > 0.8) {
        setNearby(null);
        return;
      }
      nearbyTimer.current = window.setTimeout(() => {
        void fetchNearbyLakes(stateId, extent).then((collection) => {
          setNearby(collection);
        });
      }, 280);
    },
    [stateId],
  );

  const gpsHint = locationHint(locationState);
  const appClass = [
    "app",
    chromeOpen ? "chrome-open" : "chrome-closed",
    lake && panelOpen ? "panel-open" : lake ? "panel-collapsed" : "panel-none",
  ].join(" ");

  return (
    <div className={appClass}>
      <header className="chrome" id="fishy-chrome" hidden={!chromeOpen}>
        <div className="chrome-head">
          <div className="brand">
            <p className="mark">Fishy</p>
            <p className="tag">Lake depth maps</p>
          </div>
          <button
            type="button"
            className="tap-target"
            onClick={() => setChrome(false)}
            aria-expanded="true"
            aria-controls="fishy-chrome"
          >
            Hide
          </button>
        </div>
        <div className="chrome-row">
          <label className="field">
            State
            <select aria-label="State" value={stateId} onChange={(event) => onStateChange(event.target.value)}>
              {STATES.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <LakeSearch stateId={stateId} onSelect={onSearchSelect} />
        </div>
        <div className="chrome-row chrome-tools">
          <div className="units" role="group" aria-label="Depth units">
            <button type="button" aria-pressed={unit === "ft"} onClick={() => chooseUnit("ft")}>
              ft
            </button>
            <button type="button" aria-pressed={unit === "m"} onClick={() => chooseUnit("m")}>
              m
            </button>
          </div>
          <label className="field">
            Basemap
            <select
              aria-label="Basemap"
              value={basemap}
              onChange={(event) => setBasemap(event.target.value as typeof basemap)}
            >
              <option value="openfreemap">OpenFreeMap</option>
              <option value="none">Contours only</option>
              <option value="maptiler" disabled={!config.maptiler}>
                MapTiler
              </option>
            </select>
          </label>
        </div>
        <p className="hint">
          {gpsHint ?? (loadingLake ? "Loading contours…" : `Surveyed lakes in ${state.label}. Search, or zoom in.`)}
        </p>
      </header>
      {!chromeOpen && (
        <button
          type="button"
          className="chrome-show tap-target"
          onClick={() => setChrome(true)}
          aria-expanded="false"
          aria-controls="fishy-chrome"
        >
          Show controls
        </button>
      )}
      {!chromeOpen && gpsHint && <p className="location-toast hint">{gpsHint}</p>}
      <FishMap
        contoursUrl={config.contoursUrl}
        terrainUrl={config.terrainUrl}
        hillshadeUrl={config.hillshadeUrl}
        basemap={basemap}
        maptilerStyleUrl={config.maptiler ? "/api/basemap/style.json" : undefined}
        unit={unit}
        stateBounds={state.bounds}
        nearby={nearby}
        selected={selected}
        onLake={(next) => {
          setLake(next);
          setPanel(true);
        }}
        onSelectDowlknum={(dowlknum) => void selectLake(dowlknum, true)}
        onExtent={onExtent}
        onLocation={setLocationState}
      />
      <DepthLegend unit={unit} />
      {lake && (
        <div hidden={!panelOpen}>
          <LakePanel lake={lake} unit={unit} onClose={onCloseLake} onHide={() => setPanel(false)} />
        </div>
      )}
      {lake && !panelOpen && (
        <button
          type="button"
          className="panel-show tap-target"
          onClick={() => setPanel(true)}
          aria-expanded="false"
          aria-controls="lake-panel"
        >
          {lake.name}
        </button>
      )}
      <footer className="credits">
        <p>
          {basemap !== "none" && (
            <>
              <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>
              {basemap === "openfreemap" ? " · OpenFreeMap" : " · © MapTiler"}
              {" · "}
            </>
          )}
          Bathymetry © Minnesota DNR
          {lake ? ` · ${lake.name} ${formatDepth(lake.maxDepthFt, unit)}` : ""}
        </p>
        <button type="button" onClick={() => setCreditsOpen((open) => !open)}>
          Credits
        </button>
        {creditsOpen && (
          <ul>
            <li>Minnesota DNR lake bathymetry. Access constraints: none. Credit the DNR.</li>
            <li>USGS National Hydrography Dataset is public domain. Credit the U.S. Geological Survey.</li>
            <li>NOAA NCEI Great Lakes bathymetry is public domain. Credit NOAA NCEI.</li>
            <li>
              OpenStreetMap data stays under ODbL. Attribute{" "}
              <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>.
            </li>
            <li>Open-Meteo, when wired, is CC BY 4.0. Credit Open-Meteo.</li>
            <li>National Weather Service data is public domain. Credit NWS.</li>
          </ul>
        )}
      </footer>
    </div>
  );
}
