function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status,
    headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

export function createRadarBridge(original) {
  return {
    async fetch(request, env, ctx) {
      if (request.method === "GET" && new URL(request.url).pathname === "/__r6585/builder-health") {
        try { return json(await env.R6585_BUILDER.probe()); }
        catch { return json({ revision: "R6585", error: "BUILDER_PROBE_UNAVAILABLE" }, 503); }
      }
      return original.fetch.call(original, request, env, ctx);
    },
    scheduled(controller, env, ctx) {
      // Capture synchronous RPC errors too; keep the original scheduled context.
      ctx.waitUntil(Promise.resolve().then(() => env.R6585_BUILDER.tick())
        .then(result => console.log(JSON.stringify({ revision: "R6585", status: result.status,
          target_day: result.target_day })))
        .catch(() => console.error("R6585_BUILDER_RPC_FAILED")));
      return original.scheduled.call(original, controller, env, ctx);
    },
  };
}
