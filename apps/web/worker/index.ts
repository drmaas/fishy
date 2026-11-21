import { maptilerKey, publicConfig, rewriteMaptilerStyle, upstreamMaptilerUrl } from "../lib/server/config";

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
        if (!key) return new Response("basemap unavailable", { status: 404 });
        return proxyStyle(request, key);
      }
      if (url.pathname.startsWith("/api/basemap/upstream/")) {
        if (!key) return new Response("basemap unavailable", { status: 404 });
        return proxyUpstream(request, url, key);
      }
      return new Response("not found", { status: 404 });
    } catch (error) {
      console.error(
        JSON.stringify({
          message: "api request failed",
          path: url.pathname,
          error: error instanceof Error ? error.message : "unknown",
        }),
      );
      return new Response("upstream failed", { status: 502 });
    }
  },
} satisfies ExportedHandler<Env>;

function json(body: unknown, method: string): Response {
  return new Response(method === "HEAD" ? null : JSON.stringify(body), {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

async function proxyStyle(request: Request, key: string): Promise<Response> {
  const upstream = await fetch(`https://api.maptiler.com/maps/outdoor-v2/style.json?key=${encodeURIComponent(key)}`);
  if (!upstream.ok) return new Response("basemap unavailable", { status: 404 });
  const style = rewriteMaptilerStyle(await upstream.json(), new URL(request.url).origin);
  return new Response(request.method === "HEAD" ? null : JSON.stringify(style), {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "public, max-age=300" },
  });
}

async function proxyUpstream(request: Request, url: URL, key: string): Promise<Response> {
  const target = upstreamMaptilerUrl(url.pathname, url.search, key);
  const upstream = await fetch(target, { method: request.method });
  const headers = new Headers(upstream.headers);
  headers.set("cache-control", "public, max-age=86400");
  return new Response(request.method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    headers,
  });
}
