from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import gzip
import hashlib
import json
from pathlib import Path

from .assemble import assemble_station, coverage
from .catalog import fetch_catalog
from .config import SETTINGS
from .d1 import D1
from .http import HttpClient
from .open_meteo import fetch_component, daily_rows
from .sir import fetch_official_rain


def main() -> int:
    p = argparse.ArgumentParser(description="Funghi Toscana AI R6 Data Builder")
    p.add_argument("--mode", choices=["bootstrap", "full", "light"], default="full")
    p.add_argument("--out", default="out")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    http = HttpClient(max_retries=SETTINGS.max_retries)
    d1 = D1(SETTINGS.cf_account_id, SETTINGS.cf_d1_database_id, SETTINGS.cf_api_token) if SETTINGS.d1_enabled else None
    if d1:
        d1.init_schema(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

    persisted_catalog = d1.load_catalog() if d1 else []
    stations, catalog_health = fetch_catalog(http, expected=SETTINGS.expected_stations, persisted=persisted_catalog)
    if d1:
        d1.persist_catalog(stations)
        d1.source_health("sir_registry", catalog_health)
    if len(stations) != SETTINGS.expected_stations:
        _write_diag(out_dir, {"status": "BLOCKED", "reason": "station_catalog_count", "catalog": catalog_health})
        raise SystemExit(f"Catalogo strutturale {len(stations)}/{SETTINGS.expected_stations}: ACTIVE non pubblicabile")

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
            http, targets, component=component, batch_size=SETTINGS.batch_size,
            pause_s=SETTINGS.batch_pause_s, on_batch=persist_batch, cycle_key=cycle_key,
        )
        component_cache[component] = {**existing, **recs}
        errors[component] = errs

    # Bootstrap is the only operation allowed to request a 35-day historical window.
    history_by_code: dict[str, list[dict]] = {}
    if args.mode == "bootstrap":
        existing = d1.load_component_map("history_bootstrap", cycle_key) if d1 else {}
        targets = [s for s in stations if s["code"] not in existing]
        recs, errs = fetch_component(
            http, targets, component="history_bootstrap", batch_size=SETTINGS.batch_size,
            pause_s=SETTINGS.batch_pause_s, on_batch=persist_batch, cycle_key=cycle_key,
        )
        recs = {**existing, **recs}
        errors["history_bootstrap"] = errs
        for code, rec in recs.items():
            rows = daily_rows(code, rec["payload"])
            history_by_code[code] = rows
            if d1:
                d1.persist_daily(rows)

    # Normal daily operation appends only the tiny past_days=2 slice already returned by forecast.
    for code, rec in component_cache.get("forecast", {}).items():
        rows = [r for r in daily_rows(code, rec["payload"]) if r["day"] < target_day]
        if d1 and rows:
            d1.persist_daily(rows)
        if code not in history_by_code:
            history_by_code[code] = d1.load_history(code, 35) if d1 else rows

    assembled: list[dict] = []
    for station in stations:
        code = station["code"]
        assembled.append(assemble_station(
            station,
            official_rain=official_rain.get(code),
            current=(component_cache.get("current", {}).get(code) or {}).get("payload"),
            forecast=(component_cache.get("forecast", {}).get(code) or {}).get("payload"),
            soil=(component_cache.get("soil", {}).get(code) or {}).get("payload"),
            history=history_by_code.get(code) or [],
        ))

    cov = coverage(assembled)
    generated_at = datetime.now(timezone.utc).isoformat()
    generation = f"R6-{target_day}-{datetime.now(timezone.utc).strftime('%H%M%SZ')}"
    sources = {
        "sir_cfr": sir_health,
        "sir_registry": catalog_health,
        "open_meteo": {
            "reachable": any(len(component_cache.get(k, {})) for k in ["current", "forecast", "soil"]),
            "current": len(component_cache.get("current", {})),
            "forecast": len(component_cache.get("forecast", {})),
            "soil": len(component_cache.get("soil", {})),
            "errors": errors,
        },
        "radar": {"status": "SEPARATE_UNCHANGED", "worker": "https://funghi-toscana-radar.porcinitoscanaai.workers.dev"},
    }
    complete = all(cov[k] == SETTINGS.expected_stations for k in ["structural", "current", "rain_5_7_15_30", "history_30", "forecast_7", "forecast_15", "et0", "soil", "scientific_complete"])
    snapshot = {
        "meta": {
            "architecture": "r6-external-builder-d1-atomic",
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
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
        "publication": "ACTIVE" if complete and d1 else "ARTIFACT_ONLY" if complete else "BLOCKED_INCOMPLETE",
    }
    (out_dir / "snapshot.json").write_bytes(raw)
    with gzip.open(out_dir / "snapshot.json.gz", "wb", compresslevel=9) as f:
        f.write(raw)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_diag(out_dir, manifest)

    if complete and d1:
        d1.publish_generation(generation, target_day, assembled, manifest)
    elif not complete:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        raise SystemExit("R6 incompleta: ACTIVE precedente resta intatto")
    return 0


def _write_diag(out_dir: Path, payload: dict) -> None:
    (out_dir / "diagnostics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
