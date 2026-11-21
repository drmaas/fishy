export interface PublicConfig {
  basemap: "openfreemap" | "maptiler";
  maptiler: boolean;
  contoursUrl: string;
  terrainUrl: string;
  hillshadeUrl: string;
}

export interface FishyEnv {
  FISHY_CONTOURS_URL?: string;
  FISHY_TERRAIN_URL?: string;
  FISHY_HILLSHADE_URL?: string;
  MAPTILER_API_KEY?: string;
}

const DEFAULTS = {
  contoursUrl: "/samples/sample-pond.pmtiles",
  terrainUrl: "/samples/sample-pond-terrain.pmtiles",
  hillshadeUrl: "/samples/sample-pond-hillshade.pmtiles",
};

export function maptilerKey(env: FishyEnv): string | undefined {
  const value = env.MAPTILER_API_KEY;
  return value && value.length > 0 ? value : undefined;
}

export function publicConfig(env: FishyEnv): PublicConfig {
  const keyed = maptilerKey(env) !== undefined;
  return {
    basemap: keyed ? "maptiler" : "openfreemap",
    maptiler: keyed,
    contoursUrl: env.FISHY_CONTOURS_URL || DEFAULTS.contoursUrl,
    terrainUrl: env.FISHY_TERRAIN_URL || DEFAULTS.terrainUrl,
    hillshadeUrl: env.FISHY_HILLSHADE_URL || DEFAULTS.hillshadeUrl,
  };
}

function stripKeyParams(value: unknown): unknown {
  if (typeof value === "string") {
    if (value.includes("://")) {
      try {
        const url = new URL(value);
        url.searchParams.delete("key");
        return url.toString();
      } catch {
        return value;
      }
    }
    return value;
  }
  if (Array.isArray(value)) return value.map(stripKeyParams);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, nested] of Object.entries(value)) out[key] = stripKeyParams(nested);
    return out;
  }
  return value;
}

/** Point MapTiler style URLs at the Worker and drop any embedded key. */
export function rewriteMaptilerStyle(style: unknown, origin: string): unknown {
  const text = JSON.stringify(style).replaceAll(
    "https://api.maptiler.com",
    `${origin}/api/basemap/upstream`,
  );
  return stripKeyParams(JSON.parse(text));
}

export function upstreamMaptilerUrl(pathname: string, search: string, key: string): string {
  const rest = pathname.replace(/^\/api\/basemap\/upstream/, "") || "/";
  const url = new URL(`https://api.maptiler.com${rest}`);
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  params.delete("key");
  params.set("key", key);
  url.search = params.toString();
  return url.toString();
}
