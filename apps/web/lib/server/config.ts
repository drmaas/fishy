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

const MAPTILER_URL = /https:\/\/api\.maptiler\.com(\/[^?\s]*)(\?[^"\s]*)?/g;

/** Outdoor light/dark styles, sprites, TileJSON, tiles, and glyphs. Nothing else. */
const ALLOWED_UPSTREAM = [
  /^\/maps\/outdoor-v2\/style\.json$/,
  /^\/maps\/outdoor-v2\/sprite(?:@2x)?(?:\.(?:json|png))?$/,
  /^\/maps\/outdoor-v2-dark\/style\.json$/,
  /^\/maps\/outdoor-v2-dark\/sprite(?:@2x)?(?:\.(?:json|png))?$/,
  /^\/sprites\/[A-Za-z0-9._-]+\/sprite(?:@2x)?(?:\.(?:json|png))?$/,
  /^\/tiles\/[A-Za-z0-9._-]+\/tiles\.json$/,
  /^\/tiles\/[A-Za-z0-9._-]+\/\d+\/\d+\/\d+(?:\.(?:pbf|mvt|png|jpe?g|webp))?$/,
  /^\/fonts\/[^/]+\/\d+-\d+\.pbf$/,
];

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

export function isRedirectStatus(status: number): boolean {
  return status >= 300 && status < 400;
}

export function cacheControlForStatus(status: number): string {
  if (status >= 200 && status < 300) return "public, max-age=86400";
  return "no-store";
}

/** True when a proxied body or header value would reveal the upstream key or host. */
export function exposesMaptilerSecret(value: string, key: string): boolean {
  if (value.includes("api.maptiler.com")) return true;
  if (key.length > 0 && (value.includes(key) || value.includes(encodeURIComponent(key)))) return true;
  return false;
}

const encoder = new TextEncoder();
const MAPTILER_HOST_BYTES = encoder.encode("api.maptiler.com");

function containsBytes(haystack: Uint8Array, needle: Uint8Array): boolean {
  if (needle.length === 0 || haystack.length < needle.length) return false;
  outer: for (let i = 0; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) continue outer;
    }
    return true;
  }
  return false;
}

/** String-free scan for secrets in binary proxied bodies (tiles, sprites, glyphs). */
export function exposesMaptilerSecretBytes(data: Uint8Array, key: string): boolean {
  if (containsBytes(data, MAPTILER_HOST_BYTES)) return true;
  if (key.length > 0) {
    if (containsBytes(data, encoder.encode(key))) return true;
    if (containsBytes(data, encoder.encode(encodeURIComponent(key)))) return true;
  }
  return false;
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

export function isAllowedMaptilerPath(path: string): boolean {
  if (!path.startsWith("/") || path.includes("\\") || path.includes("\0")) return false;
  const decoded = path.split("/").map(decodeSegment).join("/");
  if (decoded.includes("..") || decoded.includes("//")) return false;
  return ALLOWED_UPSTREAM.some((pattern) => pattern.test(decoded));
}

function rewriteMaptilerUrl(value: string, origin: string): string {
  return value.replace(MAPTILER_URL, (_match, path: string, query: string | undefined) => {
    const params = new URLSearchParams((query ?? "").replace(/^\?/, ""));
    params.delete("key");
    const rest = params.toString();
    return `${origin}/api/basemap/upstream${path}${rest ? `?${rest}` : ""}`;
  });
}

function rewriteValue(value: unknown, origin: string): unknown {
  if (typeof value === "string") return rewriteMaptilerUrl(value, origin);
  if (Array.isArray(value)) return value.map((item) => rewriteValue(item, origin));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, nested] of Object.entries(value)) out[key] = rewriteValue(nested, origin);
    return out;
  }
  return value;
}

/** Point every MapTiler URL at the Worker and drop embedded keys. Template braces stay literal. */
export function rewriteMaptilerStyle(style: unknown, origin: string): unknown {
  return rewriteValue(style, origin);
}

/**
 * Build an upstream MapTiler URL for an allowed path.
 * Returns null when the path is not style, sprite, TileJSON, a tile, or a glyph.
 * The key is attached here and must not be copied into a JSON body.
 */
export function upstreamMaptilerUrl(pathname: string, search: string, key: string): string | null {
  const raw = pathname.replace(/^\/api\/basemap\/upstream/, "") || "/";
  const path = raw.split("?")[0] ?? "/";
  if (!isAllowedMaptilerPath(path)) return null;
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  params.delete("key");
  params.set("key", key);
  const encoded = path
    .split("/")
    .map((segment) => (segment.includes("{") ? segment : encodeURIComponent(decodeSegment(segment))))
    .join("/");
  return `https://api.maptiler.com${encoded}?${params.toString()}`;
}
