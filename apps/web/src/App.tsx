import { formatDepth, type DepthUnit, type LakeInfo } from "@fishy/core";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
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
import { LocationToast } from "./LocationToast.tsx";
import {
  locationBannerVisible,
  permissionChangeShouldRetry,
  reduceLocationBanner,
} from "./locationBanner.ts";
import { locationHint } from "./locationHint.ts";
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
  const [locationBanner, dispatchLocationBanner] = useReducer(reduceLocationBanner, {
    location: { status: "idle" },
    dismissed: false,
  });
  const locationRetryRef = useRef<(() => void) | null>(null);
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

  const gpsHint = locationHint(locationBanner.location);
  const mapLocationToastVisible = !chromeOpen && locationBannerVisible(locationBanner);

  const retryLocation = useCallback(() => {
    dispatchLocationBanner({ type: "attempt" });
    locationRetryRef.current?.();
  }, []);

  useEffect(() => {
    const permissions = navigator.permissions;
    if (!permissions?.query) return undefined;

    let disposed = false;
    let permissionStatus: PermissionStatus | undefined;

    const onPermissionChange = () => {
      if (!permissionStatus || !permissionChangeShouldRetry(permissionStatus.state)) return;
      dispatchLocationBanner({ type: "permission_granted" });
      locationRetryRef.current?.();
    };

    void permissions.query({ name: "geolocation" }).then((status) => {
      if (disposed) return;
      permissionStatus = status;
      status.addEventListener("change", onPermissionChange);
    });

    return () => {
      disposed = true;
      permissionStatus?.removeEventListener("change", onPermissionChange);
    };
  }, []);
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
      {mapLocationToastVisible && (
        <LocationToast
          model={locationBanner}
          onDismiss={() => dispatchLocationBanner({ type: "dismiss" })}
          onRetry={retryLocation}
        />
      )}
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
        onLocation={(state) => dispatchLocationBanner({ type: "location", state })}
        onLocationAttempt={() => dispatchLocationBanner({ type: "attempt" })}
        locationRetryRef={locationRetryRef}
      />
      <DepthLegend unit={unit} />
      {lake && (
        <div hidden={!panelOpen}>
          <LakePanel lake={lake} unit={unit} onHide={() => setPanel(false)} />
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
      <div className="credits">
        <button
          type="button"
          className="credits-toggle tap-target"
          aria-expanded={creditsOpen}
          aria-controls="credits-panel"
          onClick={() => setCreditsOpen((open) => !open)}
        >
          Credits
        </button>
        {creditsOpen && (
          <aside className="credits-panel" id="credits-panel" aria-label="Data credits">
            <div className="credits-panel-head">
              <p className="credits-panel-title">Credits</p>
              <button type="button" className="credits-panel-close" onClick={() => setCreditsOpen(false)}>
                Done
              </button>
            </div>
            <ul className="credits-list">
              {basemap !== "none" && (
                <li>
                  <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>
                  {basemap === "openfreemap" ? " · OpenFreeMap" : " · © MapTiler"}
                </li>
              )}
              <li>
                Bathymetry © Minnesota DNR
                {lake ? ` · ${lake.name} ${formatDepth(lake.maxDepthFt, unit)}` : ""}
              </li>
              <li>USGS NHD · public domain</li>
              <li>NOAA NCEI Great Lakes · public domain</li>
              <li>Open-Meteo · CC BY 4.0 (when wired)</li>
              <li>NWS · public domain</li>
            </ul>
          </aside>
        )}
      </div>
    </div>
  );
}
