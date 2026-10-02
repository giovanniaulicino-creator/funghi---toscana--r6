import assert from "node:assert/strict";
import worker from "./src/index.js";

const active = "R6-2026-10-02-110110Z";
const previous = "R6-2026-10-02-105251Z";
const snapshotText = JSON.stringify({
  meta: {
    generation: active,
    target_day: "2026-10-02",
    scientifically_complete: true,
  },
  data: [{ code: "TOS1" }, { code: "TOS2" }],
});
const enc = new TextEncoder();
const bytes = enc.encode(snapshotText);
const cut = Math.floor(snapshotText.length / 2);
const chunkTexts = [snapshotText.slice(0, cut), snapshotText.slice(cut)];
const sha = "41dc54b842ee622da4f875ad60ae55df574bfd79ef52f896e81de812442f1683";
const manifest = {
  architecture: "R6",
  generation: active,
  target_day: "2026-10-02",
  station_count: 418,
  coverage: {
    total: 418,
    current: 418,
    history_30: 418,
    forecast_7: 418,
    forecast_15: 418,
    soil: 418,
    scientific_complete: 418,
  },
  scientifically_complete: true,
  sha256: sha,
  snapshot_bytes: bytes.byteLength,
  publication: "ACTIVE",
};

class MockStatement {
  constructor(db, sql) {
    this.db = db;
    this.sql = sql;
    this.params = [];
  }
  bind(...params) {
    this.params = params;
    return this;
  }
  async first() {
    this.db.queries.push({ type: "first", sql: this.sql, params: this.params });
    if (this.sql.includes("system_state") && this.sql.includes("key='ACTIVE_PREVIOUS'")) {
      return { value: previous };
    }
    if (this.sql.includes("system_state") && this.sql.includes("key='ACTIVE'")) {
      return { value: active };
    }
    if (this.sql.includes("generation_snapshot")) {
      return {
        sha256: sha,
        byte_length: bytes.byteLength,
        chunk_count: chunkTexts.length,
        verified: 1,
      };
    }
    if (this.sql.includes("generation_runs")) {
      return { manifest_json: JSON.stringify(manifest) };
    }
    throw new Error(`Mock first SQL non gestito: ${this.sql}`);
  }
  async all() {
    this.db.queries.push({ type: "all", sql: this.sql, params: this.params });
    if (this.sql.includes("generation_snapshot_chunk")) {
      this.db.chunkReads += 1;
      return {
        results: chunkTexts.map((text, i) => ({
          chunk_index: i,
          chunk_text: text,
          raw_bytes: enc.encode(text).byteLength,
        })),
      };
    }
    if (this.sql.includes("FROM source_health")) {
      return {
        results: [{
          source: "SIR/CFR rainfall",
          reachable: 1,
          acquired: 377,
          last_success_at: "2026-10-02T10:55:33Z",
          last_error: null,
          reference_at: "02/10/2026 11:45",
          details_json: JSON.stringify({ reachable: true }),
          updated_at: "2026-10-02T10:55:33Z",
        }],
      };
    }
    throw new Error(`Mock all SQL non gestito: ${this.sql}`);
  }
}

class MockDB {
  constructor() {
    this.queries = [];
    this.chunkReads = 0;
  }
  prepare(sql) {
    return new MockStatement(this, sql);
  }
}

async function call(path, { method = "GET", headers = {} } = {}) {
  const db = new MockDB();
  const response = await worker.fetch(
    new Request(`https://r6.test${path}`, { method, headers }),
    { DB: db },
  );
  return { response, db };
}

{
  const { response } = await call("/health");
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.ok, true);
  assert.equal(body.active, active);
  assert.equal(body.previous, previous);
  assert.equal(body.station_count, 418);
  assert.equal(body.worker_revision, "R6.5.0");
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
}

{
  const { response, db } = await call("/api/v6/snapshot");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("etag"), `"${sha}"`);
  assert.equal(response.headers.get("x-r6-generation"), active);
  assert.equal(response.headers.get("x-r6-snapshot-chunks"), "2");
  assert.equal(db.chunkReads, 1);
  const raw = new Uint8Array(await response.arrayBuffer());
  assert.deepEqual(raw, bytes);
}

{
  const { response, db } = await call("/api/v6/snapshot", {
    headers: { "If-None-Match": `"${sha}"` },
  });
  assert.equal(response.status, 304);
  assert.equal(db.chunkReads, 0, "ETag 304 non deve leggere i chunk D1");
}

{
  const { response, db } = await call("/api/v6/snapshot", { method: "HEAD" });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-length"), String(bytes.byteLength));
  assert.equal(db.chunkReads, 0, "HEAD non deve leggere i chunk D1");
  assert.equal((await response.arrayBuffer()).byteLength, 0);
}

{
  const { response } = await call("/api/v6/manifest");
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.data.generation, active);
  assert.equal(body.data.publication, "ACTIVE");
}

{
  const { response } = await call("/api/v6/source-health");
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.data[0].reachable, true);
  assert.equal(body.data[0].acquired, 377);
}

{
  const { response } = await call("/api/v6/snapshot", { method: "OPTIONS" });
  assert.equal(response.status, 204);
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
}

{
  const { response } = await call("/api/v6/snapshot", { method: "POST" });
  assert.equal(response.status, 405);
  assert.equal(response.headers.get("allow"), "GET, HEAD, OPTIONS");
}

console.log("[OK] R6 Worker tests: health, snapshot byte-exact, ETag 304, HEAD, CORS, manifest, source-health, 405");
