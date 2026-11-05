# R6381-COPERNICUS-FIX-2026-10-04
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
from .cache_policy import reusable_component, reusable_history, reusable_history_payload
from .copernicus import fetch_copernicus_bundle
from .config import SETTINGS
from .daily import rome_day, validate_day
from . import forecast_readiness as readiness
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
    p.add_argument("--daily", action="store_true", help="Una pubblicazione completa per giornata Europe/Rome")
    p.add_argument("--target-day", default="", help="Giornata richiesta YYYY-MM-DD; richieste scadute non scaricano dati")
    p.add_argument("--request-id", default="", help="Identificativo diagnostico dell'innesco")
    p.add_argument("--force-rebuild", action="store_true", help="Manutenzione esplicita: consenti una nuova generazione oggi")
    p.add_argument("--provider-readiness-guard", action="store_true",
                   help="Controllo preliminare forecast; attesa limitata per il solo ultimo giorno non disponibile")
    args = p.parse_args()
    if args.force_rebuild and not args.daily:
        p.error("--force-rebuild richiede --daily")
    if args.provider_readiness_guard and not args.daily:
        p.error("--provider-readiness-guard richiede --daily")
    try:
        target_day = validate_day(args.target_day) if args.target_day else rome_day()
    except ValueError as exc:
        p.error(str(exc))
    if len(args.request_id) > 160:
        p.error("request-id troppo lungo")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if target_day != rome_day():
        _daily_receipt(out_dir, "EXPIRED_REQUEST", target_day, args.request_id)
        return 0
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

    if args.daily:
        if not d1:
            p.error("--daily richiede D1 persistente")
        active = d1.daily_active_manifest(target_day, SETTINGS.expected_stations)
        if active and not args.force_rebuild:
            _daily_receipt(out_dir, "DAILY_ALREADY_READY", target_day, args.request_id, active)
            return 0

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

    local_now = datetime.now(ZoneInfo(SETTINGS.timezone))
    slot = (local_now.hour // 6) * 6
    # All automatic retries of the same Rome day reuse successful provider batches,
    # even when the retry crosses a six-hour boundary. Force remains maintenance.
    cycle_key = f"{target_day}T{0 if args.daily and not args.force_rebuild else slot:02d}-{args.mode}"

    component_cache: dict[str, dict[str, dict]] = {}
    errors: dict[str, list[str]] = {}

    def persist_batch(component: str, rows: list[dict]):
        if d1:
            d1.persist_component_batch(component, rows)

    forecast_cache = None
    if args.provider_readiness_guard and args.mode == "full":
        status, forecast_cache = _forecast_preflight(
            http, d1, stations, cycle_key, target_day, out_dir, forced=args.force_rebuild)
        if status:
            report = json.loads((out_dir / "forecast-readiness.json").read_text(encoding="utf-8"))
            details = {key: report[key] for key in
                       ("probe_code", "issues", "deadline_utc", "checked_at", "errors")}
            _daily_receipt(out_dir, status, target_day, args.request_id, readiness_details=details)
            if status in {"WAITING_FORECAST_PROVIDER", "EXPIRED_REQUEST"}:
                return 0
            raise SystemExit("R6587 forecast preliminare incompleto: leggere forecast-readiness.json; ACTIVE invariato")

    official_rain, sir_health = fetch_official_rain(
        http,
        allow_browser=True,
        expected_codes={str(s["code"]).strip().upper() for s in stations},
    )
    if d1:
        d1.source_health("sir_rainfall", sir_health)

    rejected_forecasts = []
    for component in ["current", "forecast", "soil"]:
        existing = (forecast_cache if component == "forecast" and forecast_cache is not None
                    else d1.load_component_map(component, cycle_key) if d1 else {})
        if args.daily:
            by_code = {s["code"]: s for s in stations}
            if component == "forecast":
                rejected_forecasts.extend(
                    {**readiness.forecast_evidence(code, rec, target_day), "origin": "cache"}
                    for code, rec in existing.items()
                    if code not in by_code or not reusable_component(by_code[code], component, rec.get("payload"), target_day)
                )
                _write_forecast_rejections(out_dir, rejected_forecasts)
            existing = {
                code: rec for code, rec in existing.items()
                if code in by_code and reusable_component(by_code[code], component, rec.get("payload"), target_day)
            }
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
        if args.daily:
            invalid = [code for code, rec in recs.items()
                       if code not in by_code or not reusable_component(by_code[code], component, rec.get("payload"), target_day)]
            for code in invalid:
                record = recs.pop(code)
                errs.append(f"scientific_incomplete:{code}; componente da ritentare")
                if component == "forecast":
                    evidence = readiness.forecast_evidence(code, record, target_day)
                    if not evidence["issues"]:
                        evidence["issues"] = [{"reason": "aggregate_coverage_incomplete"}]
                    rejected_forecasts.append({**evidence, "origin": "network"})
                    print("[R6587] forecast rejected " + json.dumps(
                        {"code": code, "issues": evidence["issues"]}, ensure_ascii=False), flush=True)
            if component == "forecast":
                _write_forecast_rejections(out_dir, rejected_forecasts)
        component_cache[component] = {**existing, **recs}
        errors[component] = errs

    history_by_code: dict[str, list[dict]] = {}

    if args.mode == "bootstrap":
        existing = d1.load_component_map("history_bootstrap", cycle_key) if d1 else {}
        if args.daily:
            by_code = {s["code"]: s for s in stations}
            existing = {
                code: rec for code, rec in existing.items()
                if code in by_code and reusable_history_payload(by_code[code], rec.get("payload"),
                                                               target_day, official_rain.get(code))
            }
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
            if len(history_by_code.get(s["code"]) or []) < 88 or (
                args.daily and not reusable_history(s, history_by_code.get(s["code"]) or [], target_day, official_rain.get(s["code"]))
            )
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

    # R6.38.1 — Copernicus: invalida la cache vuota R6.38 e non memorizza payload vuoti.
    copernicus_cycle = f"{target_day}-copernicus-r6381"
    copernicus_cache: dict[str, dict] = d1.load_component_map("copernicus", copernicus_cycle) if d1 else {}
    cached_payloads = {
        code: (rec.get("payload") or {}) for code, rec in copernicus_cache.items()
        if isinstance(rec, dict) and (rec.get("payload") or {})
    }
    missing_stations = [s for s in stations if s["code"] not in cached_payloads]
    if missing_stations:
        fresh_payloads, copernicus_health = fetch_copernicus_bundle(missing_stations)
    else:
        fresh_payloads = {}
        copernicus_health = {"status":"CACHE","configured":True,"reachable":True,"acquired":len(cached_payloads),
            "expected":SETTINGS.expected_stations,"complete":len(cached_payloads)==SETTINGS.expected_stations,
            "last_success_at":None,"last_error":None,"reference_time":target_day}
    fetched_at = datetime.now(timezone.utc).isoformat()
    fresh_rows=[]
    for station in missing_stations:
        code=station["code"]; payload=fresh_payloads.get(code) or {}
        if not payload:
            continue
        cached_payloads[code]=payload
        fresh_rows.append({"code":code,"component":"copernicus","fetched_at":fetched_at,"cycle_key":copernicus_cycle,"payload":payload})
    if d1 and fresh_rows:
        d1.persist_component_batch("copernicus", fresh_rows)
        stored=d1.load_component_map("copernicus",copernicus_cycle)
        copernicus_cache={code:rec for code,rec in stored.items() if isinstance(rec,dict) and (rec.get("payload") or {})}
    else:
        copernicus_cache={code:{"code":code,"component":"copernicus","fetched_at":fetched_at,"cycle_key":copernicus_cycle,"payload":payload}
                          for code,payload in cached_payloads.items() if payload}
    total_payloads=[(rec.get("payload") or {}) for rec in copernicus_cache.values() if isinstance(rec,dict) and (rec.get("payload") or {})]
    copernicus_health.update({
        "acquired":len(total_payloads),"expected":SETTINGS.expected_stations,
        "complete":len(total_payloads)==SETTINGS.expected_stations,
        "ssm_acquired":sum(p.get("satellite_surface_soil_moisture_pct") is not None for p in total_payloads),
        "swi_acquired":sum(p.get("satellite_soil_water_index_pct") is not None for p in total_payloads),
        "soil_acquired":sum(p.get("satellite_surface_soil_moisture_pct") is not None or p.get("satellite_soil_water_index_pct") is not None for p in total_payloads),
        "vegetation_acquired":sum(p.get("ndvi_current") is not None or p.get("ndmi_current") is not None or p.get("evi_current") is not None for p in total_payloads),
        "cache_cycle":copernicus_cycle,
    })
    if d1:
        d1.source_health("copernicus",copernicus_health)

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
    if target_day != rome_day():
        _daily_receipt(out_dir, "EXPIRED_REQUEST", target_day, args.request_id)
        return 0
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
            if target_day != rome_day():
                _daily_receipt(out_dir, "EXPIRED_REQUEST", target_day, args.request_id)
                return 0
            if args.daily and not args.force_rebuild:
                active = d1.daily_active_manifest(target_day, SETTINGS.expected_stations)
                if active:
                    _daily_receipt(out_dir, "DAILY_ALREADY_READY", target_day, args.request_id, active)
                    return 0
            verification = d1.publish_generation(
                generation,
                target_day,
                assembled,
                manifest,
                raw,
                expected_stations=SETTINGS.expected_stations,
                required_rome_day=target_day if args.daily else None,
            )
            manifest["publication"] = "ACTIVE"
            manifest["d1_verification"] = verification
        except Exception as exc:
            manifest["publication"] = "BLOCKED_D1"
            manifest["d1_error"] = str(exc)
            _write_manifest(out_dir, manifest)
            raise

    _write_manifest(out_dir, manifest)
    if args.daily:
        _daily_receipt(out_dir, "DAILY_PUBLISHED", target_day, args.request_id, manifest)
    return 0


def _forecast_preflight(http, d1, stations: list[dict], cycle_key: str,
                        target_day: str, out_dir: Path, *, forced: bool) -> tuple[str | None, dict]:
    """One point can prove unreadiness, never certify all 418 as complete."""
    existing = d1.load_component_map("forecast", cycle_key)
    probe = min(stations, key=lambda station: station["code"])
    code = probe["code"]
    record = existing.get(code)
    errors = []
    from_cache = bool(record and not readiness.forecast_issues(record.get("payload"), target_day)
                      and reusable_component(probe, "forecast", record.get("payload"), target_day))
    if not from_cache:
        records, errors = fetch_component(http, [probe], component="forecast",
            batch_size=1, pause_s=0, cycle_key=cycle_key)
        record = records.get(code)
        if (record and not errors and not readiness.forecast_issues(record.get("payload"), target_day)
                and reusable_component(probe, "forecast", record.get("payload"), target_day)):
            # Reuse this successful request in the normal full acquisition.
            d1.persist_component_batch("forecast", [record])
            existing[code] = record
    now = readiness.utc_now()
    evidence = readiness.forecast_evidence(code, record or {}, target_day)
    issues = evidence["issues"]
    valid = bool(record and not errors and not issues
                 and reusable_component(probe, "forecast", record.get("payload"), target_day))
    if not valid and not issues:
        issues = [{"reason": "aggregate_coverage_incomplete"}]
    status = (None if valid else "WAITING_FORECAST_PROVIDER"
              if record and not errors and readiness.tail_not_ready(issues)
              and readiness.may_wait(target_day, now, forced=forced)
              else "BLOCKED_FORECAST_PREFLIGHT")
    if now.astimezone(ZoneInfo("Europe/Rome")).date().isoformat() != target_day:
        status = "EXPIRED_REQUEST"
    report = {"status": status or "PROBE_READY_FULL_VALIDATION_REQUIRED", "probe_code": code,
              "target_day": target_day, "checked_at": now.isoformat(),
              "deadline_utc": readiness.deadline_utc(target_day), "from_cache": from_cache,
              "issues": issues, "errors": errors, "evidence": evidence,
              "published": False, "note": "Probe non certifica le altre centraline; ACTIVE non modificato."}
    (out_dir / "forecast-readiness.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("[R6587] " + str(report["status"]) + " probe=" + code
          + " issues=" + json.dumps(issues, ensure_ascii=False), flush=True)
    return status, existing


def _write_forecast_rejections(out_dir: Path, records: list[dict]) -> None:
    if records:
        (out_dir / "forecast-rejections.json").write_text(
            json.dumps({"rejections": records}, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _daily_receipt(out_dir: Path, status: str, target_day: str, request_id: str,
                   active: dict | None = None, *, readiness_details: dict | None = None) -> None:
    receipt = {
        "status": status, "target_day": target_day, "request_id": request_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "generation": (active or {}).get("generation"),
    }
    if readiness_details is not None:
        receipt["forecast_readiness"] = readiness_details
    (out_dir / "daily-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    if status != "DAILY_PUBLISHED":
        _write_diag(out_dir, receipt)
    print(f"[R6584] {status} day={target_day} generation={receipt['generation'] or '-'}", flush=True)


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
