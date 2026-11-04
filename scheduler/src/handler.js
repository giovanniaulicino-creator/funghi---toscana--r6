import { runTick, probe } from "./runtime.js";

export function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status,
    headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(runTick(env).then(result => console.log(JSON.stringify(result))));
  },
  async fetch(request, env) {
    if (request.method !== "GET" || new URL(request.url).pathname !== "/health") return json({ error: "Not found" }, 404);
    try { return json(await probe(env)); }
    catch { return json({ revision: "R6585", error: "D1_UNAVAILABLE" }, 503); }
  },
};
