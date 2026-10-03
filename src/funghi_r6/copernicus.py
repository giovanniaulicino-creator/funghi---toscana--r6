from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
import math
import os
import time
from typing import Any

import requests
from PIL import Image

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
CATALOG_URL = "https://sh.dataspace.copernicus.eu/catalog/v1/search"
STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"

# Official CLMS BYOC collections verified against Copernicus Data Space docs.
SSM_COLLECTION = "df9e9783-f580-433a-b798-3acd2760b94e"
SWI_COLLECTION = "bd02588b-7236-4b1e-9480-aeae7dce3c7a"

SSM_EVALSCRIPT = r"""
//VERSION=3
function setup() {
  return {input:[{bands:["SSM","SSM_NOISE","dataMask"]}],output:{bands:3,sampleType:"UINT8"}};
}
function evaluatePixel(s) {
  if (!s.dataMask) return [255,255,0];
  return [s.SSM,s.SSM_NOISE,1];
}
"""

SWI_EVALSCRIPT = r"""
//VERSION=3
function setup() {
  return {input:[{bands:["SWI040","QFLAG040","dataMask"]}],output:{bands:3,sampleType:"UINT8"}};
}
function evaluatePixel(s) {
  if (!s.dataMask) return [255,255,0];
  return [s.SWI040,s.QFLAG040,1];
}
"""

S2_INDEX_EVALSCRIPT = r"""
//VERSION=3
function setup() {
  return {
    input:[{bands:["B02","B04","B08","B11","SCL","dataMask"]}],
    output:[
      {id:"indices",bands:[{name:"NDVI"},{name:"NDMI"},{name:"EVI"}],sampleType:"FLOAT32"},
      {id:"dataMask",bands:1,sampleType:"UINT8"}
    ]
  };
}
function evaluatePixel(s) {
  var bad = [0,1,3,8,9,10,11].indexOf(s.SCL) >= 0;
  var valid = s.dataMask && !bad ? 1 : 0;
  var ndvi = (s.B08-s.B04)/(s.B08+s.B04+1e-6);
  var ndmi = (s.B08-s.B11)/(s.B08+s.B11+1e-6);
  var evi = 2.5*(s.B08-s.B04)/(s.B08+6*s.B04-7.5*s.B02+1.0+1e-6);
  return {indices:[ndvi,ndmi,evi],dataMask:[valid]};
}
"""


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _station_bbox(stations: list[dict[str, Any]]) -> list[float]:
    lats = [float(s["lat"]) for s in stations if s.get("lat") is not None]
    lons = [float(s["lon"]) for s in stations if s.get("lon") is not None]
    if not lats or not lons:
        raise ValueError("Nessuna coordinata stazione valida per Copernicus")
    pad = 0.06
    return [min(lons)-pad,min(lats)-pad,max(lons)+pad,max(lats)+pad]


def _grid_shape(bbox: list[float]) -> tuple[int,int]:
    west,south,east,north=bbox
    mid_lat=math.radians((south+north)*0.5)
    width=int(math.ceil(max(0.01,east-west)*111.32*max(0.25,math.cos(mid_lat))))
    height=int(math.ceil(max(0.01,north-south)*111.32))
    return max(96,min(512,width)),max(96,min(512,height))


def _token(session: requests.Session, client_id: str, client_secret: str, retries: int=4) -> str:
    last=None
    for attempt in range(1,retries+1):
        try:
            r=session.post(TOKEN_URL,data={"grant_type":"client_credentials","client_id":client_id,"client_secret":client_secret},timeout=30)
            if r.status_code==429 or 500<=r.status_code<=599:
                raise RuntimeError(f"token HTTP {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            token=(r.json() or {}).get("access_token")
            if not token:
                raise RuntimeError("access_token assente")
            return str(token)
        except Exception as exc:
            last=exc
            if attempt<retries:
                time.sleep(min(8.0,0.8*2**(attempt-1)))
    raise RuntimeError(f"OAuth Copernicus non riuscito: {last}")


def _latest_date(session: requests.Session, token: str, collection_id: str, bbox: list[float], lookback_days: int=12) -> str|None:
    end=datetime.now(timezone.utc); start=end-timedelta(days=lookback_days)
    r=session.post(CATALOG_URL,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},json={
        "collections":[f"byoc-{collection_id}"],"bbox":bbox,"datetime":f"{_iso_z(start)}/{_iso_z(end)}","limit":100,
    },timeout=45)
    r.raise_for_status()
    dates=[str(((f or {}).get("properties") or {}).get("datetime")) for f in ((r.json() or {}).get("features") or []) if ((f or {}).get("properties") or {}).get("datetime")]
    return max(dates) if dates else None


def _process_rgb(session: requests.Session, token: str, *, collection_id: str, bbox: list[float], width: int, height: int, observed_at: str, evalscript: str) -> Image.Image:
    observed=datetime.fromisoformat(observed_at.replace("Z","+00:00")).astimezone(timezone.utc)
    start=observed.replace(hour=0,minute=0,second=0,microsecond=0); end=start+timedelta(days=1)
    body={
      "input":{"bounds":{"bbox":bbox,"properties":{"crs":"http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},"data":[{
        "type":f"byoc-{collection_id}","dataFilter":{"timeRange":{"from":_iso_z(start),"to":_iso_z(end)},"mosaickingOrder":"mostRecent"},
        "processing":{"upsampling":"NEAREST","downsampling":"NEAREST"}}]},
      "output":{"width":width,"height":height,"responses":[{"identifier":"default","format":{"type":"image/tiff"}}]},
      "evalscript":evalscript,
    }
    r=session.post(PROCESS_URL,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json","Accept":"image/tiff"},json=body,timeout=120)
    r.raise_for_status()
    img=Image.open(BytesIO(r.content)); img.load()
    return img.convert("RGB")


def _sample_rgb(image: Image.Image, bbox: list[float], lat: float, lon: float) -> tuple[int,int,int]|None:
    west,south,east,north=bbox
    if lon<west or lon>east or lat<south or lat>north:
        return None
    width,height=image.size
    if width<=1 or height<=1:
        return None
    x=int(round((lon-west)/(east-west)*(width-1))); y=int(round((north-lat)/(north-south)*(height-1)))
    x=max(0,min(width-1,x)); y=max(0,min(height-1,y)); px=image.getpixel((x,y))
    if not isinstance(px,tuple) or len(px)<3:
        return None
    return int(px[0]),int(px[1]),int(px[2])


def _scaled_percent(raw: int|None, mask: int|None) -> float|None:
    if raw is None or mask is None or mask<=0 or raw==255:
        return None
    # Official CLMS metadata: UINT8 scaling 1/2, physical range 0..100%.
    return round(max(0.0,min(100.0,float(raw)*0.5)),1)


def _bbox_around(lat: float, lon: float, radius_m: float=120.0) -> list[float]:
    dlat=radius_m/111320.0
    dlon=radius_m/(111320.0*max(0.25,math.cos(math.radians(lat))))
    return [lon-dlon,lat-dlat,lon+dlon,lat+dlat]


def _stats_mean(stats_payload: dict[str,Any], band: str) -> tuple[float|None,float|None]:
    try:
        entries=stats_payload.get("data") or []
        if not entries:
            return None,None
        bands=(((entries[-1] or {}).get("outputs") or {}).get("indices") or {}).get("bands") or {}
        st=(bands.get(band) or {}).get("stats") or {}
        mean=st.get("mean"); sample=st.get("sampleCount"); nodata=st.get("noDataCount")
        value=float(mean) if mean is not None and math.isfinite(float(mean)) else None
        quality=None
        if sample is not None and float(sample)>0:
            quality=max(0.0,min(100.0,(float(sample)-float(nodata or 0))/float(sample)*100.0))
        return value,quality
    except Exception:
        return None,None


def _sentinel2_station(token: str, station: dict[str,Any], *, days: int=12, retries: int=3) -> tuple[str,dict[str,Any],str|None]:
    code=str(station.get("code") or ""); lat=float(station["lat"]); lon=float(station["lon"])
    end=datetime.now(timezone.utc); start=end-timedelta(days=days)
    body={
      "input":{"bounds":{"bbox":_bbox_around(lat,lon),"properties":{"crs":"http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},"data":[{
        "type":"sentinel-2-l2a","dataFilter":{"timeRange":{"from":_iso_z(start),"to":_iso_z(end)},"maxCloudCoverage":80,"mosaickingOrder":"leastCC"}}]},
      "aggregation":{"timeRange":{"from":_iso_z(start),"to":_iso_z(end)},"aggregationInterval":{"of":f"P{days}D"},"resx":20,"resy":20,"evalscript":S2_INDEX_EVALSCRIPT},
    }
    last=None
    for attempt in range(1,retries+1):
        try:
            r=requests.post(STATS_URL,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},json=body,timeout=60)
            if r.status_code==429 or 500<=r.status_code<=599:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:250]}")
            r.raise_for_status(); payload=r.json() or {}
            ndvi,q1=_stats_mean(payload,"NDVI"); ndmi,q2=_stats_mean(payload,"NDMI"); evi,q3=_stats_mean(payload,"EVI")
            qs=[q for q in (q1,q2,q3) if q is not None]
            out={
              "ndvi_current":round(ndvi,4) if ndvi is not None else None,
              "ndmi_current":round(ndmi,4) if ndmi is not None else None,
              "evi_current":round(evi,4) if evi is not None else None,
              "sentinel_indices_quality_pct":round(min(qs),1) if qs else None,
              "vegetation_indices_source_kind":"copernicus-sentinel-2-l2a-statistical",
              "vegetation_indices_spatial_role":"regional-anchor-at-meteo-station",
              "vegetation_indices_window_start":_iso_z(start),"vegetation_indices_window_end":_iso_z(end),
            }
            if any(out[k] is not None for k in ("ndvi_current","ndmi_current","evi_current")):
                return code,out,None
            return code,{},"nessun pixel Sentinel-2 valido dopo cloud mask"
        except Exception as exc:
            last=exc
            if attempt<retries:
                time.sleep(min(5.0,0.8*2**(attempt-1)))
    return code,{},str(last)


def fetch_copernicus_bundle(stations: list[dict[str,Any]], *, client_id: str|None=None, client_secret: str|None=None) -> tuple[dict[str,dict[str,Any]],dict[str,Any]]:
    client_id=(client_id or os.getenv("COPERNICUS_CLIENT_ID") or "").strip()
    client_secret=(client_secret or os.getenv("COPERNICUS_CLIENT_SECRET") or "").strip()
    expected=len(stations)
    if not client_id or not client_secret:
        return {}, {"status":"UNCONFIGURED","configured":False,"reachable":False,"acquired":0,"expected":expected,"complete":False,"last_success_at":None,"last_error":None,"reference_time":None,"note":"Configurare COPERNICUS_CLIENT_ID/SECRET. Fonte fail-soft: ACTIVE R6 non viene bloccato."}
    fetched_at=datetime.now(timezone.utc).isoformat(); session=requests.Session(); errors=[]; out={}
    try:
        token=_token(session,client_id,client_secret); bbox=_station_bbox(stations); width,height=_grid_shape(bbox)
        ssm_date=swi_date=None; ssm_img=swi_img=None
        try:
            ssm_date=_latest_date(session,token,SSM_COLLECTION,bbox,12)
            if ssm_date:
                ssm_img=_process_rgb(session,token,collection_id=SSM_COLLECTION,bbox=bbox,width=width,height=height,observed_at=ssm_date,evalscript=SSM_EVALSCRIPT)
            else:
                errors.append("SSM: nessuna acquisizione recente")
        except Exception as exc:
            errors.append(f"SSM: {exc}")
        try:
            swi_date=_latest_date(session,token,SWI_COLLECTION,bbox,12)
            if swi_date:
                swi_img=_process_rgb(session,token,collection_id=SWI_COLLECTION,bbox=bbox,width=width,height=height,observed_at=swi_date,evalscript=SWI_EVALSCRIPT)
            else:
                errors.append("SWI: nessuna acquisizione recente")
        except Exception as exc:
            errors.append(f"SWI: {exc}")

        for station in stations:
            code=str(station.get("code") or ""); lat=float(station["lat"]); lon=float(station["lon"])
            rec={"copernicus_soil_source_kind":"copernicus-clms-ssm1km+swi040-1km","copernicus_soil_fetched_at":fetched_at,"copernicus_ssm_observed_at":ssm_date,"copernicus_swi_observed_at":swi_date}
            if ssm_img is not None:
                px=_sample_rgb(ssm_img,bbox,lat,lon)
                if px:
                    rec["satellite_surface_soil_moisture_pct"]=_scaled_percent(px[0],px[2]); rec["copernicus_ssm_noise_pct"]=_scaled_percent(px[1],px[2])
            if swi_img is not None:
                px=_sample_rgb(swi_img,bbox,lat,lon)
                if px:
                    rec["satellite_soil_water_index_pct"]=_scaled_percent(px[0],px[2]); rec["copernicus_swi_quality_pct"]=_scaled_percent(px[1],px[2])
            if any(rec.get(k) is not None for k in ("satellite_surface_soil_moisture_pct","satellite_soil_water_index_pct")):
                out[code]=rec

        veg_errors=[]
        workers=max(1,min(12,int(os.getenv("COPERNICUS_VEG_WORKERS") or "6")))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures=[pool.submit(_sentinel2_station,token,s,days=12) for s in stations]
            for fut in as_completed(futures):
                code,rec,err=fut.result()
                if rec:
                    out.setdefault(code,{}).update(rec)
                if err:
                    veg_errors.append(f"{code}: {err}")
        errors.extend(veg_errors[:30])
        soil_acquired=sum(1 for r in out.values() if r.get("satellite_surface_soil_moisture_pct") is not None or r.get("satellite_soil_water_index_pct") is not None)
        veg_acquired=sum(1 for r in out.values() if r.get("ndmi_current") is not None or r.get("evi_current") is not None or r.get("ndvi_current") is not None)
        return out, {
          "status":"OK" if out else "NO_VALID_PIXELS","configured":True,"reachable":ssm_img is not None or swi_img is not None or veg_acquired>0,
          "acquired":len(out),"expected":expected,"complete":len(out)==expected,"soil_acquired":soil_acquired,"vegetation_acquired":veg_acquired,
          "last_success_at":fetched_at if out else None,"last_error":" | ".join(errors)[:2000] if errors else None,
          "reference_time":max([x for x in (ssm_date,swi_date) if x],default=None),"bbox":bbox,"grid":{"width":width,"height":height},
          "products":{"ssm":{"collection":SSM_COLLECTION,"observed_at":ssm_date},"swi040":{"collection":SWI_COLLECTION,"observed_at":swi_date},"sentinel2_indices":{"type":"sentinel-2-l2a","window_days":12}},
          "note":"Copernicus è evidenza indipendente fail-soft. SSM/SWI 1 km; NDVI/NDMI/EVI da Sentinel-2 L2A con cloud mask su finestra 12 giorni."
        }
    except Exception as exc:
        return {}, {"status":"ERROR","configured":True,"reachable":False,"acquired":0,"expected":expected,"complete":False,"last_success_at":None,"last_error":str(exc)[:2000],"reference_time":None}
    finally:
        session.close()
