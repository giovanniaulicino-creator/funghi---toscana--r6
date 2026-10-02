from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import SETTINGS
from .catalog import fetch_catalog
from .http import HttpClient
from .sir import fetch_official_rain


def main() -> int:
    p = argparse.ArgumentParser(description="Audit mirato SIR/CFR rainfall per Funghi Toscana R6")
    p.add_argument("--out", default="out/sir-audit.json")
    p.add_argument("--minimum", type=int, default=100)
    args = p.parse_args()

    http = HttpClient(max_retries=SETTINGS.max_retries)

    stations, catalog_health = fetch_catalog(
        http,
        expected=SETTINGS.expected_stations,
        persisted=None,
    )
    if len(stations) != SETTINGS.expected_stations:
        raise SystemExit(
            f"Catalogo strutturale non disponibile: {len(stations)}/{SETTINGS.expected_stations}"
        )

    expected_codes = {str(s["code"]).strip().upper() for s in stations}

    data, health = fetch_official_rain(
        http,
        minimum_records=args.minimum,
        allow_browser=True,
        expected_codes=expected_codes,
    )

    missing_codes = set(health.get("missing_codes") or [])
    incomplete_codes = set(health.get("incomplete_window_codes") or [])

    missing_stations = [
        {
            "code": s.get("code"),
            "name": s.get("name"),
            "municipality": s.get("municipality"),
            "province": s.get("province"),
            "altitude_m": s.get("altitude_m"),
        }
        for s in stations
        if s.get("code") in missing_codes
    ]

    incomplete_window_stations = [
        {
            "code": code,
            "name": data.get(code, {}).get("name"),
            "municipality": data.get(code, {}).get("municipality"),
            "province": data.get(code, {}).get("province"),
            "rain_5d_mm": data.get(code, {}).get("rain_5d_mm"),
            "rain_7d_mm": data.get(code, {}).get("rain_7d_mm"),
            "rain_15d_mm": data.get(code, {}).get("rain_15d_mm"),
            "rain_30d_mm": data.get(code, {}).get("rain_30d_mm"),
        }
        for code in sorted(incomplete_codes)
    ]

    payload = {
        "ok": bool(health.get("reachable")) and bool(health.get("acquisition_usable")),
        "catalog": catalog_health,
        "registry_expected": len(stations),
        "sir_acquired": len(data),
        "matched_official": health.get("matched_catalog"),
        "missing_official": health.get("missing_catalog"),
        "field_coverage": health.get("field_coverage"),
        "complete_windows_5_7_15_30": health.get("complete_windows_5_7_15_30"),
        "fallback_needed": health.get("fallback_needed"),
        "missing_stations": missing_stations,
        "incomplete_window_stations": incomplete_window_stations,
        "extra_official_codes": health.get("extra_codes") or [],
        "health": health,
        "sample": list(data.values())[:5],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(f"SIR audit incompleto: {len(data)}/{args.minimum} record minimi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
