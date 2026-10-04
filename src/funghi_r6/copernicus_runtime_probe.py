# R6382C-COPERNICUS-RUNTIME-PROBE-2026-10-04
from __future__ import annotations

import json
from pathlib import Path

from .config import SETTINGS
from .copernicus import fetch_copernicus_bundle
from .d1 import D1


def sample_stations(stations, n=40):
    valid = [
        s for s in stations
        if s.get("code") and s.get("lat") is not None and s.get("lon") is not None
    ]
    valid.sort(key=lambda s: (float(s["lat"]), float(s["lon"]), str(s["code"])))
    if len(valid) <= n:
        return valid
    idxs = [round(i * (len(valid) - 1) / (n - 1)) for i in range(n)]
    return [valid[i] for i in idxs]


def main():
    out = Path("out")
    out.mkdir(parents=True, exist_ok=True)

    d1 = D1(
        SETTINGS.cf_account_id,
        SETTINGS.cf_d1_database_id,
        SETTINGS.cf_api_token,
        max_retries=SETTINGS.max_retries,
    )
    stations = d1.load_catalog()
    sample = sample_stations(stations, 40)
    if len(sample) < 30:
        raise RuntimeError(f"Campione insufficiente: {len(sample)}")

    print(f"[R6382C] campione runtime: {len(sample)} stazioni", flush=True)
    data, health = fetch_copernicus_bundle(sample)

    def count(field):
        return sum((data.get(str(s["code"])) or {}).get(field) is not None for s in sample)

    coverage = {
        "SSM": count("satellite_surface_soil_moisture_pct"),
        "SWI": count("satellite_soil_water_index_pct"),
        "NDVI": count("ndvi_current"),
        "NDMI": count("ndmi_current"),
        "EVI": count("evi_current"),
    }
    errors = health.get("error_counts") or {}

    payload = {
        "probe": "R6382C-COPERNICUS-RUNTIME",
        "sample_count": len(sample),
        "coverage": coverage,
        "health": health,
        "sample_codes": [str(s["code"]) for s in sample],
    }
    (out / "copernicus_runtime_probe.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(" | ".join(f"{k} {v}/{len(sample)}" for k, v in coverage.items()), flush=True)
    print("error_counts:", errors, flush=True)

    veg = min(coverage["NDVI"], coverage["NDMI"], coverage["EVI"])
    soil = max(coverage["SSM"], coverage["SWI"])
    bad400 = int(errors.get("http_400") or 0)

    ok = veg >= 36 and soil >= 25 and bad400 == 0
    if ok:
        print("[OK] R6382C COPERNICUS RUNTIME PROBE SUPERATO", flush=True)
        if coverage["SWI"] == 0:
            print("[ATTENZIONE] SWI ancora 0: lo verifichiamo separatamente prima del FULL.", flush=True)
        return 0

    print("[NO] R6382C COPERNICUS RUNTIME PROBE NON SUPERATO", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
