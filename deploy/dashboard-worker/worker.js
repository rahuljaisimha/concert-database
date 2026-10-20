// Serves the dashboard (dashboards/index.html in the R2 bucket) to signed-in
// visitors only. The bucket stays private: this Worker reads that one key and
// nothing else, so the database snapshots and raw archive in the same bucket
// are never reachable from here.
//
// Cloudflare Access does the sign-in. As a second lock, the Worker checks the
// token Access adds to every request it lets through (a signed JWT for this
// Access application), so a request that got around Access (the workers.dev
// URL, a misconfigured policy) is refused.
//
// Bindings (wrangler.toml or the dashboard): BUCKET (the R2 bucket),
// TEAM_DOMAIN ("https://<team>.cloudflareaccess.com"), POLICY_AUD (the Access
// application's "Application Audience (AUD) Tag"). See docs/HOSTING.md.

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

let jwks = null, jwksAt = 0;

function b64url(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(s + "===".slice((s.length + 3) % 4)), c => c.charCodeAt(0));
}

async function keys(team) {
  if (!jwks || Date.now() - jwksAt > 3600_000) {
    const r = await fetch(`${team}/cdn-cgi/access/certs`);
    if (!r.ok) throw new Error("certs " + r.status);
    jwks = (await r.json()).keys;
    jwksAt = Date.now();
  }
  return jwks;
}

async function verify(token, env) {
  const [h, p, s] = token.split(".");
  if (!h || !p || !s) return false;
  const header = JSON.parse(new TextDecoder().decode(b64url(h)));
  const claims = JSON.parse(new TextDecoder().decode(b64url(p)));
  if (header.alg !== "RS256") return false;
  const team = env.TEAM_DOMAIN.replace(/\/+$/, "");
  const jwk = (await keys(team)).find(k => k.kid === header.kid);
  if (!jwk) return false;
  const key = await crypto.subtle.importKey("jwk", jwk, {name: "RSASSA-PKCS1-v1_5", hash: "SHA-256"}, false, ["verify"]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64url(s), new TextEncoder().encode(`${h}.${p}`));
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  const now = Date.now() / 1000;
  return ok && aud.includes(env.POLICY_AUD) && claims.iss === team && claims.exp > now && (claims.nbf || 0) <= now + 60;
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", {status: 405});
    const url = new URL(request.url);
    if (url.pathname !== "/" && url.pathname !== "/index.html") return new Response("Not found", {status: 404});
    if (!env.TEAM_DOMAIN || !env.POLICY_AUD) return new Response("Not configured", {status: 500});
    const token = request.headers.get("cf-access-jwt-assertion");
    let allowed = false;
    try { allowed = !!token && await verify(token, env); } catch (e) { allowed = false; }
    if (!allowed) return new Response("Forbidden", {status: 403});
    const obj = await env.BUCKET.get(KEY);
    if (!obj) return new Response("No dashboard uploaded yet", {status: 404});
    const headers = new Headers(HEADERS);
    if (obj.uploaded) headers.set("last-modified", obj.uploaded.toUTCString());
    return new Response(request.method === "HEAD" ? null : obj.body, {headers});
  },
};
