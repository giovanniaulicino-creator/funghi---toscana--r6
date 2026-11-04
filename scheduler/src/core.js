// R6584: orchestration only. No weather/radar data or scientific calculations.
export const REPOSITORY = "giovanniaulicino-creator/funghi---toscana--r6";
export const WORKFLOW = "r6-builder.yml";
export const STATE_KEY = "R6584_SCHEDULER";
const LEASE_MS = 15 * 60 * 1000;
const API = `https://api.github.com/repos/${REPOSITORY}/actions/workflows/${WORKFLOW}`;
const COVERAGE = ["structural", "current", "rain_5_7_15_30", "history_30", "history_90",
  "long_hydro_90", "forecast_7", "forecast_15", "et0", "soil", "scientific_complete"];
const formatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Rome", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export function romeClock(now) {
  const p = Object.fromEntries(formatter.formatToParts(new Date(now)).map(x => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, minute: Number(p.hour) * 60 + Number(p.minute) };
}

export function completeManifest(m, day) {
  const v = m?.d1_verification;
  return m?.architecture === "R6" && m.publication === "ACTIVE" && m.target_day === day
    && m.scientifically_complete === true && m.station_count === 418
    && COVERAGE.every(k => m.coverage?.[k] === 418)
    && typeof m.generation === "string" && m.generation.length > 0
    && /^[a-f0-9]{64}$/.test(m.sha256) && Number.isSafeInteger(m.snapshot_bytes) && m.snapshot_bytes > 0
    && v?.readback_verified === true && v.active_generation === m.generation
    && v.sha256 === m.sha256 && v.snapshot_bytes === m.snapshot_bytes && v.station_rows === 418
    && Number.isSafeInteger(v.snapshot_chunks) && v.snapshot_chunks > 0;
}

export async function dailyActive(db, day) {
  const r = await db.prepare(
    "SELECT r.manifest_json,s.sha256,s.byte_length,s.chunk_count,"
    + "(SELECT COUNT(*) FROM generation_station WHERE generation=r.generation AND complete=1) AS station_rows,"
    + "(SELECT COUNT(*) FROM generation_snapshot_chunk WHERE generation=r.generation) AS chunks,"
    + "(SELECT SUM(raw_bytes) FROM generation_snapshot_chunk WHERE generation=r.generation) AS chunk_bytes,"
    + "(SELECT MIN(chunk_index) FROM generation_snapshot_chunk WHERE generation=r.generation) AS first_chunk,"
    + "(SELECT MAX(chunk_index) FROM generation_snapshot_chunk WHERE generation=r.generation) AS last_chunk "
    + "FROM system_state a JOIN generation_runs r ON r.generation=a.value "
    + "JOIN generation_snapshot s ON s.generation=r.generation "
    + "WHERE a.key='ACTIVE' AND r.status='ACTIVE' AND r.target_day=? "
    + "AND r.structural_total=418 AND r.scientific_total=418 AND s.verified=1"
  ).bind(day).first();
  if (!r) return null;
  let m;
  try { m = JSON.parse(r.manifest_json); } catch { return null; }
  if (!completeManifest(m, day) || r.sha256 !== m.sha256 || r.byte_length !== m.snapshot_bytes
    || r.chunk_count !== m.d1_verification.snapshot_chunks || r.station_rows !== 418
    || r.chunks !== r.chunk_count || r.chunk_bytes !== r.byte_length
    || r.first_chunk !== 0 || r.last_chunk !== r.chunk_count - 1) return null;
  return m;
}

async function acquire(db, state, now) {
  // One SQLite statement is the cross-isolate compare-and-set; no in-memory lock.
  const row = await db.prepare(
    "INSERT INTO system_state(key,value,updated_at) VALUES(?,?,datetime('now')) "
    + "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
    + "WHERE COALESCE(json_extract(system_state.value,'$.lease_until'),0)<=? RETURNING value"
  ).bind(STATE_KEY, JSON.stringify(state), now).first();
  return Boolean(row);
}

async function save(db, state) {
  // An expired/slow invocation cannot overwrite the next owner's state.
  await db.prepare("UPDATE system_state SET value=?,updated_at=datetime('now') "
    + "WHERE key=? AND json_extract(value,'$.request_id')=?")
    .bind(JSON.stringify(state), STATE_KEY, state.request_id).run();
}

async function github(fetcher, token, path, init = {}) {
  const response = await fetcher(`${API}${path}`, {
    ...init, redirect: "manual", signal: AbortSignal.timeout(12000),
    headers: { accept: "application/vnd.github+json", authorization: `Bearer ${token}`,
      "x-github-api-version": "2026-03-10", "user-agent": "funghi-toscana-r6584-scheduler",
      ...(init.body ? { "content-type": "application/json" } : {}) },
  });
  if (!response.ok) throw new Error(`GITHUB_HTTP_${response.status}`);
  return response;
}

export async function check(env, { now = Date.now(), fetcher = fetch, requestId = crypto.randomUUID() } = {}) {
  const clock = romeClock(now);
  // DST follows Europe/Rome. No work before 01:15 local time.
  if (clock.minute < 75) return { status: "WAIT_WINDOW", target_day: clock.day };
  const active = await dailyActive(env.DB, clock.day);
  if (active) return { status: "DAILY_ALREADY_READY", target_day: clock.day, generation: active.generation };
  const state = { status: "CHECKING", target_day: clock.day, request_id: requestId,
    checked_at: new Date(now).toISOString(), lease_until: now + LEASE_MS };
  if (!await acquire(env.DB, state, now)) return { status: "LEASE_BUSY", target_day: clock.day };
  try {
    if (!env.GITHUB_ACTIONS_TOKEN) throw new Error("MISSING_GITHUB_ACTIONS_TOKEN");
    // Query by status: an old/stuck run cannot disappear behind a page of newer completions.
    for (const status of ["in_progress", "queued", "waiting", "pending", "requested"]) {
      const response = await github(fetcher, env.GITHUB_ACTIONS_TOKEN,
        `/runs?branch=main&status=${status}&per_page=1`);
      const data = await response.json();
      if (!Array.isArray(data.workflow_runs)) throw new Error("INVALID_GITHUB_RUNS_RESPONSE");
      if (data.workflow_runs.length) {
        state.status = "RUN_PENDING";
        state.run_id = data.workflow_runs[0].id;
        await save(env.DB, state);
        return state;
      }
    }
    // Catch another successful producer between lease acquisition and dispatch.
    const ready = await dailyActive(env.DB, clock.day);
    if (ready) {
      state.status = "DAILY_ALREADY_READY";
      state.generation = ready.generation;
    } else {
      const response = await github(fetcher, env.GITHUB_ACTIONS_TOKEN, "/dispatches", {
        method: "POST", body: JSON.stringify({ ref: "main", inputs: {
          mode: "full", target_day: clock.day, request_id: requestId, force_rebuild: "false",
        } }),
      });
      if (response.status === 200) {
        const accepted = await response.json();
        if (!Number.isSafeInteger(accepted.workflow_run_id) || accepted.workflow_run_id <= 0) {
          throw new Error("UNEXPECTED_DISPATCH_RESPONSE");
        }
        state.run_id = accepted.workflow_run_id;
      } else if (response.status !== 204) {
        throw new Error("UNEXPECTED_DISPATCH_RESPONSE");
      }
      // Accepted is NOT completed. Only a verified ACTIVE can satisfy the day.
      state.status = "DISPATCH_ACCEPTED";
    }
  } catch (error) {
    // No response bodies, credentials or arbitrary network error text in diagnostics.
    state.status = "RETRY_REQUIRED";
    const code = String(error?.message || "");
    state.error = /^(GITHUB_HTTP_\d{3}|MISSING_GITHUB_ACTIONS_TOKEN|INVALID_GITHUB_RUNS_RESPONSE|UNEXPECTED_DISPATCH_RESPONSE)$/.test(code)
      ? code : "NETWORK_OR_RUNTIME_ERROR";
  }
  await save(env.DB, state);
  return state;
}
