const R6_WORKER_REVISION = "R6.5.0";

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, OPTIONS",
  "access-control-allow-headers": "If-None-Match, Content-Type",
  "access-control-expose-headers": "ETag, Content-Length, X-R6-Generation, X-R6-Snapshot-SHA256, X-R6-Snapshot-Chunks, X-R6-Worker-Revision",
};

const BASE_HEADERS = {
  ...CORS_HEADERS,
  "x-r6-worker-revision": R6_WORKER_REVISION,
};

const json = (body, status = 200, extra = {}, method = "GET") => {
  const text = JSON.stringify(body);
  const headers = {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": String(new TextEncoder().encode(text).byteLength),
    ...BASE_HEADERS,
    ...extra,
  };
  return new Response(method === "HEAD" ? null : text, { status, headers });
};

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

async function snapshotMeta(db, generation) {
  if (!generation) return null;
  const row = await db.prepare(
    "SELECT sha256,byte_length,chunk_count,verified FROM generation_snapshot WHERE generation=?"
  ).bind(generation).first();
  if (!row || Number(row.verified) !== 1) return null;
  const byteLength = Number(row.byte_length);
  const chunkCount = Number(row.chunk_count);
  const sha256 = String(row.sha256 || "");
  if (!Number.isSafeInteger(byteLength) || byteLength < 0) return null;
  if (!Number.isSafeInteger(chunkCount) || chunkCount <= 0) return null;
  if (!/^[a-f0-9]{64}$/i.test(sha256)) return null;
  return {
    sha256,
    byteLength,
    chunkCount,
    verified: true,
  };
}

async function canonicalSnapshot(db, generation, meta = null) {
  if (!generation) return null;
  const validMeta = meta || await snapshotMeta(db, generation);
  if (!validMeta) return null;

  const { results = [] } = await db.prepare(
    "SELECT chunk_index,chunk_text,raw_bytes FROM generation_snapshot_chunk "
      + "WHERE generation=? ORDER BY chunk_index"
  ).bind(generation).all();

  if (results.length !== validMeta.chunkCount) return null;

  const encoder = new TextEncoder();
  const output = new Uint8Array(validMeta.byteLength);
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
    ...validMeta,
  };
}

function snapshotHeaders(meta, generation, cacheControl = "public, max-age=60") {
  return {
    ...BASE_HEADERS,
    "content-type": "application/json; charset=utf-8",
    "content-length": String(meta.byteLength),
    "cache-control": cacheControl,
    etag: `"${meta.sha256}"`,
    "x-r6-generation": String(generation || ""),
    "x-r6-snapshot-sha256": meta.sha256,
    "x-r6-snapshot-chunks": String(meta.chunkCount),
  };
}

function etagMatches(request, sha256) {
  const header = request.headers.get("if-none-match");
  if (!header) return false;
  const target = `"${sha256}"`;
  return header.split(",").map((x) => x.trim()).includes(target) || header.trim() === "*";
}

async function snapshotRoute(request, db, generation) {
  const meta = await snapshotMeta(db, generation);
  if (!meta) {
    return json({ error: "R6 ACTIVE canonico non disponibile" }, 503, {}, request.method);
  }

  // IMPORTANTE: ETag e HEAD vengono risolti usando solo i metadati.
  // In questi casi non leggiamo i ~5 MB di chunk dal D1.
  if (etagMatches(request, meta.sha256)) {
    const headers = snapshotHeaders(meta, generation);
    delete headers["content-length"];
    return new Response(null, { status: 304, headers });
  }

  if (request.method === "HEAD") {
    return new Response(null, { status: 200, headers: snapshotHeaders(meta, generation) });
  }

  const snapshot = await canonicalSnapshot(db, generation, meta);
  if (!snapshot) {
    return json({ error: "R6 ACTIVE canonico non disponibile" }, 503, {}, request.method);
  }

  return new Response(snapshot.bytes, {
    status: 200,
    headers: snapshotHeaders(snapshot, generation),
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

function methodNotAllowed(method) {
  return json(
    { error: `Method ${method} not allowed`, architecture: "R6" },
    405,
    { allow: "GET, HEAD, OPTIONS" },
    method,
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          ...BASE_HEADERS,
          "cache-control": "public, max-age=86400",
          allow: "GET, HEAD, OPTIONS",
        },
      });
    }

    if (!env?.DB) {
      return json({ error: "R6 D1 binding DB non configurato", architecture: "R6" }, 500, {}, request.method);
    }

    if (!(["GET", "HEAD"].includes(request.method))) {
      return methodNotAllowed(request.method);
    }

    try {
      if (path === "/health") {
        const active = await activeGeneration(env.DB);
        const previous = await previousGeneration(env.DB);
        const snap = await snapshotMeta(env.DB, active);
        const manifest = await generationManifest(env.DB, active);

        return json({
          ok: Boolean(
            active
            && snap
            && manifest
            && manifest.scientifically_complete === true
            && Number(manifest.station_count) === 418
          ),
          architecture: "R6",
          worker_revision: R6_WORKER_REVISION,
          active,
          previous,
          canonical_snapshot: snap ? {
            sha256: snap.sha256,
            bytes: snap.byteLength,
            chunks: snap.chunkCount,
            verified: true,
          } : null,
          station_count: Number(manifest?.station_count || 0),
          scientifically_complete: manifest?.scientifically_complete === true,
        }, 200, {}, request.method);
      }

      if (path === "/api/v6/manifest") {
        const active = await activeGeneration(env.DB);
        const manifest = await generationManifest(env.DB, active);
        return manifest
          ? json({ data: manifest }, 200, {}, request.method)
          : json({ error: "R6 ACTIVE non ancora disponibile" }, 503, {}, request.method);
      }

      if (path === "/api/v6/snapshot") {
        const active = await activeGeneration(env.DB);
        return snapshotRoute(request, env.DB, active);
      }

      if (path === "/api/v6/source-health") {
        return json({ data: await sourceHealth(env.DB) }, 200, {}, request.method);
      }

      // Ponte di compatibilita' per l'APK esistente. Non spostare l'APK su R6
      // finche' il Worker R6 non e' stato collaudato separatamente.
      if (path === "/api/v1/stations") {
        const active = await activeGeneration(env.DB);
        return snapshotRoute(request, env.DB, active);
      }

      if (path === "/api/v1/cache-status") {
        const active = await activeGeneration(env.DB);
        const previous = await previousGeneration(env.DB);
        const manifest = await generationManifest(env.DB, active);
        const snap = await snapshotMeta(env.DB, active);
        if (!manifest) {
          return json({
            data: {
              worker_checked: true,
              worker_ok: false,
              architecture: "R6",
              r6_active: active,
              r6_previous: previous,
              worker_revision: R6_WORKER_REVISION,
            },
          }, 503, {}, request.method);
        }

        return json({
          data: {
            worker_checked: true,
            worker_ok: Boolean(snap && manifest.scientifically_complete === true),
            architecture: "R6",
            worker_revision: R6_WORKER_REVISION,
            r6_active: active,
            r6_previous: previous,
            snapshot_station_count: manifest.station_count,
            snapshot_sha256: manifest.sha256,
            snapshot_bytes: manifest.snapshot_bytes,
            snapshot_chunks: snap?.chunkCount || 0,
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
        }, 200, {}, request.method);
      }

      return json({ error: "Not found", architecture: "R6" }, 404, {}, request.method);
    } catch (error) {
      return json({
        error: String(error?.message || error),
        architecture: "R6",
        worker_revision: R6_WORKER_REVISION,
      }, 500, {}, request.method);
    }
  },
};
