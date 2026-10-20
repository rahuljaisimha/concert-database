// Serves the dashboard (dashboards/index.html in the R2 bucket). Cloudflare
// Access in front of the Worker does the sign-in (docs/HOSTING.md). The bucket
// stays private: this Worker reads that one key and nothing else, so the
// database snapshots and raw archive in the same bucket are never reachable
// from here, whatever happens to the Access settings.
//
// Binding (Settings → Bindings): BUCKET = the concert-database R2 bucket.

const KEY = "dashboards/index.html";

const HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "private, no-store",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
  // The page is self-contained: its own inline script and styles, nothing loaded from elsewhere.
  "content-security-policy":
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; " +
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
};

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", {status: 405});
    const url = new URL(request.url);
    if (url.pathname !== "/" && url.pathname !== "/index.html") return new Response("Not found", {status: 404});
    if (!env.BUCKET) return new Response("No R2 binding named BUCKET (Settings → Bindings)", {status: 500});
    const obj = await env.BUCKET.get(KEY);
    if (!obj) return new Response("No dashboard uploaded yet", {status: 404});
    const headers = new Headers(HEADERS);
    if (obj.uploaded) headers.set("last-modified", obj.uploaded.toUTCString());
    return new Response(request.method === "HEAD" ? null : obj.body, {headers});
  },
};
