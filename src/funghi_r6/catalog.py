from __future__ import annotations

from typing import Any

WFS_URL = "https://geo.sir.toscana.it/geoserver/geo/ows"
LEGACY_CATALOG_URL = "https://porcini-toscana-ai.porcinitoscanaai.workers.dev/api/v1/stations"


def _first(props: dict[str, Any], *names: str) -> Any:
    lowered = {str(k).lower(): v for k, v in props.items()}
    for name in names:
        if name.lower() in lowered and lowered[name.lower()] not in (None, ""):
            return lowered[name.lower()]
    return None


def _code(props: dict[str, Any]) -> str | None:
    for key, value in props.items():
        text = str(value or "").strip().upper()
        if text.startswith("TOS") and text[3:].isdigit() and len(text) >= 11:
            return text
    value = _first(props, "codice", "code", "idstazione", "id_stazione", "idsensore")
    return str(value).strip().upper() if value else None


def normalize_wfs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        code = _code(props)
        if not code or code in seen or len(coords) < 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        station = {
            "code": code,
            "name": _first(props, "nome", "name", "denominazione") or code,
            "municipality": _first(props, "comune", "municipality"),
            "province": _first(props, "provincia", "province", "sigla_prov"),
            "zone": _first(props, "zona", "zone"),
            "altitude_m": _to_float(_first(props, "quota", "altitudine", "altitude", "quota_m")),
            "lat": lat,
            "lon": lon,
            "registry_source": "sir-wfs-cf_pluviometri",
        }
        out.append(station)
        seen.add(code)
    return sorted(out, key=lambda x: x["code"])


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _legacy_catalog(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for raw in payload.get("data") or []:
        code = str(raw.get("code") or "").strip().upper()
        lat, lon = _to_float(raw.get("lat")), _to_float(raw.get("lon"))
        if not code or code in seen or lat is None or lon is None:
            continue
        out.append({
            "code": code, "name": raw.get("name") or code,
            "municipality": raw.get("municipality"), "province": raw.get("province"),
            "zone": raw.get("zone"), "altitude_m": _to_float(raw.get("altitude_m")),
            "lat": lat, "lon": lon, "registry_source": "r5-structural-bootstrap-fallback",
        })
        seen.add(code)
    return sorted(out, key=lambda x: x["code"])


def fetch_catalog(http, *, expected: int, persisted: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    errors: list[str] = []
    params = {
        "service": "WFS", "version": "1.0.0", "request": "GetFeature",
        "typeName": "geo:cf_pluviometri", "maxFeatures": "300000",
        "outputFormat": "application/json", "srsName": "EPSG:4326",
    }
    try:
        stations = normalize_wfs(http.get_json(WFS_URL, params=params))
        if len(stations) == expected:
            return stations, {
                "source": "SIR/WFS cf_pluviometri", "reachable": True,
                "acquired": len(stations), "expected": expected, "complete": True,
                "fallback": False,
            }
        errors.append(f"WFS count {len(stations)}/{expected}")
    except Exception as exc:
        errors.append(f"WFS: {exc}")

    if persisted and len(persisted) == expected:
        return persisted, {
            "source": "D1 persisted station_catalog", "reachable": False,
            "acquired": len(persisted), "expected": expected, "complete": True,
            "fallback": True, "last_error": errors[-1] if errors else None,
        }

    try:
        payload = http.get_json(LEGACY_CATALOG_URL, params={"lite": "1", "r6_catalog": "1"})
        stations = _legacy_catalog(payload)
        if len(stations) == expected:
            return stations, {
                "source": "R5 structural bootstrap fallback", "reachable": True,
                "acquired": len(stations), "expected": expected, "complete": True,
                "fallback": True, "last_error": errors[-1] if errors else None,
            }
        errors.append(f"R5 structural count {len(stations)}/{expected}")
    except Exception as exc:
        errors.append(f"R5 structural: {exc}")

    return [], {
        "source": "station catalog", "reachable": False, "acquired": 0,
        "expected": expected, "complete": False, "fallback": True,
        "last_error": "; ".join(errors[-3:]),
    }
