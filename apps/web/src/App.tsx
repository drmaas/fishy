import { formatDepth, type DepthUnit, type LakeInfo } from "@fishy/core";
import { useEffect, useState } from "react";
import { FishMap } from "./FishMap.tsx";
import { LakePanel } from "./LakePanel.tsx";

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

export function App() {
  const [config, setConfig] = useState<Config>(FALLBACK);
  const [basemap, setBasemap] = useState<"openfreemap" | "none" | "maptiler">("openfreemap");
  const [unit, setUnit] = useState<DepthUnit>("ft");
  const [lake, setLake] = useState<LakeInfo | null>(null);
  const [creditsOpen, setCreditsOpen] = useState(false);

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

  function chooseUnit(next: DepthUnit) {
    setUnit(next);
    window.localStorage.setItem("fishy-units", next);
  }

  return (
    <div className="app">
      <header className="top">
        <div>
          <p className="mark">Fishy</p>
          <p className="tag">Lake depth maps</p>
        </div>
        <label>
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
        <div className="units" role="group" aria-label="Depth units">
          <button type="button" aria-pressed={unit === "ft"} onClick={() => chooseUnit("ft")}>
            ft
          </button>
          <button type="button" aria-pressed={unit === "m"} onClick={() => chooseUnit("m")}>
            m
          </button>
        </div>
      </header>
      <FishMap
        contoursUrl={config.contoursUrl}
        terrainUrl={config.terrainUrl}
        hillshadeUrl={config.hillshadeUrl}
        basemap={basemap}
        maptilerStyleUrl={config.maptiler ? "/api/basemap/style.json" : undefined}
        unit={unit}
        onLake={setLake}
      />
      {lake && <LakePanel lake={lake} unit={unit} onClose={() => setLake(null)} />}
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
