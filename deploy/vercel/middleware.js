// Vercel Routing Middleware: sends every request to the backend that runs on your Mac (through a Cloudflare Tunnel).
// The backend address comes from the BACKEND_URL environment variable in Vercel, e.g. https://jobs.yourdomain.com
// so the address is not written into the code and can be changed without editing files.
export const config = { matcher: "/:path*" };

export default function middleware(request) {
  const backend = (process.env.BACKEND_URL || "").replace(/\/+$/, "");
  if (!/^https:\/\//.test(backend)) {
    return new Response("BACKEND_URL is not set in this Vercel project's Environment Variables.", { status: 500 });
  }
  const url = new URL(request.url);
  // Same mechanism the official rewrite() helper uses: an external rewrite keeps the browser on the Vercel address.
  return new Response(null, { headers: { "x-middleware-rewrite": backend + url.pathname + url.search } });
}
