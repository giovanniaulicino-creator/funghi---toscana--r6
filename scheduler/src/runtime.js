import { check, dailyActive, romeClock, STATE_KEY } from "./core.js";

export const TICK_KEY = "R6585_LAST_TICK";

// One overwritten receipt, never a growing history or a scientific revision.
export async function runTick(env, options = {}) {
  const started = options.now ?? Date.now();
  const result = await check(env, options);
  const receipt = { revision: "R6585", source: "radar-cron-rpc", started_ms: started,
    checked_at: new Date(started).toISOString(), finished_at: new Date().toISOString(),
    status: result.status, target_day: result.target_day,
    ...(result.generation ? { generation: result.generation } : {}),
    ...(result.run_id ? { run_id: result.run_id } : {}),
    ...(result.error ? { error: result.error } : {}) };
  await env.DB.prepare("INSERT INTO system_state(key,value,updated_at) VALUES(?,?,datetime('now')) "
    + "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
    + "WHERE COALESCE(json_extract(system_state.value,'$.started_ms'),0)<=?")
    .bind(TICK_KEY, JSON.stringify(receipt), started).run();
  return receipt;
}

export async function probe(env) {
  const day = romeClock(Date.now()).day;
  const active = await dailyActive(env.DB, day);
  const read = async key => {
    const row = await env.DB.prepare("SELECT value FROM system_state WHERE key=?").bind(key).first();
    return row ? JSON.parse(row.value) : null;
  };
  return { revision: "R6585", target_day: day, ready: Boolean(active),
    generation: active?.generation || null, token_configured: Boolean(env.GITHUB_ACTIONS_TOKEN),
    last_check: await read(STATE_KEY), last_tick: await read(TICK_KEY) };
}
