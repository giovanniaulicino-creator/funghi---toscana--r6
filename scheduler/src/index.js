import { check, dailyActive, romeClock, STATE_KEY } from "./core.js";

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status,
    headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

export default {
  async scheduled(_controller, env, ctx) {
    // Use actual time, not a stale cron event's requested day.
    ctx.waitUntil(check(env).then(result => console.log(JSON.stringify(result))));
  },
  async fetch(request, env) {
    if (request.method !== "GET" || new URL(request.url).pathname !== "/health") return json({ error: "Not found" }, 404);
    try {
      const day = romeClock(Date.now()).day;
      const active = await dailyActive(env.DB, day);
      const row = await env.DB.prepare("SELECT value FROM system_state WHERE key=?").bind(STATE_KEY).first();
      const state = row ? JSON.parse(row.value) : null;
      // Public health is read-only; there is deliberately no public dispatch route.
      return json({ revision: "R6584", target_day: day, ready: Boolean(active),
        generation: active?.generation || null, token_configured: Boolean(env.GITHUB_ACTIONS_TOKEN), last_check: state });
    } catch { return json({ revision: "R6584", error: "D1_UNAVAILABLE" }, 503); }
  },
};
