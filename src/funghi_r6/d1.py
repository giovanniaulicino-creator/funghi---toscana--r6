from __future__ import annotations

import json
from typing import Any
import requests


class D1:
    def __init__(self, account_id: str, database_id: str, token: str):
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        r = requests.post(self.url, headers=self.headers, json={"sql": sql, "params": params or []}, timeout=45)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("success"):
            raise RuntimeError(payload.get("errors") or "D1 query failed")
        return payload.get("result") or []

    def batch(self, statements: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
        body = [{"sql": sql, "params": params} for sql, params in statements]
        r = requests.post(self.url, headers=self.headers, json=body, timeout=60)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("success"):
            raise RuntimeError(payload.get("errors") or "D1 batch failed")
        return payload.get("result") or []

    def init_schema(self, schema: str) -> None:
        # Schema contains simple independent CREATE statements; execute one-by-one for REST portability.
        for statement in [x.strip() for x in schema.split(";") if x.strip()]:
            self.query(statement)


    def load_catalog(self) -> list[dict[str, Any]]:
        result = self.query(
            "SELECT code,name,municipality,province,zone,altitude_m,lat,lon,registry_source FROM station_catalog ORDER BY code"
        )
        return ((result[0].get("results") if result else []) or [])

    def persist_catalog(self, stations: list[dict[str, Any]]) -> None:
        statements = []
        for s in stations:
            statements.append((
                "INSERT INTO station_catalog(code,name,municipality,province,zone,altitude_m,lat,lon,registry_source,updated_at) VALUES(?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name,municipality=excluded.municipality,province=excluded.province,zone=excluded.zone,altitude_m=excluded.altitude_m,lat=excluded.lat,lon=excluded.lon,registry_source=excluded.registry_source,updated_at=datetime('now')",
                [s.get("code"), s.get("name"), s.get("municipality"), s.get("province"), s.get("zone"), s.get("altitude_m"), s.get("lat"), s.get("lon"), s.get("registry_source")],
            ))
        self._chunked(statements)

    def persist_component_batch(self, component: str, records: list[dict[str, Any]]) -> None:
        statements = []
        for rec in records:
            statements.append((
                "INSERT INTO component_cache(code,component,payload_json,source,observed_at,quality,cycle_key,updated_at) VALUES(?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(code,component) DO UPDATE SET payload_json=excluded.payload_json,source=excluded.source,observed_at=excluded.observed_at,quality=excluded.quality,cycle_key=excluded.cycle_key,updated_at=datetime('now')",
                [rec["code"], component, json.dumps(rec["payload"], separators=(",", ":")), "open-meteo", rec.get("fetched_at"), "model_exact_point", rec.get("cycle_key")],
            ))
        self._chunked(statements)


    def load_component_map(self, component: str, cycle_key: str) -> dict[str, dict[str, Any]]:
        result = self.query(
            "SELECT code,payload_json,observed_at,cycle_key FROM component_cache WHERE component=? AND cycle_key=?",
            [component, cycle_key],
        )
        rows = (result[0].get("results") if result else []) or []
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                continue
            out[str(row.get("code") or "")] = {
                "code": row.get("code"), "component": component,
                "fetched_at": row.get("observed_at"), "cycle_key": row.get("cycle_key"),
                "payload": payload,
            }
        return out

    def persist_daily(self, rows: list[dict[str, Any]]) -> None:
        statements = []
        for row in rows:
            statements.append((
                "INSERT INTO daily_history(code,day,rain_mm,temperature_min_c,temperature_max_c,temperature_mean_c,humidity_mean_pct,wind_mean_ms,vpd_max_kpa,et0_mm,source,quality,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(code,day) DO UPDATE SET rain_mm=excluded.rain_mm,temperature_min_c=excluded.temperature_min_c,temperature_max_c=excluded.temperature_max_c,temperature_mean_c=excluded.temperature_mean_c,humidity_mean_pct=excluded.humidity_mean_pct,wind_mean_ms=excluded.wind_mean_ms,vpd_max_kpa=excluded.vpd_max_kpa,et0_mm=excluded.et0_mm,source=excluded.source,quality=excluded.quality,updated_at=datetime('now')",
                [row.get("code"), row.get("day"), row.get("rain_mm"), row.get("temperature_min_c"), row.get("temperature_max_c"), row.get("temperature_mean_c"), row.get("humidity_mean_pct"), row.get("wind_mean_ms"), row.get("vpd_max_kpa"), row.get("et0_mm"), row.get("source"), row.get("quality")],
            ))
        self._chunked(statements)

    def load_history(self, code: str, days: int = 35) -> list[dict[str, Any]]:
        result = self.query(
            "SELECT code,day,rain_mm,temperature_min_c,temperature_max_c,temperature_mean_c,humidity_mean_pct,wind_mean_ms,vpd_max_kpa,et0_mm,source,quality FROM daily_history WHERE code=? ORDER BY day DESC LIMIT ?",
            [code, days],
        )
        rows = (result[0].get("results") if result else []) or []
        return list(reversed(rows))

    def source_health(self, source: str, health: dict[str, Any]) -> None:
        self.query(
            "INSERT INTO source_health(source,reachable,acquired,last_success_at,last_error,reference_at,details_json,updated_at) VALUES(?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(source) DO UPDATE SET reachable=excluded.reachable,acquired=excluded.acquired,last_success_at=excluded.last_success_at,last_error=excluded.last_error,reference_at=excluded.reference_at,details_json=excluded.details_json,updated_at=datetime('now')",
            [source, 1 if health.get("reachable") else 0, int(health.get("acquired") or 0), health.get("last_success_at"), health.get("last_error"), health.get("reference_time"), json.dumps(health, separators=(",", ":"))],
        )

    def publish_generation(self, generation: str, target_day: str, stations: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
        self.query(
            "INSERT OR REPLACE INTO generation_runs(generation,generated_at,target_day,status,structural_total,scientific_total,manifest_json) VALUES(?,datetime('now'),?,'STAGING',?,?,?)",
            [generation, target_day, manifest["coverage"]["structural"], manifest["coverage"]["scientific_complete"], json.dumps(manifest, separators=(",", ":"))],
        )
        statements = [(
            "INSERT OR REPLACE INTO generation_station(generation,code,payload_json,complete) VALUES(?,?,?,1)",
            [generation, s["code"], json.dumps(s, separators=(",", ":"))],
        ) for s in stations]
        self._chunked(statements)
        # One D1 batch = atomic pointer swap: previous active is retained and new generation becomes visible in one transaction.
        self.batch([
            ("UPDATE generation_runs SET status='PREVIOUS' WHERE generation=(SELECT value FROM system_state WHERE key='ACTIVE') AND status='ACTIVE'", []),
            ("INSERT INTO system_state(key,value,updated_at) VALUES('ACTIVE_PREVIOUS',(SELECT value FROM system_state WHERE key='ACTIVE'),datetime('now')) ON CONFLICT(key) DO UPDATE SET value=(SELECT value FROM system_state WHERE key='ACTIVE'),updated_at=datetime('now')", []),
            ("INSERT INTO system_state(key,value,updated_at) VALUES('ACTIVE',?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')", [generation]),
            ("UPDATE generation_runs SET status='ACTIVE' WHERE generation=?", [generation]),
        ])

    def _chunked(self, statements: list[tuple[str, list[Any]]], size: int = 50) -> None:
        for i in range(0, len(statements), size):
            self.batch(statements[i:i + size])
