const json = (body, status = 200, extra = {}) => new Response(JSON.stringify(body), {
  status,
  headers: {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    ...extra,
  },
});

async function activeGeneration(db) {
  const row = await db.prepare("SELECT value FROM system_state WHERE key='ACTIVE'").first();
  return row?.value || null;
}

async function previousGeneration(db) {
  const row = await db.prepare("SELECT value FROM system_state WHERE key='ACTIVE_PREVIOUS'").first();
  return row?.value || null;
}

async function generationManifest(db, generation) {
  if (!generation) return null;
  const row = await db.prepare(
    "SELECT manifest_json FROM generation_runs WHERE generation=? AND status='ACTIVE'"
  ).bind(generation).first();
  if (!row?.manifest_json) return null;
  try {
    return JSON.parse(row.manifest_json);
  } catch {
    return null;
  }
}

async function canonicalSnapshot(db, generation) {
  if (!generation) return null;

  const meta = await db.prepare(
    "SELECT sha256,byte_length,chunk_count,verified FROM generation_snapshot WHERE generation=?"
  ).bind(generation).first();

  if (!meta || Number(meta.verified) !== 1) return null;

  const { results = [] } = await db.prepare(
    "SELECT chunk_index,chunk_text,raw_bytes FROM generation_snapshot_chunk "
      + "WHERE generation=? ORDER BY chunk_index"
  ).bind(generation).all();

  if (results.length !== Number(meta.chunk_count)) return null;

  const encoder = new TextEncoder();
  const output = new Uint8Array(Number(meta.byte_length));
  let offset = 0;

  for (let i = 0; i < results.length; i += 1) {
    const row = results[i];
    if (Number(row.chunk_index) !== i) return null;
    const part = encoder.encode(String(row.chunk_text ?? ""));
    if (part.byteLength !== Number(row.raw_bytes)) return null;
    if (offset + part.byteLength > output.byteLength) return null;
    output.set(part, offset);
    offset += part.byteLength;
  }

  if (offset !== output.byteLength) return null;

  return {
    bytes: output,
    sha256: String(meta.sha256),
    byteLength: Number(meta.byte_length),
    chunkCount: Number(meta.chunk_count),
  };
}

function snapshotResponse(snapshot, request, cacheControl = "public, max-age=60") {
  const etag = `"${snapshot.sha256}"`;
  if (request.headers.get("if-none-match") === etag) {
    return new Response(null, {
      status: 304,
      headers: {
        etag,
        "cache-control": cacheControl,
      },
    });
  }

  return new Response(snapshot.bytes, {
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "content-length": String(snapshot.byteLength),
      "cache-control": cacheControl,
      etag,
      "x-r6-snapshot-sha256": snapshot.sha256,
      "x-r6-snapshot-chunks": String(snapshot.chunkCount),
    },
  });
}

async function sourceHealth(db) {
  const { results = [] } = await db.prepare(
    "SELECT source,reachable,acquired,last_success_at,last_error,reference_at,details_json,updated_at "
      + "FROM source_health ORDER BY source"
  ).all();
  return results.map((r) => {
    let details = null;
    try {
      details = r.details_json ? JSON.parse(r.details_json) : null;
    } catch {}
    return { ...r, reachable: Boolean(r.reachable), details };
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    try {
      if (request.method === "GET" && path === "/health") {
        const active = await activeGeneration(env.DB);
        const previous = await previousGeneration(env.DB);
        const snap = active
          ? await env.DB.prepare(
            "SELECT sha256,byte_length,chunk_count,verified FROM generation_snapshot WHERE generation=?"
          ).bind(active).first()
          : null;

        return json({
          ok: Boolean(active && snap && Number(snap.verified) === 1),
          architecture: "R6",
          active,
          previous,
          canonical_snapshot: snap ? {
            sha256: snap.sha256,
            bytes: Number(snap.byte_length),
            chunks: Number(snap.chunk_count),
            verified: Boolean(snap.verified),
          } : null,
        });
      }

      if (request.method === "GET" && path === "/api/v6/manifest") {
        const active = await activeGeneration(env.DB);
        const manifest = await generationManifest(env.DB, active);
        return manifest
          ? json({ data: manifest })
          : json({ error: "R6 ACTIVE non ancora disponibile" }, 503);
      }

      if (request.method === "GET" && path === "/api/v6/snapshot") {
        const active = await activeGeneration(env.DB);
        const snapshot = await canonicalSnapshot(env.DB, active);
        return snapshot
          ? snapshotResponse(snapshot, request)
          : json({ error: "R6 ACTIVE canonico non disponibile" }, 503);
      }

      if (request.method === "GET" && path === "/api/v6/source-health") {
        return json({ data: await sourceHealth(env.DB) });
      }

      // Ponte di compatibilita' per l'APK esistente. Non spostare l'APK su R6
      // finche' il Worker R6 non e' stato collaudato separatamente.
      if (request.method === "GET" && path === "/api/v1/stations") {
        const active = await activeGeneration(env.DB);
        const snapshot = await canonicalSnapshot(env.DB, active);
        return snapshot
          ? snapshotResponse(snapshot, request)
          : json({ error: "R6 warming" }, 503);
      }

      if (request.method === "GET" && path === "/api/v1/cache-status") {
        const active = await activeGeneration(env.DB);
        const previous = await previousGeneration(env.DB);
        const manifest = await generationManifest(env.DB, active);
        if (!manifest) {
          return json({
            data: {
              worker_checked: true,
              worker_ok: true,
              r6_active: null,
              r6_previous: previous,
            },
          });
        }

        return json({
          data: {
            worker_checked: true,
            worker_ok: true,
            architecture: "R6",
            r6_active: active,
            r6_previous: previous,
            snapshot_station_count: manifest.station_count,
            snapshot_sha256: manifest.sha256,
            snapshot_bytes: manifest.snapshot_bytes,
            metrics: {
              total: manifest.coverage?.total || 0,
              fully_complete: manifest.coverage?.scientific_complete || 0,
              current: manifest.coverage?.current || 0,
              history: manifest.coverage?.history_30 || 0,
              forecast_7d: manifest.coverage?.forecast_7 || 0,
              forecast_15d: manifest.coverage?.forecast_15 || 0,
              soil: manifest.coverage?.soil || 0,
            },
            source_health: await sourceHealth(env.DB),
          },
        });
      }

      return json({ error: "Not found" }, 404);
    } catch (error) {
      return json({
        error: String(error?.message || error),
        architecture: "R6",
      }, 500);
    }
  },
};
