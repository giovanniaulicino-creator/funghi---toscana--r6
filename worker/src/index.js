const json = (body, status = 200, extra = {}) => new Response(JSON.stringify(body), {
  status,
  headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra },
});

async function activeGeneration(db) {
  const row = await db.prepare("SELECT value FROM system_state WHERE key='ACTIVE'").first();
  return row?.value || null;
}

async function generationManifest(db, generation) {
  if (!generation) return null;
  const row = await db.prepare("SELECT manifest_json FROM generation_runs WHERE generation=? AND status='ACTIVE'").bind(generation).first();
  if (!row?.manifest_json) return null;
  try { return JSON.parse(row.manifest_json); } catch { return null; }
}

async function snapshot(db, generation) {
  if (!generation) return null;
  const row = await db.prepare(`
    SELECT
      (SELECT manifest_json FROM generation_runs WHERE generation=?) AS manifest_json,
      json_group_array(json(payload_json)) AS data_json
    FROM generation_station
    WHERE generation=? AND complete=1
  `).bind(generation, generation).first();
  if (!row?.manifest_json || !row?.data_json) return null;
  const manifest = JSON.parse(row.manifest_json);
  const data = JSON.parse(row.data_json);
  return {
    meta: {
      architecture: "r6-external-builder-d1-atomic",
      generation,
      generated_at: manifest.generated_at,
      target_day: manifest.target_day,
      scientifically_complete: manifest.scientifically_complete,
      coverage: manifest.coverage,
      source_status: manifest.source_status,
      model_version: "4.9.0-data-completeness-r6",
    },
    data,
  };
}

async function sourceHealth(db) {
  const { results = [] } = await db.prepare("SELECT source,reachable,acquired,last_success_at,last_error,reference_at,details_json,updated_at FROM source_health ORDER BY source").all();
  return results.map((r) => {
    let details = null;
    try { details = r.details_json ? JSON.parse(r.details_json) : null; } catch {}
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
        return json({ ok: true, architecture: "R6", active });
      }

      if (request.method === "GET" && path === "/api/v6/manifest") {
        const active = await activeGeneration(env.DB);
        const manifest = await generationManifest(env.DB, active);
        return manifest ? json({ data: manifest }) : json({ error: "R6 ACTIVE non ancora disponibile" }, 503);
      }

      if (request.method === "GET" && path === "/api/v6/snapshot") {
        const active = await activeGeneration(env.DB);
        const payload = await snapshot(env.DB, active);
        return payload ? json(payload, 200, { "cache-control": "public, max-age=60" }) : json({ error: "R6 ACTIVE non ancora disponibile" }, 503);
      }

      if (request.method === "GET" && path === "/api/v6/source-health") {
        return json({ data: await sourceHealth(env.DB) });
      }

      // Compatibility bridge for the existing APK. Do not switch the APK to this Worker
      // until the R6 audit has produced a real 418/418 scientific ACTIVE generation.
      if (request.method === "GET" && path === "/api/v1/stations") {
        const active = await activeGeneration(env.DB);
        const payload = await snapshot(env.DB, active);
        return payload ? json(payload, 200, { "cache-control": "public, max-age=60" }) : json({ error: "R6 warming" }, 503);
      }

      if (request.method === "GET" && path === "/api/v1/cache-status") {
        const active = await activeGeneration(env.DB);
        const manifest = await generationManifest(env.DB, active);
        if (!manifest) return json({ data: { worker_checked: true, worker_ok: true, r6_active: null } });
        return json({ data: {
          worker_checked: true,
          worker_ok: true,
          architecture: "R6",
          r6_active: active,
          snapshot_station_count: manifest.station_count,
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
        }});
      }

      return json({ error: "Not found" }, 404);
    } catch (error) {
      return json({ error: String(error?.message || error), architecture: "R6" }, 500);
    }
  },
};
