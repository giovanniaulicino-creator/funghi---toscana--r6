# R6382A-SENTINEL-MICRO-PROBE-2026-10-04
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json, math, os, time
from pathlib import Path
import requests
from .config import SETTINGS
from .d1 import D1

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
STATS_URL = "https://sh.dataspace.copernicus.eu/statistics/v1"

EVALSCRIPT = r"""
//VERSION=3
function setup() {
  return {
    input: [{bands:["B02","B04","B08","B11","SCL","dataMask"]}],
    output: [
      {id:"indices",bands:[{name:"NDVI"},{name:"NDMI"},{name:"EVI"}],sampleType:"FLOAT32"},
      {id:"dataMask",bands:1}
    ]
  };
}
function evaluatePixel(s) {
  var bad=[0,1,3,8,9,10,11].indexOf(s.SCL)>=0;
  var valid=s.dataMask && !bad ? 1 : 0;
  var ndvi=(s.B08-s.B04)/(s.B08+s.B04+1e-6);
  var ndmi=(s.B08-s.B11)/(s.B08+s.B11+1e-6);
  var evi=2.5*(s.B08-s.B04)/(s.B08+6*s.B04-7.5*s.B02+1.0+1e-6);
  return {indices:[ndvi,ndmi,evi],dataMask:[valid]};
}
"""

def iso_z(v):
    return v.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def get_token(cid,sec):
    r=requests.post(TOKEN_URL,data={"grant_type":"client_credentials","client_id":cid,"client_secret":sec},timeout=30)
    if r.status_code>=400:
        raise RuntimeError(f"OAuth HTTP {r.status_code}: {r.text[:1500]}")
    t=(r.json() or {}).get("access_token")
    if not t:
        raise RuntimeError("OAuth senza access_token")
    return str(t)

def bbox_around(lat,lon,radius_m=250.0):
    dlat=radius_m/111320.0
    dlon=radius_m/(111320.0*max(0.25,math.cos(math.radians(lat))))
    return [lon-dlon,lat-dlat,lon+dlon,lat+dlat]

def station_request(tok,station):
    lat=float(station["lat"]); lon=float(station["lon"])
    end=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    start=end-timedelta(days=30)
    body={
      "input":{
        "bounds":{"bbox":bbox_around(lat,lon),"properties":{"crs":"http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
        "data":[{"type":"sentinel-2-l2a","dataFilter":{"maxCloudCoverage":90,"mosaickingOrder":"leastCC"}}]
      },
      "aggregation":{
        "timeRange":{"from":iso_z(start),"to":iso_z(end)},
        "aggregationInterval":{"of":"P30D"},
        "width":16,
        "height":16,
        "evalscript":EVALSCRIPT
      }
    }
    for attempt in range(1,4):
        r=requests.post(
            STATS_URL,
            headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json","Accept":"application/json"},
            json=body,
            timeout=45
        )
        if r.status_code==429 or 500<=r.status_code<=599:
            if attempt<3:
                time.sleep(4*attempt)
                continue
        if r.status_code>=400:
            return {"ok":False,"http_status":r.status_code,"response_text":r.text[:5000],"request_body":body}
        payload=r.json() or {}
        bands=((((payload.get("data") or [{}])[-1].get("outputs") or {}).get("indices") or {}).get("bands") or {})
        vals={name:(((bands.get(name) or {}).get("stats") or {}).get("mean")) for name in ("NDVI","NDMI","EVI")}
        valid=any(v is not None for v in vals.values())
        return {"ok":valid,"http_status":r.status_code,"values":vals,"bands_keys":list(bands.keys()),"payload":None if valid else payload}
    return {"ok":False,"http_status":None,"response_text":"retry esauriti","request_body":body}

def main():
    out=Path("out"); out.mkdir(exist_ok=True)
    cid=(os.getenv("COPERNICUS_CLIENT_ID") or "").strip()
    sec=(os.getenv("COPERNICUS_CLIENT_SECRET") or "").strip()
    if not cid or not sec:
        raise RuntimeError("Secrets Copernicus assenti")

    d1=D1(SETTINGS.cf_account_id,SETTINGS.cf_d1_database_id,SETTINGS.cf_api_token,max_retries=SETTINGS.max_retries)
    stations=d1.load_catalog()
    stations=[s for s in stations if s.get("code") and s.get("lat") is not None and s.get("lon") is not None]
    stations=sorted(stations,key=lambda s:str(s.get("code")))
    if len(stations)<5:
        raise RuntimeError(f"Catalogo D1 insufficiente: {len(stations)}")

    idx=[0,len(stations)//4,len(stations)//2,(3*len(stations))//4,len(stations)-1]
    selected=[stations[i] for i in idx]
    tok=get_token(cid,sec)
    results=[]

    for i,s in enumerate(selected,1):
        print(f"[MICRO] {i}/5 {s['code']} lat={s['lat']} lon={s['lon']}",flush=True)
        res=station_request(tok,s)
        print(f"[MICRO] HTTP={res.get('http_status')} ok={res.get('ok')} values={res.get('values')}",flush=True)
        if not res.get("ok") and res.get("response_text"):
            print("[MICRO] ERRORE RAW:",res["response_text"][:1600],flush=True)
        results.append({"station":{"code":s["code"],"lat":s["lat"],"lon":s["lon"]},"result":res})

    valid=sum(1 for x in results if x["result"].get("ok"))
    payload={"probe":"R6382A-SENTINEL-MICRO","statistics_endpoint":STATS_URL,"sample_count":len(results),"valid_count":valid,"results":results}
    (out/"sentinel_micro_probe.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"[MICRO] RISULTATO validi={valid}/{len(results)}",flush=True)
    print("[OK] SENTINEL MICRO PROBE SUPERATO" if valid>=3 else "[NO] SENTINEL MICRO PROBE NON SUPERATO",flush=True)
    return 0 if valid>=3 else 2

if __name__=="__main__":
    raise SystemExit(main())
