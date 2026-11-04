// Boot the deployed entrypoint and its named RPC in actual workerd, not a JS mock.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { Miniflare, convertV4MiniflareOptions } = require("miniflare");
const { buildSync } = require("esbuild");
const root = path.resolve(__dirname, "..");
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "r6585-runtime-"));
const bundle = file => buildSync({ entryPoints: [file], bundle: true, format: "esm", write: false,
  external: ["cloudflare:workers"], logLevel: "silent" }).outputFiles[0].text;

(async () => {
  for (const name of ["bridge.js", "r6585-radar-entry.js"]) fs.copyFileSync(path.join(root, "radar-bridge", name), path.join(temp, name));
  const original = process.env.R6585_RADAR_SOURCE;
  const stub = `export const __test = Object.freeze({ marker: () => 'original' });
    export default { async fetch(r, env) { return new Response('original:' + new URL(r.url).pathname); },
      scheduled(c, env, ctx) { ctx.waitUntil(env.RADAR_KV.put('radar-cron', 'completed')); } };`;
  fs.writeFileSync(path.join(temp, "worker.js"), original ? fs.readFileSync(original) : stub);
  // Deterministic Rome daytime in this test isolate only: always exercise real fetch.
  const testNow = new Date(); testNow.setUTCHours(12, 0, 0, 0);
  const calls = []; let redirectGitHub = false;
  const outbound = async request => {
    const url = new URL(request.url);
    assert.equal(url.hostname, "api.github.com"); calls.push({ method: request.method, url: url.href,
      ...(request.method === "POST" ? { body: await request.json() } : {}) });
    if (redirectGitHub) return new Response(null, { status: 302, headers: { location: "https://untrusted.test/never-follow" } });
    return request.method === "POST" ? new Response(null, { status: 204 }) : Response.json({ workflow_runs: [] });
  };
  const opts = { cf: false, workers: [
    { name: "radar", modules: true, script: bundle(path.join(temp, "r6585-radar-entry.js")), compatibilityDate: "2026-09-29",
      serviceBindings: { R6585_BUILDER: { name: "scheduler", entrypoint: "BuilderTrigger" } },
      kvNamespaces: { RADAR_KV: "r6585-radar-test" },
      outboundService: () => new Response("blocked test network", { status: 404 }) },
    { name: "scheduler", modules: true, script: `Date.now = () => ${testNow.getTime()};\n` + bundle(path.join(root, "scheduler/src/index.js")),
      compatibilityDate: "2026-11-04", d1Databases: { DB: "r6585-test-db" },
      bindings: { GITHUB_ACTIONS_TOKEN: "synthetic-test-credential" }, outboundService: outbound },
    { name: "original", modules: true, script: bundle(path.join(temp, "worker.js")), compatibilityDate: "2026-09-29",
      kvNamespaces: { RADAR_KV: "r6585-radar-test" }, outboundService: () => new Response("blocked", { status: 404 }) },
  ] };
  const mf = new Miniflare(convertV4MiniflareOptions(opts));
  const checks = [];
  try {
    const db = await mf.getD1Database("DB", "scheduler");
    for (const sql of fs.readFileSync(path.join(root, "src/funghi_r6/schema.sql"), "utf8").split(";").filter(x => x.trim())) await db.prepare(sql).run();
    const radar = await mf.getWorker("radar"), old = await mf.getWorker("original");
    const kv = await mf.getKVNamespace("RADAR_KV", "radar");
    await kv.put("radar/status-v1", JSON.stringify({ state: "ok", marker: "cached-test" }));
    await kv.put("radar/network-v1", JSON.stringify({ meta: { marker: "cached-test" }, stations: [] }));
    for (const pathname of ["/healthz", "/api/v1/radar/status", "/api/v1/radar/network", "/unknown"]) {
      const before = await old.fetch("https://radar.test" + pathname);
      const after = await radar.fetch("https://radar.test" + pathname);
      assert.equal(after.status, before.status); assert.deepEqual([...after.headers], [...before.headers]);
      assert.equal(await after.text(), await before.text());
    }
    checks.push("original radar cached HTTP contracts unchanged");
    const health = async () => {
      const response = await radar.fetch("https://radar.test/__r6585/builder-health");
      assert.equal(response.status, 200); return await response.json();
    };
    let h = await health(); assert.equal(h.revision, "R6585"); assert.equal(h.token_configured, true);
    assert.equal(h.last_tick, null); assert.equal(calls.length, 0);
    assert.equal((await db.prepare("SELECT COUNT(*) AS n FROM system_state").first()).n, 0);
    checks.push("production entrypoint boots; real named RPC probe is read-only");
    const day = h.target_day, generation = "R6-" + day + "-runtime-test";
    const coverage = Object.fromEntries(["structural", "current", "rain_5_7_15_30", "history_30", "history_90",
      "long_hydro_90", "forecast_7", "forecast_15", "et0", "soil", "scientific_complete"].map(k => [k, 418]));
    const manifest = { architecture: "R6", publication: "ACTIVE", generation, target_day: day,
      station_count: 418, scientifically_complete: true, sha256: "a".repeat(64), snapshot_bytes: 12, coverage,
      d1_verification: { active_generation: generation, readback_verified: true, sha256: "a".repeat(64),
        snapshot_bytes: 12, station_rows: 418, snapshot_chunks: 2 } };
    await db.prepare("INSERT INTO system_state VALUES('ACTIVE',?,datetime('now'))").bind(generation).run();
    await db.prepare("INSERT INTO generation_runs VALUES(?,datetime('now'),?,'ACTIVE',418,418,?)").bind(generation, day, JSON.stringify(manifest)).run();
    await db.prepare("INSERT INTO generation_snapshot VALUES(?,?,12,2,1,datetime('now'))").bind(generation, manifest.sha256).run();
    await db.batch(Array.from({ length: 418 }, (_, i) => db.prepare("INSERT INTO generation_station VALUES(?,?,1)").bind(generation, "TOS" + i)));
    for (let i = 0; i < 2; i++) await db.prepare("INSERT INTO generation_snapshot_chunk VALUES(?,?,?,6)").bind(generation, i, "123456").run();
    const runCron = () => radar.scheduled({ scheduledTime: Date.now(), cron: "*/15 * * * *" });
    assert.equal((await runCron()).outcome, "ok");
    h = await health();
    assert.equal(h.ready, true); assert.equal(h.last_tick.status, "DAILY_ALREADY_READY");
    assert.equal(calls.length, 0);
    if (!original) assert.equal(await kv.get("radar-cron"), "completed");
    checks.push("real radar cron waits for RPC tick; complete daily data triggers no HTTP");
    await db.prepare("DELETE FROM system_state WHERE key='ACTIVE'").run();
    assert.equal((await runCron()).outcome, "ok"); h = await health();
    assert.equal(h.last_tick.status, "DISPATCH_ACCEPTED", JSON.stringify({ h, calls }));
    {
      assert.equal(calls.length, 6);
      assert.deepEqual(calls[5].body.inputs, { mode: "full", target_day: day,
        request_id: h.last_check.request_id, force_rebuild: "false" });
      await runCron(); h = await health(); assert.equal(h.last_tick.status, "LEASE_BUSY"); assert.equal(calls.length, 6);
    }
    checks.push("real RPC missing daily dataset dispatches once; second cron respects D1 lease");
    await db.prepare("DELETE FROM system_state WHERE key='R6584_SCHEDULER'").run();
    redirectGitHub = true;
    await runCron(); h = await health();
    assert.equal(h.last_tick.status, "RETRY_REQUIRED"); assert.equal(h.last_tick.error, "GITHUB_HTTP_302");
    assert.equal(calls.length, 7); // One failed request; no follow to untrusted.test.
    checks.push("real fetch rejects redirect without forwarding the credential");
    assert.equal((await radar.fetch("https://radar.test/__r6585/builder-health", { method: "POST" })).status, original ? 404 : 200);
    console.log(JSON.stringify({ revision: "R6585", runtime: "workerd", clock: "fixed UTC noon in test isolate", radar: original ? "live-audited-source" : "fixture", checks }, null, 2));
  } finally { await mf.dispose(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => fs.rmSync(temp, { recursive: true, force: true }));
