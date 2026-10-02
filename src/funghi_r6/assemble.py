from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def _n(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _mean(values: list[Any]) -> float | None:
    xs = [_n(v) for v in values]
    ys = [x for x in xs if x is not None]
    return round(sum(ys) / len(ys), 3) if ys else None


def _sum(values: list[Any]) -> float | None:
    xs = [_n(v) for v in values]
    ys = [x for x in xs if x is not None]
    return round(sum(ys), 3) if ys else None


def _window(history: list[dict[str, Any]], days: int, field: str) -> list[Any]:
    rows = sorted(history, key=lambda x: x.get("day") or "")[-days:]
    return [x.get(field) for x in rows]


def _daily_forecast(item: dict[str, Any], days: int, key: str, target_day: str) -> list[Any]:
    daily = item.get("daily") or {}
    dates = list(daily.get("time") or [])
    values = list(daily.get(key) or [])
    pairs = [
        (str(d), values[i] if i < len(values) else None)
        for i, d in enumerate(dates)
        if str(d) > target_day
    ]
    return [v for _, v in pairs[:days]]


def assemble_station(
    station: dict[str, Any],
    *,
    official_rain: dict[str, Any] | None,
    current: dict[str, Any] | None,
    forecast: dict[str, Any] | None,
    soil: dict[str, Any] | None,
    history: list[dict[str, Any]],
    target_day: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    resolved_target_day = target_day or date.today().isoformat()
    cur = (current or {}).get("current") or {}
    frc = forecast or {}
    sl = (soil or {}).get("current") or {}
    official = official_rain or {}

    fallback_rain = {d: _sum(_window(history, d, "rain_mm")) for d in (5, 7, 15, 30)}
    rain = {}
    provenance: dict[str, Any] = {}
    for d in (5, 7, 15, 30):
        off = _n(official.get(f"rain_{d}d_mm"))
        rain[d] = off if off is not None else fallback_rain[d]
        provenance[f"rain_{d}d_mm"] = {
            "source": "SIR/CFR official" if off is not None else "Open-Meteo exact-point daily fallback",
            "timestamp": official.get("rain_observed_label") if off is not None else (history[-1].get("day") if history else None),
            "quality": "official" if off is not None else "model_exact_point",
        }

    out = {
        **station,
        "temperature_c": _n(cur.get("temperature_2m")),
        "humidity_pct": _n(cur.get("relative_humidity_2m")),
        "wind_speed_ms": _n(cur.get("wind_speed_10m")),
        "wind_gust_ms": _n(cur.get("wind_gusts_10m")),
        "wind_direction_deg": _n(cur.get("wind_direction_10m")),
        "rain_5d_mm": rain[5],
        "rain_7d_mm": rain[7],
        "rain_15d_mm": rain[15],
        "rain_30d_mm": rain[30],
        "temperature_mean_30d_c": _mean(_window(history, 30, "temperature_mean_c")),
        "humidity_mean_30d_pct": _mean(_window(history, 30, "humidity_mean_pct")),
        "wind_mean_30d_ms": _mean(_window(history, 30, "wind_mean_ms")),
        "et0_30d_mm": _sum(_window(history, 30, "et0_mm")),
        "forecast_precipitation_7d_mm": _sum(_daily_forecast(frc, 7, "precipitation_sum", resolved_target_day)),
        "forecast_precipitation_15d_mm": _sum(_daily_forecast(frc, 15, "precipitation_sum", resolved_target_day)),
        "forecast_temperature_mean_7d_c": _mean(_daily_forecast(frc, 7, "temperature_2m_mean", resolved_target_day)),
        "forecast_temperature_mean_15d_c": _mean(_daily_forecast(frc, 15, "temperature_2m_mean", resolved_target_day)),
        "forecast_humidity_mean_7d_pct": _mean(_daily_forecast(frc, 7, "relative_humidity_2m_mean", resolved_target_day)),
        "forecast_humidity_mean_15d_pct": _mean(_daily_forecast(frc, 15, "relative_humidity_2m_mean", resolved_target_day)),
        "forecast_wind_mean_7d_ms": _mean(_daily_forecast(frc, 7, "wind_speed_10m_mean", resolved_target_day)),
        "forecast_wind_mean_15d_ms": _mean(_daily_forecast(frc, 15, "wind_speed_10m_mean", resolved_target_day)),
        "forecast_et0_7d_mm": _sum(_daily_forecast(frc, 7, "et0_fao_evapotranspiration", resolved_target_day)),
        "forecast_et0_15d_mm": _sum(_daily_forecast(frc, 15, "et0_fao_evapotranspiration", resolved_target_day)),
        "soil_moisture_3_9": _n(sl.get("soil_moisture_3_to_9cm")),
        "soil_moisture_9_27": _n(sl.get("soil_moisture_9_to_27cm")),
        "soil_temperature_6cm_c": _n(sl.get("soil_temperature_6cm")),
        "soil_temperature_18cm_c": _n(sl.get("soil_temperature_18cm")),
        "vpd_kpa": _n(sl.get("vapour_pressure_deficit")),
        "current_et0_mm": _n(sl.get("evapotranspiration")),
        "weather_daily_30d": sorted(history, key=lambda x: x.get("day") or "")[-30:],
        "rain_daily_30d": [
            {"day": x.get("day"), "mm": x.get("rain_mm"), "days_ago": i + 1}
            for i, x in enumerate(reversed(sorted(history, key=lambda x: x.get("day") or "")[-30:]))
        ],
        "weather_core_cached_at": now,
        "weather_history_cached_at": now,
        "forecast_7d_cached_at": now,
        "forecast_15d_cached_at": now,
        "soil_cached_at": now,
        "provenance": provenance,
    }
    for key in ["temperature_c", "humidity_pct", "wind_speed_ms"]:
        out["provenance"][key] = {
            "source": "Open-Meteo exact-point",
            "timestamp": cur.get("time"),
            "quality": "model_exact_point",
        }
    for key in ["soil_moisture_3_9", "soil_moisture_9_27", "current_et0_mm"]:
        out["provenance"][key] = {
            "source": "Open-Meteo exact-point",
            "timestamp": sl.get("time"),
            "quality": "model_exact_point",
        }
    return out


def station_complete(s: dict[str, Any]) -> bool:
    required = [
        "temperature_c", "humidity_pct", "wind_speed_ms",
        "rain_5d_mm", "rain_7d_mm", "rain_15d_mm", "rain_30d_mm",
        "forecast_precipitation_7d_mm", "forecast_precipitation_15d_mm",
        "forecast_temperature_mean_7d_c", "forecast_temperature_mean_15d_c",
        "forecast_et0_7d_mm", "forecast_et0_15d_mm",
        "soil_moisture_3_9", "soil_moisture_9_27",
    ]
    return all(_n(s.get(k)) is not None for k in required) and len(s.get("weather_daily_30d") or []) >= 28


def coverage(stations: list[dict[str, Any]]) -> dict[str, int]:
    total = len(stations)

    def count(fn):
        return sum(1 for s in stations if fn(s))

    numeric = lambda s, k: _n(s.get(k)) is not None
    return {
        "total": total,
        "structural": count(lambda s: bool(s.get("code")) and _n(s.get("lat")) is not None and _n(s.get("lon")) is not None),
        "current": count(lambda s: all(numeric(s, k) for k in ["temperature_c", "humidity_pct", "wind_speed_ms"])),
        "rain_5_7_15_30": count(lambda s: all(numeric(s, f"rain_{d}d_mm") for d in [5, 7, 15, 30])),
        "history_30": count(lambda s: len(s.get("weather_daily_30d") or []) >= 28),
        "forecast_7": count(lambda s: all(numeric(s, k) for k in ["forecast_precipitation_7d_mm", "forecast_temperature_mean_7d_c", "forecast_et0_7d_mm"])),
        "forecast_15": count(lambda s: all(numeric(s, k) for k in ["forecast_precipitation_15d_mm", "forecast_temperature_mean_15d_c", "forecast_et0_15d_mm"])),
        "et0": count(lambda s: all(numeric(s, k) for k in ["et0_30d_mm", "forecast_et0_7d_mm", "forecast_et0_15d_mm"])),
        "soil": count(lambda s: all(numeric(s, k) for k in ["soil_moisture_3_9", "soil_moisture_9_27"])),
        "scientific_complete": count(station_complete),
    }
