from __future__ import annotations
import argparse,json
from pathlib import Path
from .catalog import fetch_catalog
from .config import SETTINGS
from .copernicus import fetch_copernicus_bundle
from .d1 import D1
from .http import HttpClient

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--out",default="out");a=ap.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    http=HttpClient(max_retries=SETTINGS.max_retries)
    d1=D1(SETTINGS.cf_account_id,SETTINGS.cf_d1_database_id,SETTINGS.cf_api_token,max_retries=SETTINGS.max_retries) if SETTINGS.d1_enabled else None
    stations,_=fetch_catalog(http,expected=SETTINGS.expected_stations,persisted=(d1.load_catalog() if d1 else []))
    data,health=fetch_copernicus_bundle(stations)
    cv={k:sum((r or {}).get(f) is not None for r in data.values()) for k,f in {
      "SSM":"satellite_surface_soil_moisture_pct","SWI":"satellite_soil_water_index_pct",
      "NDVI":"ndvi_current","NDMI":"ndmi_current","EVI":"evi_current"}.items()}
    payload={"station_count":len(stations),"health":health,"coverage":cv}
    (out/"copernicus_probe.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(" | ".join(f"{k} {v}/{len(stations)}" for k,v in cv.items()))
    print("error_counts:",health.get("error_counts"))
    ok=(cv["SSM"]+cv["SWI"]>0 and cv["NDVI"]+cv["NDMI"]+cv["EVI"]>0 and int((health.get("error_counts") or {}).get("http_400") or 0)==0)
    print("[OK] COPERNICUS PROBE SUPERATO" if ok else "[NO] COPERNICUS PROBE NON SUPERATO")
    return 0 if ok else 2
if __name__=="__main__":raise SystemExit(main())
