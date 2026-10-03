# R638-BACKEND-COPERNICUS-LONGHYDRO-2026-10-03
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import gzip
import hashlib
import json
from pathlib import Path

from .assemble import assemble_station, coverage
from .catalog import fetch_catalog
from .copernicus import fetch_copernicus_bundle
from .config import SETTINGS
from .d1 import D1
from .http import HttpClient
from .open_meteo import fetch_component, daily_rows
from .sir import fetch_official_rain


def _open_meteo_health(
    component_cache: dict[str, dict[str, dict]],
    errors: dict[str, list[str]],
    expected_stations: int,
) -> dict:
    # reachable e complete sono volutamente distinti:
    # una fonte puo essere raggiungibile pur avendo copertura parziale.
    components = ("current", "forecast", "soil")
    code_sets = [set((component_cache.get(name) or {}).keys()) for name in components]
    complete_codes = set.intersection(*code_sets) if code_sets else set()

    records = [
        rec
        for name in components
        for rec in (component_cache.get(name) or {}).values()
        if isinstance(rec, dict)
    ]
    fetched = sorted(
        str(rec.get("fetched_at"))
        for rec in records
        if rec.get("fetched_at")
    )
    last_success = fetched[-1] if fetched else None

    relevant_errors = {
        name: list(errors.get(name) or [])
        for name in components
        if errors.get(name)
    }
    flat_errors = [
        f"{name}: {message}"
        for name, messages in relevant_errors.items()
        for message in messages
    ]

    acquired = len(complete_codes)
    return {
        "reachable": bool(records),
        "acquired": acquired,
        "complete": acquired == expected_stations,
        "current": len(component_cache.get("current") or {}),
        "forecast": len(component_cache.get("forecast") or {}),
        "soil": len(component_cache.get("soil") or {}),
        "last_success_at": last_success,
        "last_error": " | ".join(flat_errors)[:2000] if flat_errors else None,
        "reference_time": last_success,
        "errors": relevant_errors,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Funghi Toscana AI R6 Data Builder")
    p.add_argument("--mode", choices=["bootstrap", "full", "light"], default="full")
    p.add_argument("--out", default="out")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    http = HttpClient(max_retries=SETTINGS.max_retries)
    d1 = (
        D1(
            SETTINGS.cf_account_id,
            SETTINGS.cf_d1_database_id,
            SETTINGS.cf_api_token,
            max_retries=SETTINGS.max_retries,
        )
        if SETTINGS.d1_enabled
        else None
    )

    if args.mode in {"full", "light"} and not d1:
        payload = {
            "status": "SKIPPED_NO_D1",
            "mode": args.mode,
            "reason": "full/light richiedono lo storico persistente nel D1 R6; usare bootstrap finche il D1 non e configurato",
        }
        _write_diag(out_dir, payload)
        raise SystemExit(
            "R6 full/light richiedono D1 configurato. Nessun ACTIVE toccato; eseguire bootstrap oppure configurare D1 R6."
        )

    if d1:
        d1.init_schema(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

    persisted_catalog = d1.load_catalog() if d1 else []
    stations, catalog_health = fetch_catalog(
        http,
        expected=SETTINGS.expected_stations,
        persisted=persisted_catalog,
    )
    if d1:
        d1.persist_catalog(stations)
        d1.source_health("sir_registry", catalog_health)
    if len(stations) != SETTINGS.expected_stations:
        _write_diag(
            out_dir,
            {"status": "BLOCKED", "reason": "station_catalog_count", "catalog": catalog_health},
        )
        raise SystemExit(
            f"Catalogo strutturale {len(stations)}/{SETTINGS.expected_stations}: ACTIVE non pubblicabile"
        )

    official_rain, sir_health = fetch_official_rain(
        http,
        allow_browser=True,
        expected_codes={str(s["code"]).strip().upper() for s in stations},
    )
    if d1:
        d1.source_health("sir_rainfall", sir_health)

    local_now = datetime.now(ZoneInfo(SETTINGS.timezone))
    target_day = local_now.date().isoformat()
    slot = (local_now.hour // 6) * 6
    cycle_key = f"{local_now.date().isoformat()}T{slot:02d}-{args.mode}"

    component_cache: dict[str, dict[str, dict]] = {}
    errors: dict[str, list[str]] = {}

    def persist_batch(component: str, rows: list[dict]):
        if d1:
            d1.persist_component_batch(component, rows)

    for component in ["current", "forecast", "soil"]:
        existing = d1.load_component_map(component, cycle_key) if d1 else {}
        targets = [s for s in stations if s["code"] not in existing]
        recs, errs = fetch_component(
            http,
            targets,
            component=component,
            batch_size=SETTINGS.batch_size,
            pause_s=SETTINGS.batch_pause_s,
            on_batch=persist_batch,
            cycle_key=cycle_key,
        )
        component_cache[component] = {**existing, **recs}
        errors[component] = errs

    history_by_code: dict[str, list[dict]] = {}

    if args.mode == "bootstrap":
        existing = d1.load_component_map("history_bootstrap", cycle_key) if d1 else {}
        targets = [s for s in stations if s["code"] not in existing]
        recs, errs = fetch_component(
            http,
            targets,
            component="history_bootstrap",
            batch_size=SETTINGS.batch_size,
            pause_s=SETTINGS.batch_pause_s,
            on_batch=persist_batch,
            cycle_key=cycle_key,
        )
        recs = {**existing, **recs}
        errors["history_bootstrap"] = errs
        for code, rec in recs.items():
            rows = daily_rows(code, rec["payload"])
            history_by_code[code] = rows
            if d1:
                d1.persist_daily(rows)

    # Il forecast porta con se i due giorni appena trascorsi: vengono sempre
    # sovrascritti/aggiornati nello storico persistente.
    for code, rec in component_cache.get("forecast", {}).items():
        rows = [r for r in daily_rows(code, rec["payload"]) if r["day"] < target_day]
        if d1 and rows:
            d1.persist_daily(rows)
        if not d1 and code not in history_by_code:
            history_by_code[code] = rows

    if d1 and args.mode != "bootstrap":
        history_start = (
            date.fromisoformat(target_day) - timedelta(days=100)
        ).isoformat()
        history_by_code = d1.load_history_map(history_start, 95)

        # Self-healing: se il D1 e nuovo o qualche storico e' incompleto,
        # recupera 35 giorni solo per le stazioni necessarie.
        deficient = [
            s for s in stations
            if len(history_by_code.get(s["code"]) or []) < 88
        ]
        if deficient:
            recovery_cycle = f"{cycle_key}-history-recovery"
            recs, errs = fetch_component(
                http,
                deficient,
                component="history_bootstrap",
                batch_size=SETTINGS.batch_size,
                pause_s=SETTINGS.batch_pause_s,
                on_batch=persist_batch,
                cycle_key=recovery_cycle,
            )
            errors["history_recovery"] = errs
            for code, rec in recs.items():
                rows = daily_rows(code, rec["payload"])
                d1.persist_daily(rows)
            history_by_code = d1.load_history_map(history_start, 95)

    # R6.38 — Copernicus indipendente, fail-soft e cache giornaliera D1.
    # La mancanza di credenziali o di pixel validi NON blocca ACTIVE: viene
    # registrata in source_health e il modello riduce l'affidabilita' locale.
    copernicus_cache: dict[str, dict] = {}
    copernicus_cycle = f"{target_day}-copernicus-r638"
    if d1:
        copernicus_cache = d1.load_component_map("copernicus", copernicus_cycle)
    if len(copernicus_cache) == SETTINGS.expected_stations:
        copernicus_health = {
            "status": "CACHE", "configured": True, "reachable": True,
            "acquired": len(copernicus_cache), "expected": SETTINGS.expected_stations,
            "complete": True, "last_success_at": max(
                (str(x.get("fetched_at")) for x in copernicus_cache.values() if x.get("fetched_at")),
                default=None,
            ),
            "last_error": None, "reference_time": target_day,
            "note": "Copernicus R6.38 riusato dalla cache D1 giornaliera.",
        }
    else:
        cached_payloads = {
            code: (rec.get("payload") or {})
            for code, rec in copernicus_cache.items()
            if isinstance(rec, dict)
        }
        missing_stations = [s for s in stations if s["code"] not in cached_payloads]
        fresh_payloads, copernicus_health = fetch_copernicus_bundle(missing_stations)
        fetched_at = datetime.now(timezone.utc).isoformat()
        fresh_rows = []
        configured = copernicus_health.get("configured") is True
        for station in missing_stations:
            code = station["code"]
            payload = fresh_payloads.get(code) or {}
            cached_payloads[code] = payload
            # Se il client e' configurato, memorizziamo anche l'esito vuoto di una
            # cella senza pixel validi per non martellare l'API nello stesso giorno.
            # Se mancano le credenziali, invece, non creiamo una falsa cache completa.
            if configured:
                fresh_rows.append({
                    "code": code, "component": "copernicus",
                    "fetched_at": fetched_at, "cycle_key": copernicus_cycle,
                    "payload": payload,
                })
        if d1 and fresh_rows:
            d1.persist_component_batch("copernicus", fresh_rows)
            copernicus_cache = d1.load_component_map("copernicus", copernicus_cycle)
        else:
            copernicus_cache = {
                code: {"code": code, "component": "copernicus", "fetched_at": fetched_at, "cycle_key": copernicus_cycle, "payload": payload}
                for code, payload in cached_payloads.items()
            }
        total_payloads = [(rec.get("payload") or {}) for rec in copernicus_cache.values() if isinstance(rec, dict)]
        nonempty = sum(1 for payload in total_payloads if payload)
        soil_count = sum(1 for payload in total_payloads if payload.get("satellite_surface_soil_moisture_pct") is not None or payload.get("satellite_soil_water_index_pct") is not None)
        veg_count = sum(1 for payload in total_payloads if payload.get("ndvi_current") is not None or payload.get("ndmi_current") is not None or payload.get("evi_current") is not None)
        copernicus_health.update({
            "acquired": nonempty, "expected": SETTINGS.expected_stations,
            "complete": nonempty == SETTINGS.expected_stations,
            "soil_acquired": soil_count, "vegetation_acquired": veg_count,
        })
    if d1:
        d1.source_health("copernicus", copernicus_health)

    assembled: list[dict] = []
    for station in stations:
        code = station["code"]
        station_out = assemble_station(
            station,
            official_rain=official_rain.get(code),
            current=(component_cache.get("current", {}).get(code) or {}).get("payload"),
            forecast=(component_cache.get("forecast", {}).get(code) or {}).get("payload"),
            soil=(component_cache.get("soil", {}).get(code) or {}).get("payload"),
            history=history_by_code.get(code) or [],
            target_day=target_day,
        )
        cop_payload = (copernicus_cache.get(code) or {}).get("payload") or {}
        if cop_payload:
            station_out.update(cop_payload)
            prov = station_out.setdefault("provenance", {})
            for key in (
                "satellite_surface_soil_moisture_pct", "satellite_soil_water_index_pct",
                "ndvi_current", "ndmi_current", "evi_current",
            ):
                if station_out.get(key) is not None:
                    prov[key] = {
                        "source": station_out.get("copernicus_soil_source_kind")
                            if key.startswith("satellite_")
                            else station_out.get("vegetation_indices_source_kind"),
                        "timestamp": station_out.get("copernicus_ssm_observed_at")
                            if key == "satellite_surface_soil_moisture_pct"
                            else station_out.get("copernicus_swi_observed_at")
                            if key == "satellite_soil_water_index_pct"
                            else station_out.get("vegetation_indices_window_end"),
                        "quality": "independent_satellite_observation",
                    }
        assembled.append(station_out)

    cov = coverage(assembled)
    generated_at = datetime.now(timezone.utc).isoformat()
    generation = f"R6-{target_day}-{datetime.now(timezone.utc).strftime('%H%M%SZ')}"
    open_meteo_health = _open_meteo_health(
        component_cache,
        errors,
        SETTINGS.expected_stations,
    )
    if d1:
        d1.source_health("open_meteo", open_meteo_health)

    sources = {
        "sir_cfr": sir_health,
        "sir_registry": catalog_health,
        "open_meteo": open_meteo_health,
        "copernicus": copernicus_health,
        "radar": {
            "status": "SEPARATE_UNCHANGED",
            "worker": "https://funghi-toscana-radar.porcinitoscanaai.workers.dev",
        },
    }
    complete = all(
        cov[k] == SETTINGS.expected_stations
        for k in [
            "structural",
            "current",
            "rain_5_7_15_30",
            "history_90",
            "long_hydro_90",
            "forecast_7",
            "forecast_15",
            "et0",
            "soil",
            "scientific_complete",
        ]
    )
    snapshot = {
        "meta": {
            "architecture": "r6-external-builder-d1-canonical",
            "model_version": SETTINGS.model_version,
            "generation": generation,
            "generated_at": generated_at,
            "target_day": target_day,
            "cycle_key": cycle_key,
            "scientifically_complete": complete,
            "coverage": cov,
            "source_status": sources,
        },
        "data": assembled,
    }
    raw = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()

    manifest = {
        "architecture": "R6",
        "generation": generation,
        "generated_at": generated_at,
        "target_day": target_day,
        "cycle_key": cycle_key,
        "station_count": len(assembled),
        "coverage": cov,
        "scientifically_complete": complete,
        "sha256": sha256,
        "snapshot_bytes": len(raw),
        "source_status": sources,
        "publication": (
            "D1_PENDING"
            if complete and d1
            else "ARTIFACT_ONLY"
            if complete
            else "BLOCKED_INCOMPLETE"
        ),
    }

    (out_dir / "snapshot.json").write_bytes(raw)
    with gzip.open(out_dir / "snapshot.json.gz", "wb", compresslevel=9) as f:
        f.write(raw)

    if not complete:
        _write_manifest(out_dir, manifest)
        raise SystemExit("R6 incompleta: ACTIVE precedente resta intatto")

    if d1:
        try:
            verification = d1.publish_generation(
                generation,
                target_day,
                assembled,
                manifest,
                raw,
                expected_stations=SETTINGS.expected_stations,
            )
            manifest["publication"] = "ACTIVE"
            manifest["d1_verification"] = verification
        except Exception as exc:
            manifest["publication"] = "BLOCKED_D1"
            manifest["d1_error"] = str(exc)
            _write_manifest(out_dir, manifest)
            raise

    _write_manifest(out_dir, manifest)
    return 0


def _write_manifest(out_dir: Path, manifest: dict) -> None:
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_diag(out_dir, manifest)


def _write_diag(out_dir: Path, payload: dict) -> None:
    (out_dir / "diagnostics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
