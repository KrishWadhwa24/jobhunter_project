// Forwards every request to the backend (the gateway on your Mac, through the Cloudflare Tunnel).
// BACKEND_URL (Vercel environment variable) = https://jobs.yourdomain.com
// The request body is passed on explicitly, so form posts such as the sign-in keep their fields.

const HOP = new Set(["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
  "transfer-encoding", "upgrade", "host", "content-length", "x-vercel-id", "x-vercel-forwarded-for",
  "x-vercel-deployment-url", "x-vercel-ip-city", "x-vercel-ip-country", "x-vercel-ip-country-region",
  "x-vercel-ip-latitude", "x-vercel-ip-longitude", "x-vercel-ip-timezone", "x-vercel-proxied-for",
  "x-vercel-proxy-signature", "x-vercel-proxy-signature-ts", "x-vercel-oidc-token", "forwarded"]);

async function handle(request) {
  const backend = (process.env.BACKEND_URL || "").replace(/\/+$/, "");
  if (!/^https:\/\//.test(backend)) {
    return new Response("BACKEND_URL is not set (or is not an https:// address) in this Vercel project's Environment Variables.",
      { status: 500 });
  }
  const incoming = new URL(request.url);
  const params = new URLSearchParams(incoming.search);
  let path = params.get("__p");
  params.delete("__p");
  path = "/" + String(path === null ? incoming.pathname.replace(/^\/api\/proxy\/?/, "") : path).replace(/^\/+/, "");
  const qs = params.toString();
  const target = backend + path + (qs ? "?" + qs : "");

  const headers = new Headers();
  for (const [k, v] of request.headers) {
    if (!HOP.has(k.toLowerCase())) headers.set(k, v);
  }
  const publicHost = request.headers.get("x-forwarded-host") || request.headers.get("host") || incoming.host;
  headers.set("x-forwarded-host", publicHost);
  headers.set("x-forwarded-proto", "https");
  const fwd = request.headers.get("x-forwarded-for");
  if (fwd) headers.set("x-forwarded-for", fwd);
  headers.set("accept-encoding", "identity");

  const hasBody = !["GET", "HEAD"].includes(request.method);
  let upstream;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: "manual",
    });
  } catch (e) {
    return new Response("Could not reach the backend. Is the Mac on, ./jh up running and the Cloudflare tunnel up?", { status: 502 });
  }
  const out = new Headers();
  for (const [k, v] of upstream.headers) {
    const lk = k.toLowerCase();
    if (HOP.has(lk) || lk === "content-encoding" || lk === "set-cookie") continue;
    out.set(k, v);
  }
  const cookies = typeof upstream.headers.getSetCookie === "function" ? upstream.headers.getSetCookie() : [];
  for (const c of cookies) out.append("set-cookie", c);
  return new Response(upstream.status === 204 || upstream.status === 304 ? null : upstream.body, { status: upstream.status, headers: out });
}

export const GET = handle;
export const POST = handle;
export const HEAD = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
