import assert from "node:assert/strict";
import { test } from "node:test";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import worker from "./src/handler.js";
import { check, completeManifest, dailyActive, romeClock, STATE_KEY, REPOSITORY } from "./src/core.js";
import { runTick, probe, TICK_KEY } from "./src/runtime.js";

const DAY = "2026-11-04";
const NOW = Date.parse(DAY + "T00:15:00Z"); // 01:15 Rome in winter.
const secret = "unit-test-secret-never-logged";

test("Node-testable handler exports only the default handler and JSON helper", async () => {
  const entry = await import("./src/handler.js");
  assert.deepEqual(Object.keys(entry), ["default", "json"]);
  assert.equal(typeof entry.default.scheduled, "function");
  assert.equal(typeof entry.default.fetch, "function");
});

function database() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(readFileSync(new URL("../src/funghi_r6/schema.sql", import.meta.url), "utf8"));
  return { sql, prepare(query) {
    const stmt = sql.prepare(query);
    const bound = params => ({
      first: async () => stmt.get(...params) || null,
      all: async () => ({ results: stmt.all(...params) }),
      run: async () => stmt.run(...params),
    });
    return { ...bound([]), bind: (...params) => bound(params) };
  }};
}
function environment() { return { DB: database(), GITHUB_ACTIONS_TOKEN: secret }; }
function seed(db, day = DAY) {
  const generation = "R6-" + day + "-test";
  const coverage = Object.fromEntries(["structural", "current", "rain_5_7_15_30", "history_30", "history_90",
    "long_hydro_90", "forecast_7", "forecast_15", "et0", "soil", "scientific_complete"].map(k => [k, 418]));
  const manifest = { architecture: "R6", publication: "ACTIVE", generation, target_day: day,
    station_count: 418, scientifically_complete: true, sha256: "a".repeat(64), snapshot_bytes: 12, coverage,
    d1_verification: { active_generation: generation, readback_verified: true, sha256: "a".repeat(64),
      snapshot_bytes: 12, station_rows: 418, snapshot_chunks: 2 }};
  db.sql.prepare("INSERT OR REPLACE INTO system_state VALUES('ACTIVE',?,datetime('now'))").run(generation);
  db.sql.prepare("INSERT INTO generation_runs VALUES(?,datetime('now'),?,'ACTIVE',418,418,?)")
    .run(generation, day, JSON.stringify(manifest));
  db.sql.prepare("INSERT INTO generation_snapshot VALUES(?,?,12,2,1,datetime('now'))")
    .run(generation, manifest.sha256);
  for (let i = 0; i < 418; i++) db.sql.prepare("INSERT INTO generation_station VALUES(?,?,1)").run(generation, "TOS" + i);
  for (let i = 0; i < 2; i++) db.sql.prepare("INSERT INTO generation_snapshot_chunk VALUES(?,?,?,6)").run(generation, i, "123456");
  return manifest;
}
function mockGitHub({ pending, failure, dispatchStatus = 204, dispatchBody = {}, failDispatch = false, onGet } = {}) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, ...init });
    assert.ok(url.startsWith("https://api.github.com/repos/" + REPOSITORY + "/"));
    assert.equal(init.redirect, "manual");
    assert.equal(init.headers.authorization, "Bearer " + secret);
    if (failure) return new Response("do not log response with " + secret, { status: failure });
    if (init.method === "POST") {
      if (failDispatch) throw new Error(secret);
      return new Response(dispatchStatus === 204 ? null : JSON.stringify(dispatchBody), { status: dispatchStatus });
    }
    if (onGet) await onGet(calls);
    const status = new URL(url).searchParams.get("status");
    return Response.json({ workflow_runs: pending === status ? [{ id: 42, status }] : [] });
  };
  return { calls, fn };
}
const options = (mock, extra = {}) => ({ now: NOW, requestId: "request-a", fetcher: mock.fn, ...extra });

test("RPC receipt is one bounded row; complete daily data makes no HTTP call", async () => {
  const env = environment(); seed(env.DB);
  const http = mockGitHub();
  const receipt = await runTick(env, options(http));
  assert.equal(receipt.status, "DAILY_ALREADY_READY");
  assert.equal(receipt.source, "radar-cron-rpc");
  await runTick(env, options(http, { now: NOW + 1 }));
  await runTick(env, options(http)); // Older invocation must not replace newer receipt.
  const rows = env.DB.sql.prepare("SELECT value FROM system_state WHERE key=?").all(TICK_KEY);
  assert.equal(rows.length, 1); assert.equal(JSON.parse(rows[0].value).started_ms, NOW + 1);
  assert.equal(http.calls.length, 0);
});

test("RPC probe is strictly read-only and exposes no token", async () => {
  const env = environment();
  const before = env.DB.sql.prepare("SELECT * FROM system_state").all();
  const result = await probe(env);
  assert.equal(result.revision, "R6585"); assert.equal(result.last_tick, null);
  assert.equal(result.token_configured, true);
  assert.equal(JSON.stringify(result).includes(secret), false);
  assert.deepEqual(env.DB.sql.prepare("SELECT * FROM system_state").all(), before);
});

test("RPC tick retains the dispatch and lease contract", async () => {
  const env = environment(), http = mockGitHub();
  assert.equal((await runTick(env, options(http))).status, "DISPATCH_ACCEPTED");
  assert.equal((await runTick(env, options(http))).status, "LEASE_BUSY");
  assert.equal(http.calls.filter(c => c.method === "POST").length, 1);
});

test("Rome day/window follows midnight and both DST changes", () => {
  assert.deepEqual(romeClock(Date.parse("2026-11-03T23:00:00Z")), { day: DAY, minute: 0 });
  assert.deepEqual(romeClock(NOW), { day: DAY, minute: 75 });
  assert.deepEqual(romeClock(Date.parse("2026-07-01T23:15:00Z")), { day: "2026-07-02", minute: 75 });
  assert.equal(romeClock(Date.parse("2026-03-29T00:59:00Z")).minute, 119);
  assert.equal(romeClock(Date.parse("2026-03-29T01:00:00Z")).minute, 180);
  assert.equal(romeClock(Date.parse("2026-10-25T00:59:00Z")).minute, 179);
  assert.equal(romeClock(Date.parse("2026-10-25T01:00:00Z")).minute, 120);
});

test("before 01:15 there is no D1 or GitHub work", async () => {
  const result = await check({}, { now: NOW - 1, fetcher: () => assert.fail("HTTP before window") });
  assert.equal(result.status, "WAIT_WINDOW");
});

test("complete ACTIVE stops all GitHub calls and leaves scientific data unchanged", async () => {
  const env = environment();
  const manifest = seed(env.DB);
  const mock = mockGitHub();
  const before = env.DB.sql.prepare("SELECT * FROM generation_runs").all();
  assert.equal((await check(env, options(mock))).status, "DAILY_ALREADY_READY");
  assert.equal(mock.calls.length, 0);
  assert.deepEqual(env.DB.sql.prepare("SELECT * FROM generation_runs").all(), before);
  assert.equal(await dailyActive(env.DB, "2026-11-05"), null);
  assert.equal(completeManifest(manifest, DAY), true);
});

for (const damage of [
  "UPDATE generation_snapshot SET verified=0",
  "UPDATE generation_snapshot SET byte_length=13",
  "DELETE FROM generation_snapshot_chunk WHERE chunk_index=0",
  "UPDATE generation_snapshot_chunk SET chunk_index=chunk_index+10",
  "UPDATE generation_station SET complete=0 WHERE code='TOS1'",
  "UPDATE generation_runs SET manifest_json='invalid'",
  "UPDATE generation_runs SET status='STAGING'",
  "UPDATE generation_runs SET manifest_json=json_set(manifest_json,'$.coverage.forecast_15',417)",
  "UPDATE generation_runs SET manifest_json=json_set(manifest_json,'$.d1_verification.readback_verified',0)",
]) test("damaged metadata/structure does not falsely stop retries: " + damage, async () => {
  const env = environment();
  seed(env.DB);
  env.DB.sql.exec(damage);
  assert.equal(await dailyActive(env.DB, DAY), null);
});

test("missing today dispatches full with fixed repo/main, exact Rome day and force=false", async () => {
  const env = environment();
  seed(env.DB, "2026-11-03");
  const mock = mockGitHub();
  const result = await check(env, options(mock));
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(mock.calls.length, 6);
  const post = mock.calls.at(-1);
  assert.deepEqual(JSON.parse(post.body), { ref: "main", inputs: {
    mode: "full", target_day: DAY, request_id: "request-a", force_rebuild: "false",
  }});
  assert.equal(env.DB.sql.prepare("SELECT value FROM system_state WHERE key='ACTIVE'").get().value, "R6-2026-11-03-test");
  assert.equal((await check(env, options(mock, { requestId: "request-b", now: NOW + 600000 }))).status, "LEASE_BUSY");
  assert.equal(mock.calls.length, 6);
});

for (const pending of ["in_progress", "queued", "waiting", "pending", "requested"]) {
  test(pending + " run prevents dispatch without cancelling or changing that run", async () => {
    const mock = mockGitHub({ pending });
    const state = await check(environment(), options(mock));
    assert.equal(state.status, "RUN_PENDING");
    assert.equal(state.run_id, 42);
    assert.equal(mock.calls.filter(c => c.method === "POST").length, 0);
  });
}

test("simultaneous isolates acquire one lease and dispatch once (real SQLite CAS)", async () => {
  const env = environment(), mock = mockGitHub();
  const states = await Promise.all(Array.from({ length: 12 }, (_, i) =>
    check(env, options(mock, { requestId: "concurrent-" + i }))));
  assert.equal(states.filter(s => s.status === "DISPATCH_ACCEPTED").length, 1);
  assert.equal(states.filter(s => s.status === "LEASE_BUSY").length, 11);
  assert.equal(mock.calls.filter(c => c.method === "POST").length, 1);
});

for (const failure of [301, 302, 307, 308, 401, 403, 429, 500]) test("HTTP " + failure + " is retryable and never leaks secret/body", async () => {
  const env = environment(), mock = mockGitHub({ failure });
  const first = await check(env, options(mock));
  assert.equal(first.status, "RETRY_REQUIRED");
  assert.equal(first.error, "GITHUB_HTTP_" + failure);
  assert.ok(!JSON.stringify(first).includes(secret));
  const recovered = mockGitHub();
  const next = await check(env, options(recovered, { now: NOW + 20 * 60000, requestId: "retry" }));
  assert.equal(next.status, "DISPATCH_ACCEPTED");
});

test("unknown dispatch outcome keeps lease and later checks for the queued run", async () => {
  const env = environment(), timeout = mockGitHub({ failDispatch: true });
  const first = await check(env, options(timeout));
  assert.equal(first.status, "RETRY_REQUIRED");
  assert.equal(first.error, "NETWORK_OR_RUNTIME_ERROR");
  assert.ok(!JSON.stringify(first).includes(secret));
  const pending = mockGitHub({ pending: "queued" });
  const next = await check(env, options(pending, { now: NOW + 20 * 60000, requestId: "retry" }));
  assert.equal(next.status, "RUN_PENDING");
  assert.equal(pending.calls.filter(c => c.method === "POST").length, 0);
});

test("a successful ACTIVE appearing during the GitHub checks prevents dispatch", async () => {
  const env = environment();
  const mock = mockGitHub({ onGet(calls) { if (calls.length === 1) seed(env.DB); } });
  assert.equal((await check(env, options(mock))).status, "DAILY_ALREADY_READY");
  assert.equal(mock.calls.filter(c => c.method === "POST").length, 0);
});

test("an invalid HTTP 200 response is not mislabeled as accepted/completed", async () => {
  const env = environment(), mock = mockGitHub({ dispatchStatus: 200 });
  const state = await check(env, options(mock));
  assert.equal(state.status, "RETRY_REQUIRED");
  assert.equal(state.error, "UNEXPECTED_DISPATCH_RESPONSE");
  assert.equal(await dailyActive(env.DB, DAY), null);
});

test("GitHub API 2026-03-10 returns a run id with HTTP 200; acceptance is still not completion", async () => {
  const env = environment(), mock = mockGitHub({ dispatchStatus: 200, dispatchBody: { workflow_run_id: 12345 } });
  const state = await check(env, options(mock));
  assert.equal(state.status, "DISPATCH_ACCEPTED");
  assert.equal(state.run_id, 12345);
  assert.equal(mock.calls.at(-1).headers["x-github-api-version"], "2026-03-10");
  assert.equal(await dailyActive(env.DB, DAY), null);
});

test("missing credential is visible and never makes an anonymous request", async () => {
  const env = environment(), mock = mockGitHub();
  delete env.GITHUB_ACTIONS_TOKEN;
  const state = await check(env, options(mock));
  assert.equal(state.error, "MISSING_GITHUB_ACTIONS_TOKEN");
  assert.equal(mock.calls.length, 0);
});

test("next day is checked independently, even after yesterday completed", async () => {
  const env = environment(), mock = mockGitHub();
  seed(env.DB);
  assert.equal((await check(env, options(mock))).status, "DAILY_ALREADY_READY");
  const next = await check(env, options(mock, { now: NOW + 86400000 }));
  assert.equal(next.status, "DISPATCH_ACCEPTED");
  assert.equal(next.target_day, "2026-11-05");
});

test("public health cannot dispatch; it exposes no credentials and reports D1 failures", async () => {
  const env = environment();
  const res = await worker.fetch(new Request("https://scheduler.test/health"), env);
  assert.equal(res.status, 200);
  assert.ok(!(await res.text()).includes(secret));
  for (const req of [new Request("https://scheduler.test/check", { method: "POST" }),
    new Request("https://scheduler.test/health", { method: "POST" })]) {
    assert.equal((await worker.fetch(req, env)).status, 404);
  }
  assert.equal((await worker.fetch(new Request("https://scheduler.test/health"), {})).status, 503);
  assert.equal(env.DB.sql.prepare("SELECT * FROM system_state WHERE key=?").get(STATE_KEY), undefined);
});
