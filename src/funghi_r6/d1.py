from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Any

import requests


SNAPSHOT_CHUNK_BYTES = 512_000
D1_WRITE_BATCH_SIZE = 50
D1_SNAPSHOT_BATCH_SIZE = 4

REQUIRED_COVERAGE_KEYS = (
    "structural",
    "current",
    "rain_5_7_15_30",
    "history_30",
    "forecast_7",
    "forecast_15",
    "et0",
    "soil",
    "scientific_complete",
)


def _rows(result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return ((result[0].get("results") if result else []) or [])


def split_utf8_chunks(raw: bytes, max_bytes: int = SNAPSHOT_CHUNK_BYTES) -> list[tuple[str, int]]:
    """Split exact UTF-8 bytes without cutting a multibyte code point."""
    if max_bytes < 4:
        raise ValueError("max_bytes troppo piccolo")
    if not raw:
        return [("", 0)]

    chunks: list[tuple[str, int]] = []
    start = 0
    while start < len(raw):
        end = min(start + max_bytes, len(raw))
        if end < len(raw):
            while end > start and (raw[end] & 0xC0) == 0x80:
                end -= 1
        if end <= start:
            raise RuntimeError("Impossibile creare chunk UTF-8 valido")
        piece = raw[start:end]
        text = piece.decode("utf-8")
        chunks.append((text, len(piece)))
        start = end
    return chunks


def validate_snapshot_contract(
    generation: str,
    target_day: str,
    stations: list[dict[str, Any]],
    manifest: dict[str, Any],
    raw_snapshot: bytes,
    expected_stations: int,
) -> dict[str, Any]:
    if len(stations) != expected_stations:
        raise RuntimeError(f"Gate R6: stazioni {len(stations)}/{expected_stations}")

    if int(manifest.get("station_count") or 0) != expected_stations:
        raise RuntimeError("Gate R6: station_count manifest non valido")
    if manifest.get("generation") != generation:
        raise RuntimeError("Gate R6: generation manifest incoerente")
    if manifest.get("target_day") != target_day:
        raise RuntimeError("Gate R6: target_day manifest incoerente")
    if manifest.get("scientifically_complete") is not True:
        raise RuntimeError("Gate R6: manifest non scientificamente completo")

    cov = manifest.get("coverage") or {}
    for key in REQUIRED_COVERAGE_KEYS:
        if int(cov.get(key) or 0) != expected_stations:
            raise RuntimeError(f"Gate R6: coverage {key}={cov.get(key)} / {expected_stations}")

    actual_sha = hashlib.sha256(raw_snapshot).hexdigest()
    if manifest.get("sha256") != actual_sha:
        raise RuntimeError("Gate R6: SHA-256 manifest/snapshot non coincide")
    if int(manifest.get("snapshot_bytes") or -1) != len(raw_snapshot):
        raise RuntimeError("Gate R6: dimensione manifest/snapshot non coincide")

    try:
        payload = json.loads(raw_snapshot.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Gate R6: snapshot JSON non valido: {exc}") from exc

    meta = payload.get("meta") or {}
    data = payload.get("data") or []
    if meta.get("generation") != generation:
        raise RuntimeError("Gate R6: generation snapshot incoerente")
    if meta.get("target_day") != target_day:
        raise RuntimeError("Gate R6: target_day snapshot incoerente")
    if meta.get("scientifically_complete") is not True:
        raise RuntimeError("Gate R6: snapshot non scientificamente completo")
    if len(data) != expected_stations:
        raise RuntimeError(f"Gate R6: data snapshot {len(data)}/{expected_stations}")

    codes = [str(x.get("code") or "") for x in data]
    if len(set(codes)) != expected_stations or any(not c for c in codes):
        raise RuntimeError("Gate R6: codici stazione mancanti o duplicati")

    return {
        "sha256": actual_sha,
        "snapshot_bytes": len(raw_snapshot),
        "station_rows": expected_stations,
    }


class D1:
    def __init__(self, account_id: str, database_id: str, token: str, *, max_retries: int = 5):
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.max_retries = max(1, int(max_retries))

    @staticmethod
    def _transient_message(message: str) -> bool:
        text = message.lower()
        markers = (
            "network connection lost",
            "storage caused object to be reset",
            "reset because its code was updated",
            "overloaded",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "rate limit",
            "too many requests",
        )
        return any(x in text for x in markers)

    def _sleep(self, attempt: int) -> None:
        delay = min(8.0, 0.5 * (2 ** max(0, attempt - 1))) + random.uniform(0.0, 0.25)
        time.sleep(delay)

    def _post(self, body: Any, *, timeout: int) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = requests.post(self.url, headers=self.headers, json=body, timeout=timeout)
                if r.status_code == 429 or 500 <= r.status_code <= 599:
                    message = f"D1 HTTP {r.status_code}: {r.text[:500]}"
                    if attempt < self.max_retries:
                        self._sleep(attempt)
                        continue
                    raise RuntimeError(message)
                r.raise_for_status()
                payload = r.json()
                if not isinstance(payload, dict) or not payload.get("success"):
                    message = str((payload or {}).get("errors") if isinstance(payload, dict) else payload)
                    if attempt < self.max_retries and self._transient_message(message):
                        self._sleep(attempt)
                        continue
                    raise RuntimeError(message or "D1 request failed")

                result = payload.get("result") or []
                failed = [
                    item for item in result
                    if isinstance(item, dict) and item.get("success") is False
                ]
                if failed:
                    message = json.dumps(failed, ensure_ascii=False)[:2000]
                    if attempt < self.max_retries and self._transient_message(message):
                        self._sleep(attempt)
                        continue
                    raise RuntimeError(f"D1 statement failed: {message}")
                return result
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self._sleep(attempt)
                    continue
                raise RuntimeError(f"D1 rete/timeout dopo {attempt} tentativi: {exc}") from exc
            except requests.HTTPError as exc:
                last_error = exc
                raise RuntimeError(f"D1 HTTP non recuperabile: {exc}") from exc
            except RuntimeError as exc:
                last_error = exc
                raise

        raise RuntimeError(f"D1 request failed: {last_error}")

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return self._post({"sql": sql, "params": params or []}, timeout=45)

    def batch(self, statements: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
        if not statements:
            return []
        body = [{"sql": sql, "params": params} for sql, params in statements]
        return self._post(body, timeout=60)

    def init_schema(self, schema: str) -> None:
        for statement in [x.strip() for x in schema.split(";") if x.strip()]:
            self.query(statement)

    def load_catalog(self) -> list[dict[str, Any]]:
        result = self.query(
            "SELECT code,name,municipality,province,zone,altitude_m,lat,lon,registry_source "
            "FROM station_catalog ORDER BY code"
        )
        return _rows(result)

    def persist_catalog(self, stations: list[dict[str, Any]]) -> None:
        statements = []
        for s in stations:
            statements.append((
                "INSERT INTO station_catalog(code,name,municipality,province,zone,altitude_m,lat,lon,registry_source,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name,municipality=excluded.municipality,"
                "province=excluded.province,zone=excluded.zone,altitude_m=excluded.altitude_m,lat=excluded.lat,"
                "lon=excluded.lon,registry_source=excluded.registry_source,updated_at=datetime('now')",
                [s.get("code"), s.get("name"), s.get("municipality"), s.get("province"), s.get("zone"),
                 s.get("altitude_m"), s.get("lat"), s.get("lon"), s.get("registry_source")],
            ))
        self._chunked(statements)

    def persist_component_batch(self, component: str, records: list[dict[str, Any]]) -> None:
        statements = []
        for rec in records:
            statements.append((
                "INSERT INTO component_cache(code,component,payload_json,source,observed_at,quality,cycle_key,updated_at) "
                "VALUES(?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(code,component) DO UPDATE SET payload_json=excluded.payload_json,source=excluded.source,"
                "observed_at=excluded.observed_at,quality=excluded.quality,cycle_key=excluded.cycle_key,updated_at=datetime('now')",
                [rec["code"], component, json.dumps(rec["payload"], separators=(",", ":")), "open-meteo",
                 rec.get("fetched_at"), "model_exact_point", rec.get("cycle_key")],
            ))
        self._chunked(statements)

    def load_component_map(self, component: str, cycle_key: str) -> dict[str, dict[str, Any]]:
        result = self.query(
            "SELECT code,payload_json,observed_at,cycle_key FROM component_cache WHERE component=? AND cycle_key=?",
            [component, cycle_key],
        )
        out: dict[str, dict[str, Any]] = {}
        for row in _rows(result):
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                continue
            out[str(row.get("code") or "")] = {
                "code": row.get("code"),
                "component": component,
                "fetched_at": row.get("observed_at"),
                "cycle_key": row.get("cycle_key"),
                "payload": payload,
            }
        return out

    def persist_daily(self, rows: list[dict[str, Any]]) -> None:
        statements = []
        for row in rows:
            statements.append((
                "INSERT INTO daily_history(code,day,rain_mm,temperature_min_c,temperature_max_c,temperature_mean_c,"
                "humidity_mean_pct,wind_mean_ms,vpd_max_kpa,et0_mm,source,quality,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(code,day) DO UPDATE SET rain_mm=excluded.rain_mm,"
                "temperature_min_c=excluded.temperature_min_c,temperature_max_c=excluded.temperature_max_c,"
                "temperature_mean_c=excluded.temperature_mean_c,humidity_mean_pct=excluded.humidity_mean_pct,"
                "wind_mean_ms=excluded.wind_mean_ms,vpd_max_kpa=excluded.vpd_max_kpa,et0_mm=excluded.et0_mm,"
                "source=excluded.source,quality=excluded.quality,updated_at=datetime('now')",
                [row.get("code"), row.get("day"), row.get("rain_mm"), row.get("temperature_min_c"),
                 row.get("temperature_max_c"), row.get("temperature_mean_c"), row.get("humidity_mean_pct"),
                 row.get("wind_mean_ms"), row.get("vpd_max_kpa"), row.get("et0_mm"), row.get("source"), row.get("quality")],
            ))
        self._chunked(statements)

    def load_history(self, code: str, days: int = 35) -> list[dict[str, Any]]:
        result = self.query(
            "SELECT code,day,rain_mm,temperature_min_c,temperature_max_c,temperature_mean_c,humidity_mean_pct,"
            "wind_mean_ms,vpd_max_kpa,et0_mm,source,quality FROM daily_history "
            "WHERE code=? ORDER BY day DESC LIMIT ?",
            [code, days],
        )
        return list(reversed(_rows(result)))

    def load_history_map(self, start_day: str, days: int = 35) -> dict[str, list[dict[str, Any]]]:
        result = self.query(
            "SELECT code,day,rain_mm,temperature_min_c,temperature_max_c,temperature_mean_c,humidity_mean_pct,"
            "wind_mean_ms,vpd_max_kpa,et0_mm,source,quality FROM daily_history "
            "WHERE day>=? ORDER BY code,day",
            [start_day],
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in _rows(result):
            grouped.setdefault(str(row.get("code") or ""), []).append(row)
        return {code: rows[-days:] for code, rows in grouped.items()}

    def source_health(self, source: str, health: dict[str, Any]) -> None:
        self.query(
            "INSERT INTO source_health(source,reachable,acquired,last_success_at,last_error,reference_at,details_json,updated_at) "
            "VALUES(?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(source) DO UPDATE SET reachable=excluded.reachable,acquired=excluded.acquired,"
            "last_success_at=excluded.last_success_at,last_error=excluded.last_error,reference_at=excluded.reference_at,"
            "details_json=excluded.details_json,updated_at=datetime('now')",
            [source, 1 if health.get("reachable") else 0, int(health.get("acquired") or 0),
             health.get("last_success_at"), health.get("last_error"), health.get("reference_time"),
             json.dumps(health, separators=(",", ":"))],
        )

    def active_generation(self) -> str | None:
        result = self.query("SELECT value FROM system_state WHERE key='ACTIVE'")
        rows = _rows(result)
        return str(rows[0].get("value")) if rows and rows[0].get("value") else None

    def _persist_snapshot_chunks(self, generation: str, raw_snapshot: bytes, sha256: str) -> int:
        chunks = split_utf8_chunks(raw_snapshot)
        self.query("DELETE FROM generation_snapshot_chunk WHERE generation=?", [generation])
        self.query("DELETE FROM generation_snapshot WHERE generation=?", [generation])
        self.query(
            "INSERT INTO generation_snapshot(generation,sha256,byte_length,chunk_count,verified,created_at) "
            "VALUES(?,?,?,?,0,datetime('now'))",
            [generation, sha256, len(raw_snapshot), len(chunks)],
        )
        statements = [
            (
                "INSERT INTO generation_snapshot_chunk(generation,chunk_index,chunk_text,raw_bytes) VALUES(?,?,?,?)",
                [generation, index, text, raw_bytes],
            )
            for index, (text, raw_bytes) in enumerate(chunks)
        ]
        self._chunked(statements, size=D1_SNAPSHOT_BATCH_SIZE)
        return len(chunks)

    def _read_snapshot_bytes(self, generation: str, expected_chunks: int) -> bytes:
        result = self.query(
            "SELECT chunk_index,chunk_text,raw_bytes FROM generation_snapshot_chunk "
            "WHERE generation=? ORDER BY chunk_index",
            [generation],
        )
        rows = _rows(result)
        if len(rows) != expected_chunks:
            raise RuntimeError(f"D1 snapshot chunks {len(rows)}/{expected_chunks}")

        parts: list[bytes] = []
        for expected_index, row in enumerate(rows):
            if int(row.get("chunk_index") or 0) != expected_index:
                raise RuntimeError("D1 snapshot chunk_index non contiguo")
            part = str(row.get("chunk_text") or "").encode("utf-8")
            if len(part) != int(row.get("raw_bytes") or 0):
                raise RuntimeError(f"D1 snapshot chunk {expected_index}: byte_length incoerente")
            parts.append(part)
        return b"".join(parts)

    def _generation_counts(self, generation: str) -> tuple[int, int]:
        result = self.query(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN complete=1 THEN 1 ELSE 0 END) AS complete "
            "FROM generation_station WHERE generation=?",
            [generation],
        )
        rows = _rows(result)
        row = rows[0] if rows else {}
        return int(row.get("total") or 0), int(row.get("complete") or 0)

    def publish_generation(
        self,
        generation: str,
        target_day: str,
        stations: list[dict[str, Any]],
        manifest: dict[str, Any],
        raw_snapshot: bytes,
        *,
        expected_stations: int,
    ) -> dict[str, Any]:
        contract = validate_snapshot_contract(
            generation, target_day, stations, manifest, raw_snapshot, expected_stations
        )

        staging_manifest = dict(manifest)
        staging_manifest["publication"] = "D1_STAGING"

        self.query(
            "INSERT OR REPLACE INTO generation_runs(generation,generated_at,target_day,status,structural_total,"
            "scientific_total,manifest_json) VALUES(?,datetime('now'),?,'STAGING',?,?,?)",
            [generation, target_day, expected_stations, expected_stations,
             json.dumps(staging_manifest, separators=(",", ":"))],
        )

        self.query("DELETE FROM generation_station WHERE generation=?", [generation])
        station_statements = [
            (
                "INSERT INTO generation_station(generation,code,complete) VALUES(?,?,1)",
                [generation, s["code"]],
            )
            for s in stations
        ]
        self._chunked(station_statements)

        chunk_count = self._persist_snapshot_chunks(generation, raw_snapshot, contract["sha256"])
        readback = self._read_snapshot_bytes(generation, chunk_count)
        readback_sha = hashlib.sha256(readback).hexdigest()
        if readback != raw_snapshot or readback_sha != contract["sha256"]:
            raise RuntimeError("D1 snapshot readback non identico all'artefatto canonico")

        self.query(
            "UPDATE generation_snapshot SET verified=1 "
            "WHERE generation=? AND sha256=? AND byte_length=? AND chunk_count=?",
            [generation, contract["sha256"], len(raw_snapshot), chunk_count],
        )

        total_rows, complete_rows = self._generation_counts(generation)
        if total_rows != expected_stations or complete_rows != expected_stations:
            raise RuntimeError(
                f"D1 generation_station incompleta: total={total_rows}, complete={complete_rows}, atteso={expected_stations}"
            )

        verification = {
            **contract,
            "snapshot_chunks": chunk_count,
            "readback_verified": True,
            "active_generation": generation,
        }
        active_manifest = dict(manifest)
        active_manifest["publication"] = "ACTIVE"
        active_manifest["d1_verification"] = verification

        guard = (
            "EXISTS("
            "SELECT 1 FROM generation_runs r "
            "JOIN generation_snapshot s ON s.generation=r.generation "
            "WHERE r.generation=? AND r.status='STAGING' "
            "AND r.structural_total=? AND r.scientific_total=? "
            "AND s.verified=1 AND s.sha256=? AND s.byte_length=? "
            "AND (SELECT COUNT(*) FROM generation_station gs "
            "     WHERE gs.generation=r.generation AND gs.complete=1)=?"
            ")"
        )
        gp = [
            generation, expected_stations, expected_stations,
            contract["sha256"], len(raw_snapshot), expected_stations,
        ]

        # Un unico batch D1: il vecchio ACTIVE resta disponibile se una qualsiasi
        # istruzione fallisce. Il guard rende anche un eventuale retry idempotente.
        self.batch([
            (
                f"UPDATE generation_runs SET status='PREVIOUS' "
                f"WHERE generation=(SELECT value FROM system_state WHERE key='ACTIVE') "
                f"AND status='ACTIVE' AND {guard}",
                gp,
            ),
            (
                f"INSERT INTO system_state(key,value,updated_at) "
                f"SELECT 'ACTIVE_PREVIOUS',value,datetime('now') FROM system_state "
                f"WHERE key='ACTIVE' AND value IS NOT NULL AND value<>? AND {guard} "
                f"ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                [generation, *gp],
            ),
            (
                f"INSERT INTO system_state(key,value,updated_at) "
                f"SELECT 'ACTIVE',?,datetime('now') WHERE {guard} "
                f"ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                [generation, *gp],
            ),
            (
                f"UPDATE generation_runs SET status='ACTIVE',manifest_json=? "
                f"WHERE generation=? AND status='STAGING' AND {guard}",
                [json.dumps(active_manifest, separators=(",", ":")), generation, *gp],
            ),
        ])

        active = self.active_generation()
        if active != generation:
            raise RuntimeError(
                f"D1 ACTIVE gate non commutato: atteso {generation}, trovato {active or 'NULL'}"
            )

        try:
            self.cleanup_old_generations()
        except Exception as exc:
            # Non invalidare un ACTIVE gia' verificato per un problema di housekeeping.
            print(f"[R6] WARNING cleanup generazioni D1: {exc}", flush=True)

        return verification

    def cleanup_old_generations(self) -> None:
        keep_sql = (
            "SELECT value FROM system_state "
            "WHERE key IN ('ACTIVE','ACTIVE_PREVIOUS') AND value IS NOT NULL"
        )
        for table in (
            "generation_snapshot_chunk",
            "generation_snapshot",
            "generation_station",
            "generation_runs",
        ):
            self.query(f"DELETE FROM {table} WHERE generation NOT IN ({keep_sql})")

    def _chunked(self, statements: list[tuple[str, list[Any]]], size: int = D1_WRITE_BATCH_SIZE) -> None:
        for i in range(0, len(statements), size):
            self.batch(statements[i:i + size])
