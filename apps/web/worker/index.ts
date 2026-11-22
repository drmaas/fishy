import {
  cacheControlForStatus,
  exposesMaptilerSecret,
  exposesMaptilerSecretBytes,
  isRedirectStatus,
  maptilerKey,
  publicConfig,
  rewriteMaptilerStyle,
  upstreamMaptilerUrl,
} from "../lib/server/config";
import {
  buildLakeDetail,
  buildSearchWhere,
  contoursQueryUrl,
  dnrFetchHeaders,
  extentQueryUrl,
  isSupportedState,
  LAKE_CACHE_CONTROL,
  LAKE_DETAIL_CACHE_REV,
  maxDepthQueryUrl,
  nearbyQueryUrl,
  normalizeOutlineCollection,
  normalizeSearchHits,
  outlineQueryUrl,
  parseBbox,
  parseExtent,
  parseMaxDepth,
  sanitizeSearchQuery,
  searchQueryUrl,
  shoreAreaQueryUrl,
  type GeoJsonFeatureCollection,
} from "../lib/server/lakes";
import { isLakeIdForState, lakeIdFromDetailPath } from "@fishy/core";

const STYLE_PATH = "/maps/outdoor-v2/style.json";
const DNR_HOST = "enterprise.gisdata.mn.gov";

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) {
      return new Response(null, { status: 404 });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed", { status: 405 });
    }
    try {
      if (url.pathname === "/api/config") {
        return json(publicConfig(env), request.method);
      }
      const lakeResponse = await handleLakes(request, url);
      if (lakeResponse) return lakeResponse;
      const key = maptilerKey(env);
      if (url.pathname === "/api/basemap/style.json") {
        if (!key) return text("basemap unavailable", 404);
        return proxyStyle(request, key);
      }
      if (url.pathname.startsWith("/api/basemap/upstream/")) {
        if (!key) return text("basemap unavailable", 404);
        return proxyUpstream(request, url, key);
      }
      return text("not found", 404);
    } catch {
      console.error(JSON.stringify({ message: "api request failed", path: url.pathname }));
      return text("upstream failed", 502);
    }
  },
} satisfies ExportedHandler<Env>;

function text(body: string, status: number): Response {
  return new Response(body, { status, headers: { "cache-control": "no-store" } });
}

function json(body: unknown, method: string, cacheControl = "no-store"): Response {
  return new Response(method === "HEAD" ? null : JSON.stringify(body), {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": cacheControl },
  });
}

async function handleLakes(request: Request, url: URL): Promise<Response | null> {
  if (url.pathname === "/api/lakes") {
    return cachedLakeResponse(request, () => searchLakes(request, url));
  }
  if (url.pathname === "/api/lakes/nearby") {
    return cachedLakeResponse(request, () => nearbyLakes(request, url));
  }
  const lakeId = lakeIdFromDetailPath(url.pathname);
  if (lakeId !== null) {
    return cachedLakeResponse(request, () => lakeDetail(request, url, lakeId));
  }
  if (url.pathname.startsWith("/api/lakes/")) {
    return text("not found", 404);
  }
  return null;
}

function lakeCacheKey(request: Request): Request {
  const url = new URL(request.url);
  if (lakeIdFromDetailPath(url.pathname) !== null) {
    url.searchParams.set("_rev", LAKE_DETAIL_CACHE_REV);
  }
  return new Request(url.toString(), { method: request.method, headers: request.headers });
}

async function cachedLakeResponse(request: Request, build: () => Promise<Response>): Promise<Response> {
  const cache = typeof caches !== "undefined" ? caches.default : undefined;
  const key = lakeCacheKey(request);
  if (cache) {
    const hit = await cache.match(key);
    if (hit) return hit;
  }
  const response = await build();
  if (cache && response.ok && response.headers.get("cache-control") === LAKE_CACHE_CONTROL) {
    await cache.put(key, response.clone());
  }
  return response;
}

async function searchLakes(request: Request, url: URL): Promise<Response> {
  if (!isSupportedState(url.searchParams.get("state"))) return text("not found", 404);
  const state = url.searchParams.get("state");
  const query = sanitizeSearchQuery(url.searchParams.get("q"), state);
  if (!query) return text("not found", 404);
  const upstream = await dnrFetch(searchQueryUrl(buildSearchWhere(query, state!)));
  if (!upstream.ok) return text("upstream failed", 502);
  const body = (await upstream.json()) as {
    features?: Array<{ attributes?: Record<string, unknown> }>;
    error?: unknown;
  };
  if (body.error) return text("upstream failed", 502);
  return json({ lakes: normalizeSearchHits(body.features ?? []) }, request.method, LAKE_CACHE_CONTROL);
}

async function nearbyLakes(request: Request, url: URL): Promise<Response> {
  if (!isSupportedState(url.searchParams.get("state"))) return text("not found", 404);
  const bbox = parseBbox(
    url.searchParams.get("west"),
    url.searchParams.get("south"),
    url.searchParams.get("east"),
    url.searchParams.get("north"),
  );
  if (!bbox) return text("not found", 404);
  const upstream = await dnrFetch(nearbyQueryUrl(bbox));
  if (!upstream.ok) return text("upstream failed", 502);
  const collection = (await upstream.json()) as GeoJsonFeatureCollection;
  if (collection.type !== "FeatureCollection" || !Array.isArray(collection.features)) {
    return text("upstream failed", 502);
  }
  return json(normalizeOutlineCollection(collection), request.method, LAKE_CACHE_CONTROL);
}

async function lakeDetail(request: Request, url: URL, lakeId: string): Promise<Response> {
  const state = url.searchParams.get("state");
  if (!isSupportedState(state)) return text("not found", 404);
  if (!isLakeIdForState(state!, lakeId)) return text("not found", 404);

  const [extentRes, outlineRes, contoursRes, maxDepthRes, shoreRes] = await Promise.all([
    dnrFetch(extentQueryUrl(lakeId)),
    dnrFetch(outlineQueryUrl(lakeId)),
    dnrFetch(contoursQueryUrl(lakeId)),
    dnrFetch(maxDepthQueryUrl(lakeId)),
    dnrFetch(shoreAreaQueryUrl(lakeId)),
  ]);
  if (![extentRes, outlineRes, contoursRes, maxDepthRes, shoreRes].every((response) => response.ok)) {
    return text("upstream failed", 502);
  }

  const extentBody = (await extentRes.json()) as { extent?: Record<string, unknown> | null };
  const outline = (await outlineRes.json()) as GeoJsonFeatureCollection;
  const contours = (await contoursRes.json()) as GeoJsonFeatureCollection;
  const maxDepthBody = (await maxDepthRes.json()) as {
    features?: Array<{ attributes?: Record<string, unknown> }>;
  };
  const shoreBody = (await shoreRes.json()) as {
    features?: Array<{ attributes?: Record<string, unknown> }>;
  };

  const extent = parseExtent(extentBody);
  const maxDepthFt = parseMaxDepth(maxDepthBody);
  const shore = shoreBody.features?.[0]?.attributes;
  if (!extent || maxDepthFt === null || !shore) return text("not found", 404);
  if (outline.type !== "FeatureCollection" || contours.type !== "FeatureCollection") {
    return text("upstream failed", 502);
  }
  if (outline.features.length === 0 || contours.features.length === 0) return text("not found", 404);

  const acres = shore.acres;
  const areaAcres = typeof acres === "number" && Number.isFinite(acres) ? acres : null;
  if (areaAcres === null) return text("not found", 404);
  const nameValue = shore.lake_name;
  const name = typeof nameValue === "string" && nameValue.trim() !== "" ? nameValue : "Unnamed lake";

  return json(
    buildLakeDetail({
      dowlknum: lakeId,
      name,
      areaAcres,
      maxDepthFt,
      extent,
      outline,
      contours,
    }),
    request.method,
    LAKE_CACHE_CONTROL,
  );
}

async function dnrFetch(target: string): Promise<Response> {
  const upstream = await fetch(target, {
    method: "GET",
    redirect: "manual",
    headers: dnrFetchHeaders(),
  });
  if (isRedirectStatus(upstream.status)) return text("upstream failed", 502);
  try {
    if (new URL(upstream.url || target).host !== DNR_HOST) return text("upstream failed", 502);
  } catch {
    return text("upstream failed", 502);
  }
  return upstream;
}

async function proxyStyle(request: Request, key: string): Promise<Response> {
  const upstream = await fetch(`https://api.maptiler.com${STYLE_PATH}?key=${encodeURIComponent(key)}`, {
    method: request.method,
    redirect: "manual",
  });
  if (refused(upstream)) return text("upstream redirect refused", 502);
  if (!upstream.ok) return text("basemap unavailable", upstream.status);
  const body = JSON.stringify(rewriteMaptilerStyle(await upstream.json(), new URL(request.url).origin));
  if (exposesMaptilerSecret(body, key)) return text("basemap unavailable", 502);
  return new Response(request.method === "HEAD" ? null : body, {
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": cacheControlForStatus(upstream.status),
    },
  });
}

function refused(upstream: Response): boolean {
  if (isRedirectStatus(upstream.status)) return true;
  if (!upstream.url) return false;
  try {
    return new URL(upstream.url).host !== "api.maptiler.com";
  } catch {
    return true;
  }
}

async function proxyUpstream(request: Request, url: URL, key: string): Promise<Response> {
  const target = upstreamMaptilerUrl(url.pathname, url.search, key);
  if (!target) return text("not found", 404);
  const upstream = await fetch(target, { method: request.method, redirect: "manual" });
  if (refused(upstream)) return text("upstream redirect refused", 502);
  if (!upstream.ok) return text("basemap unavailable", upstream.status);
  const buffer = await upstream.arrayBuffer();
  const contentType = upstream.headers.get("content-type") ?? "application/octet-stream";
  if (contentType.includes("json")) {
    const raw = new TextDecoder().decode(buffer);
    let body: string;
    try {
      body = JSON.stringify(rewriteMaptilerStyle(JSON.parse(raw), url.origin));
    } catch {
      return text("basemap unavailable", 502);
    }
    if (exposesMaptilerSecret(body, key)) return text("basemap unavailable", 502);
    return new Response(request.method === "HEAD" ? null : body, {
      status: 200,
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": cacheControlForStatus(200),
      },
    });
  }
  const bytes = new Uint8Array(buffer);
  if (exposesMaptilerSecretBytes(bytes, key) || exposesMaptilerSecret(contentType, key)) {
    return text("basemap unavailable", 502);
  }
  return new Response(request.method === "HEAD" ? null : buffer, {
    status: upstream.status,
    headers: {
      "content-type": contentType,
      "cache-control": cacheControlForStatus(upstream.status),
    },
  });
}
