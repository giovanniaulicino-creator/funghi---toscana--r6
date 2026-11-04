"""Cache reuse follows the existing scientific coverage, without changing it."""
from __future__ import annotations

from datetime import date, timedelta
import math

from .assemble import assemble_station, coverage
from .open_meteo import daily_rows


def _coverage(station: dict, target_day: str, **inputs) -> dict:
    values = {"official_rain": None, "current": None, "forecast": None, "soil": None, "history": []}
    values.update(inputs)
    return coverage([assemble_station(station, target_day=target_day, **values)])


def reusable_component(station: dict, component: str, payload: object, target_day: str) -> bool:
    required = {"current": ("current",), "forecast": ("forecast_7", "forecast_15"), "soil": ("soil",)}
    if component not in required or not isinstance(payload, dict):
        return False
    try:
        if component == "forecast":
            daily = payload.get("daily") or {}
            dates = daily.get("time") or []
            for offset in range(1, 16):
                day = (date.fromisoformat(target_day) + timedelta(days=offset)).isoformat()
                if dates.count(day) != 1:
                    return False
                index = dates.index(day)
                for field in ("precipitation_sum", "temperature_2m_mean", "et0_fao_evapotranspiration"):
                    values = daily.get(field) or []
                    if index >= len(values) or not math.isfinite(float(values[index])):
                        return False
        cov = _coverage(station, target_day, **{component: payload})
        return all(cov[key] == 1 for key in required[component])
    except (AttributeError, TypeError, ValueError, IndexError, OverflowError):
        return False


def reusable_history(station: dict, rows: list, target_day: str, official_rain: dict | None) -> bool:
    try:
        cov = _coverage(station, target_day, history=rows, official_rain=official_rain)
        # Count alone does not certify that numeric rain/ET0 values are present.
        return all(cov[key] == 1 for key in ("history_90", "long_hydro_90"))
    except (AttributeError, TypeError, ValueError, IndexError, OverflowError):
        return False


def reusable_history_payload(station: dict, payload: object, target_day: str, official_rain: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        return reusable_history(station, daily_rows(station["code"], payload), target_day, official_rain)
    except (AttributeError, TypeError, ValueError, IndexError, OverflowError):
        return False
