import {
  cacheControlForStatus,
  isRedirectStatus,
  maptilerKey,
  publicConfig,
  rewriteMaptilerStyle,
  upstreamMaptilerUrl,
} from "../lib/server/config";

const STYLE_PATH = "/maps/outdoor-v2/style.json";

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
    } catch (error) {
      console.error(
        JSON.stringify({
          message: "api request failed",
          path: url.pathname,
          error: error instanceof Error ? error.message : "unknown",
        }),
      );
      return text("upstream failed", 502);
    }
  },
} satisfies ExportedHandler<Env>;

function text(body: string, status: number): Response {
  return new Response(body, { status, headers: { "cache-control": "no-store" } });
}

function json(body: unknown, method: string): Response {
  return new Response(method === "HEAD" ? null : JSON.stringify(body), {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

async function proxyStyle(request: Request, key: string): Promise<Response> {
  const upstream = await fetch(`https://api.maptiler.com${STYLE_PATH}?key=${encodeURIComponent(key)}`, {
    method: request.method,
    redirect: "manual",
  });
  if (isRedirectStatus(upstream.status)) return text("upstream redirect refused", 502);
  if (!upstream.ok) return text("basemap unavailable", 404);
  const style = rewriteMaptilerStyle(await upstream.json(), new URL(request.url).origin);
  return new Response(request.method === "HEAD" ? null : JSON.stringify(style), {
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": cacheControlForStatus(upstream.status),
    },
  });
}

async function proxyUpstream(request: Request, url: URL, key: string): Promise<Response> {
  const target = upstreamMaptilerUrl(url.pathname, url.search, key);
  if (!target) return text("not found", 404);
  const upstream = await fetch(target, { method: request.method, redirect: "manual" });
  if (isRedirectStatus(upstream.status)) return text("upstream redirect refused", 502);
  const cache = cacheControlForStatus(upstream.status);
  const contentType = upstream.headers.get("content-type") ?? "application/octet-stream";
  if (contentType.includes("json") && request.method !== "HEAD") {
    const rewritten = rewriteMaptilerStyle(await upstream.json(), url.origin);
    return new Response(JSON.stringify(rewritten), {
      status: upstream.status,
      headers: { "content-type": "application/json; charset=utf-8", "cache-control": cache },
    });
  }
  return new Response(request.method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    headers: { "content-type": contentType, "cache-control": cache },
  });
}
