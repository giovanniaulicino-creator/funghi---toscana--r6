# R6381-COPERNICUS-FIX-2026-10-04
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
import math, os, threading, time
from typing import Any
import requests
from PIL import Image

TOKEN_URL="https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL="https://sh.dataspace.copernicus.eu/api/v1/process"
CATALOG_URL="https://sh.dataspace.copernicus.eu/catalog/v1/search"
STATS_URL="https://sh.dataspace.copernicus.eu/statistics/v1"
SSM_COLLECTION="df9e9783-f580-433a-b798-3acd2760b94e"
SWI_COLLECTION="bd02588b-7236-4b1e-9480-aeae7dce3c7a"

SSM_EVALSCRIPT=r"""
//VERSION=3
function setup(){return {input:[{bands:["SSM","SSM_NOISE","dataMask"]}],output:{bands:3,sampleType:"UINT8"}};}
function evaluatePixel(s){if(!s.dataMask)return [255,255,0];return [s.SSM,s.SSM_NOISE,1];}
"""
SWI_EVALSCRIPT=r"""
//VERSION=3
function setup(){return {input:[{bands:["SWI040","QFLAG040","dataMask"]}],output:{bands:3,sampleType:"UINT8"}};}
function evaluatePixel(s){if(!s.dataMask)return [255,255,0];return [s.SWI040,s.QFLAG040,1];}
"""
S2_INDEX_EVALSCRIPT=r"""
//VERSION=3
function setup(){return {input:[{bands:["B02","B04","B08","B11","SCL","dataMask"]}],output:[
{id:"indices",bands:[{name:"NDVI"},{name:"NDMI"},{name:"EVI"}],sampleType:"FLOAT32"},
{id:"dataMask",bands:1,sampleType:"UINT8"}]};}
function evaluatePixel(s){
 var bad=[0,1,3,8,9,10,11].indexOf(s.SCL)>=0;
 var valid=s.dataMask&&!bad?1:0;
 var ndvi=(s.B08-s.B04)/(s.B08+s.B04+1e-6);
 var ndmi=(s.B08-s.B11)/(s.B08+s.B11+1e-6);
 var evi=2.5*(s.B08-s.B04)/(s.B08+6*s.B04-7.5*s.B02+1.0+1e-6);
 return {indices:[ndvi,ndmi,evi],dataMask:[valid]};
}
"""

_rate_lock=threading.Lock()
_next_request_at=0.0

def _iso_z(v):
    return v.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def _station_bbox(stations):
    lats=[float(s["lat"]) for s in stations if s.get("lat") is not None]
    lons=[float(s["lon"]) for s in stations if s.get("lon") is not None]
    if not lats or not lons: raise ValueError("Nessuna coordinata stazione valida")
    pad=.06
    return [min(lons)-pad,min(lats)-pad,max(lons)+pad,max(lats)+pad]

def _grid_shape(b):
    w,s,e,n=b
    mid=math.radians((s+n)*.5)
    x=int(math.ceil(max(.01,e-w)*111.32*max(.25,math.cos(mid))))
    y=int(math.ceil(max(.01,n-s)*111.32))
    return max(96,min(512,x)),max(96,min(512,y))

def _token(sess,cid,secret,retries=4):
    last=None
    for a in range(1,retries+1):
        try:
            r=sess.post(TOKEN_URL,data={"grant_type":"client_credentials","client_id":cid,"client_secret":secret},timeout=30)
            if r.status_code>=400: raise RuntimeError(f"token HTTP {r.status_code}: {r.text[:800]}")
            t=(r.json() or {}).get("access_token")
            if not t: raise RuntimeError("access_token assente")
            return str(t)
        except Exception as e:
            last=e
            if a<retries: time.sleep(min(12,1.2*2**(a-1)))
    raise RuntimeError(f"OAuth fallito: {last}")

def _catalog_dates(sess,token,collection,bbox,lookback):
    end=datetime.now(timezone.utc); start=end-timedelta(days=lookback)
    r=sess.post(CATALOG_URL,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},
        json={"collections":[f"byoc-{collection}"],"bbox":bbox,"datetime":f"{_iso_z(start)}/{_iso_z(end)}","limit":100},timeout=45)
    if r.status_code>=400: raise RuntimeError(f"catalog HTTP {r.status_code}: {r.text[:1000]}")
    dates={str(((f or {}).get("properties") or {}).get("datetime")) for f in ((r.json() or {}).get("features") or [])
           if ((f or {}).get("properties") or {}).get("datetime")}
    return sorted(dates,reverse=True)

def _process_day(sess,token,collection,bbox,width,height,obs,script):
    d=datetime.fromisoformat(obs.replace("Z","+00:00")).astimezone(timezone.utc)
    start=d.replace(hour=0,minute=0,second=0,microsecond=0); end=start+timedelta(days=1)
    body={"input":{"bounds":{"bbox":bbox,"properties":{"crs":"http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
      "data":[{"type":f"byoc-{collection}","dataFilter":{"timeRange":{"from":_iso_z(start),"to":_iso_z(end)},"mosaickingOrder":"mostRecent"},
      "processing":{"upsampling":"NEAREST","downsampling":"NEAREST"}}]},
      "output":{"width":width,"height":height,"responses":[{"identifier":"default","format":{"type":"image/tiff"}}]},
      "evalscript":script}
    r=sess.post(PROCESS_URL,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json","Accept":"image/tiff"},json=body,timeout=120)
    if r.status_code>=400: raise RuntimeError(f"process HTTP {r.status_code}: {r.text[:1200]}")
    img=Image.open(BytesIO(r.content)); img.load(); return img.convert("RGB")

def _sample(img,bbox,lat,lon):
    w,s,e,n=bbox
    if lon<w or lon>e or lat<s or lat>n:return None
    W,H=img.size
    x=max(0,min(W-1,int(round((lon-w)/(e-w)*(W-1)))))
    y=max(0,min(H-1,int(round((n-lat)/(n-s)*(H-1)))))
    p=img.getpixel((x,y))
    return (int(p[0]),int(p[1]),int(p[2])) if isinstance(p,tuple) and len(p)>=3 else None

def _pct(raw,mask):
    if raw is None or mask is None or mask<=0 or raw==255:return None
    return round(max(0,min(100,float(raw)*.5)),1)

def _fill_clms(sess,token,stations,bbox,width,height,collection,script,value_field,quality_field,obs_field,lookback,max_dates,label):
    dates=_catalog_dates(sess,token,collection,bbox,lookback)
    out={}; errors=[]; pending={str(s["code"]):s for s in stations}
    for i,obs in enumerate(dates[:max_dates],1):
        if not pending: break
        try: img=_process_day(sess,token,collection,bbox,width,height,obs,script)
        except Exception as e:
            errors.append(f"{label} {obs}: {e}"); continue
        gained=0
        for code,st in list(pending.items()):
            p=_sample(img,bbox,float(st["lat"]),float(st["lon"]))
            if not p: continue
            v=_pct(p[0],p[2])
            if v is None: continue
            out[code]={value_field:v,quality_field:_pct(p[1],p[2]),obs_field:obs}
            del pending[code]; gained+=1
        print(f"[R6.38.1] {label} {i}/{min(len(dates),max_dates)} {obs[:10]} +{gained} copertura {len(out)}/{len(stations)}",flush=True)
    if not dates: errors.append(f"{label}: nessuna acquisizione recente")
    return out,dates[:max_dates],errors

def _bbox_around(lat,lon,radius_m=120):
    dlat=radius_m/111320
    dlon=radius_m/(111320*max(.25,math.cos(math.radians(lat))))
    return [lon-dlon,lat-dlat,lon+dlon,lat+dlat]

def _stats_mean(payload,band):
    try:
        entries=payload.get("data") or []
        bands=(((entries[-1] or {}).get("outputs") or {}).get("indices") or {}).get("bands") or {}
        st=(bands.get(band) or {}).get("stats") or {}
        m=st.get("mean"); sample=st.get("sampleCount"); nodata=st.get("noDataCount")
        v=float(m) if m is not None and math.isfinite(float(m)) else None
        q=None
        if sample is not None and float(sample)>0:q=max(0,min(100,(float(sample)-float(nodata or 0))/float(sample)*100))
        return v,q
    except Exception:return None,None

def _rate_wait(interval):
    global _next_request_at
    with _rate_lock:
        now=time.monotonic(); slot=max(now,_next_request_at); _next_request_at=slot+interval
    if slot>now: time.sleep(slot-now)

def _stats_post(token,body,retries=4,interval=.75):
    last=None
    for a in range(1,retries+1):
        _rate_wait(interval)
        try:
            r=requests.post(STATS_URL,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json","Accept":"application/json"},json=body,timeout=75)
            if r.status_code==429 or 500<=r.status_code<=599:
                ra=r.headers.get("Retry-After")
                delay=float(ra) if ra and ra.replace(".","",1).isdigit() else min(30,2*2**(a-1))
                last=RuntimeError(f"HTTP {r.status_code}: {r.text[:1000]}")
                if a<retries: time.sleep(delay); continue
                raise last
            if r.status_code>=400: raise RuntimeError(f"HTTP {r.status_code}: {r.text[:1500]}")
            return r.json() or {}
        except Exception as e:
            last=e
            if a<retries: time.sleep(min(20,1.5*2**(a-1)))
    raise RuntimeError(str(last))

def _s2(token,st,days=16):
    code=str(st.get("code") or ""); lat=float(st["lat"]); lon=float(st["lon"])
    end=datetime.now(timezone.utc); start=end-timedelta(days=days)
    body={"input":{"bounds":{"bbox":_bbox_around(lat,lon),"properties":{"crs":"http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
      "data":[{"type":"sentinel-2-l2a","dataFilter":{"timeRange":{"from":_iso_z(start),"to":_iso_z(end)},"maxCloudCoverage":80,"mosaickingOrder":"leastCC"}}]},
      "aggregation":{"timeRange":{"from":_iso_z(start),"to":_iso_z(end)},"aggregationInterval":{"of":f"P{days}D"},"resx":20,"resy":20,"evalscript":S2_INDEX_EVALSCRIPT}}
    try:
        p=_stats_post(token,body,4,max(.25,float(os.getenv("COPERNICUS_VEG_MIN_INTERVAL_S") or ".75")))
        ndvi,q1=_stats_mean(p,"NDVI"); ndmi,q2=_stats_mean(p,"NDMI"); evi,q3=_stats_mean(p,"EVI")
        qs=[x for x in (q1,q2,q3) if x is not None]
        out={"ndvi_current":round(ndvi,4) if ndvi is not None else None,
             "ndmi_current":round(ndmi,4) if ndmi is not None else None,
             "evi_current":round(evi,4) if evi is not None else None,
             "sentinel_indices_quality_pct":round(min(qs),1) if qs else None,
             "vegetation_indices_source_kind":"copernicus-sentinel-2-l2a-statistical",
             "vegetation_indices_spatial_role":"regional-anchor-at-meteo-station",
             "vegetation_indices_window_start":_iso_z(start),"vegetation_indices_window_end":_iso_z(end)}
        return code,(out if any(out[k] is not None for k in ("ndvi_current","ndmi_current","evi_current")) else {}),None
    except Exception as e:return code,{},str(e)

def fetch_copernicus_bundle(stations,client_id=None,client_secret=None):
    cid=(client_id or os.getenv("COPERNICUS_CLIENT_ID") or "").strip()
    sec=(client_secret or os.getenv("COPERNICUS_CLIENT_SECRET") or "").strip()
    expected=len(stations)
    if not cid or not sec:
        return {},{"status":"UNCONFIGURED","configured":False,"reachable":False,"acquired":0,"expected":expected,"complete":False}
    sess=requests.Session(); errors=[]; out={}; fetched=datetime.now(timezone.utc).isoformat()
    try:
        token=_token(sess,cid,sec); bbox=_station_bbox(stations); width,height=_grid_shape(bbox)
        ssm,ssmd,er=_fill_clms(sess,token,stations,bbox,width,height,SSM_COLLECTION,SSM_EVALSCRIPT,
            "satellite_surface_soil_moisture_pct","copernicus_ssm_noise_pct","copernicus_ssm_observed_at",18,10,"SSM")
        errors+=er
        swi,swid,er=_fill_clms(sess,token,stations,bbox,width,height,SWI_COLLECTION,SWI_EVALSCRIPT,
            "satellite_soil_water_index_pct","copernicus_swi_quality_pct","copernicus_swi_observed_at",45,12,"SWI040")
        errors+=er
        for code in set(ssm)|set(swi):
            r=out.setdefault(code,{})
            r.update(ssm.get(code) or {});r.update(swi.get(code) or {})
            r["copernicus_soil_source_kind"]="copernicus-clms-ssm1km+swi040-1km";r["copernicus_soil_fetched_at"]=fetched
        workers=max(1,min(4,int(os.getenv("COPERNICUS_VEG_WORKERS") or "2")))
        days=max(8,min(30,int(os.getenv("COPERNICUS_VEG_WINDOW_DAYS") or "16")))
        completed=valid=0; veg_errors=[]
        print(f"[R6.38.1] Sentinel-2 start {len(stations)} stazioni workers={workers}",flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures=[pool.submit(_s2,token,s,days) for s in stations]
            for fut in as_completed(futures):
                completed+=1;code,rec,err=fut.result()
                if rec:out.setdefault(code,{}).update(rec);valid+=1
                if err:veg_errors.append(f"{code}: {err}")
                if completed==1 or completed%25==0 or completed==len(stations):
                    print(f"[R6.38.1] Sentinel-2 {completed}/{len(stations)} validi={valid} errori={len(veg_errors)}",flush=True)
        errors+=veg_errors[:60]
        counts={"http_400":sum("HTTP 400" in e for e in veg_errors),"http_429":sum("HTTP 429" in e for e in veg_errors)}
        ssmc=sum(r.get("satellite_surface_soil_moisture_pct") is not None for r in out.values())
        swic=sum(r.get("satellite_soil_water_index_pct") is not None for r in out.values())
        veg=sum(any(r.get(k) is not None for k in ("ndvi_current","ndmi_current","evi_current")) for r in out.values())
        return out,{"status":"OK" if out else "NO_VALID_PIXELS","configured":True,"reachable":bool(ssmd or swid or veg),
          "acquired":len(out),"expected":expected,"complete":len(out)==expected,"ssm_acquired":ssmc,"swi_acquired":swic,
          "soil_acquired":sum(1 for r in out.values() if r.get("satellite_surface_soil_moisture_pct") is not None or r.get("satellite_soil_water_index_pct") is not None),"vegetation_acquired":veg,"last_success_at":fetched if out else None,
          "last_error":" | ".join(errors)[:4000] if errors else None,"error_counts":counts,
          "products":{"ssm":{"catalog_dates_used":ssmd},"swi040":{"catalog_dates_used":swid},
          "sentinel2_indices":{"statistics_endpoint":STATS_URL,"workers":workers,"window_days":days}}}
    except Exception as e:
        return {},{"status":"ERROR","configured":True,"reachable":False,"acquired":0,"expected":expected,"complete":False,"last_error":str(e)[:4000]}
    finally:sess.close()
