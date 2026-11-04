import assert from "node:assert/strict";
import { test } from "node:test";
import { createRadarBridge } from "./bridge.js";

function context() {
  const promises = [];
  return { promises, waitUntil(p) { promises.push(p); } };
}

test("all existing requests receive the original objects and response", async () => {
  const paths = ["/healthz", "/api/v1/radar/status", "/api/v1/radar/network", "/unknown", "/__r6585/builder-health"];
  for (const path of paths) for (const method of ["GET", "POST", "OPTIONS"]) {
    if (path === "/__r6585/builder-health" && method === "GET") continue;
    const request = new Request("https://radar.test" + path, { method });
    const response = new Response("original", { status: 202 });
    const env = {}, ctx = context();
    const original = { fetch(r, e, c) { assert.equal(this, original); assert.equal(r, request);
      assert.equal(e, env); assert.equal(c, ctx); return response; } };
    assert.equal(await createRadarBridge(original).fetch(request, env, ctx), response);
    assert.equal(ctx.promises.length, 0);
  }
});

test("health probe reads metadata and never ticks the builder", async () => {
  let probes = 0;
  const env = { R6585_BUILDER: { probe: async () => { probes++; return { revision: "R6585" }; },
    tick: () => assert.fail("probe dispatched") } };
  const response = await createRadarBridge({ fetch: () => assert.fail("original called") })
    .fetch(new Request("https://radar.test/__r6585/builder-health"), env, context());
  assert.equal(response.status, 200); assert.equal(probes, 1);
  assert.deepEqual(await response.json(), { revision: "R6585" });
});

test("probe failure returns a fixed error without exception text", async () => {
  const response = await createRadarBridge({}).fetch(new Request("https://radar.test/__r6585/builder-health"),
    { R6585_BUILDER: { probe: () => { throw Error("private-data"); } } }, context());
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { revision: "R6585", error: "BUILDER_PROBE_UNAVAILABLE" });
});

test("cron awaits both jobs and preserves the original controller and context", async () => {
  const controller = {}, ctx = context(); let radarFinished = false, tickFinished = false;
  const env = { R6585_BUILDER: { tick: async () => { tickFinished = true; return { status: "READY" }; } } };
  const original = { scheduled(c, e, x) { assert.equal(this, original); assert.equal(c, controller);
    assert.equal(e, env); assert.equal(x, ctx); x.waitUntil(Promise.resolve().then(() => { radarFinished = true; })); } };
  createRadarBridge(original).scheduled(controller, env, ctx);
  await Promise.all(ctx.promises);
  assert.equal(radarFinished, true); assert.equal(tickFinished, true); assert.equal(ctx.promises.length, 2);
});

for (const kind of ["missing-binding", "sync-throw", "async-reject"]) {
  test("RPC " + kind + " leaves the radar job running", async () => {
    let radar = 0; const ctx = context();
    const env = kind === "missing-binding" ? {} : { R6585_BUILDER: { tick: () => {
      if (kind === "sync-throw") throw Error("private-data");
      return Promise.reject(Error("private-data"));
    } } };
    createRadarBridge({ scheduled(_c, _e, x) { x.waitUntil(Promise.resolve().then(() => { radar++; })); } })
      .scheduled({}, env, ctx);
    await Promise.all(ctx.promises); assert.equal(radar, 1);
  });
}

test("an original synchronous radar failure leaves the RPC registered", async () => {
  let tick = 0; const ctx = context();
  const bridge = createRadarBridge({ scheduled() { throw Error("original-failure"); } });
  assert.throws(() => bridge.scheduled({}, { R6585_BUILDER: { tick: async () => {
    tick++; return { status: "READY" }; } } }, ctx), /original-failure/);
  await Promise.all(ctx.promises); assert.equal(tick, 1);
});

test("an asynchronous radar failure does not cancel the registered RPC", async () => {
  let tick = 0; const ctx = context();
  createRadarBridge({ scheduled(_c, _e, x) { x.waitUntil(Promise.reject(Error("radar-failure"))); } })
    .scheduled({}, { R6585_BUILDER: { tick: async () => { tick++; return { status: "READY" }; } } }, ctx);
  const result = await Promise.allSettled(ctx.promises);
  assert.equal(tick, 1); assert.equal(result[0].status, "fulfilled"); assert.equal(result[1].status, "rejected");
});
