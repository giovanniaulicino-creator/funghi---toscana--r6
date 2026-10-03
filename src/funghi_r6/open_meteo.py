# R638-BACKEND-COPERNICUS-LONGHYDRO-2026-10-03
from __future__ import annotations

from datetime import date, timedelta, datetime, timezone
import time
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

CURRENT_VARS = "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
SOIL_VARS = "soil_moisture_3_to_9cm,soil_moisture_9_to_27cm,soil_temperature_6cm,soil_temperature_18cm,vapour_pressure_deficit,evapotranspiration"
DAILY_VARS = ",".join([
    "temperature_2m_min", "temperature_2m_max", "temperature_2m_mean",
    "relative_humidity_2m_min", "relative_humidity_2m_max", "relative_humidity_2m_mean",
    "wind_speed_10m_max", "wind_gusts_10m_max", "wind_speed_10m_mean", "wind_gusts_10m_mean",
    "precipitation_sum", "precipitation_probability_max", "precipitation_hours",
    "sunshine_duration", "shortwave_radiation_sum", "vapour_pressure_deficit_max",
    "et0_fao_evapotranspiration",
])
ARCHIVE_DAILY_VARS = DAILY_VARS.replace(",precipitation_probability_max", "")


def chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _coords(batch: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "latitude": ",".join(f"{float(x['lat']):.5f}" for x in batch),
        "longitude": ",".join(f"{float(x['lon']):.5f}" for x in batch),
    }


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    return [payload] if isinstance(payload, dict) else []


def fetch_component(
    http,
    stations: list[dict[str, Any]],
    *,
    component: str,
    batch_size: int,
    pause_s: float,
    on_batch: Callable[[str, list[dict[str, Any]]], None] | None = None,
    cycle_key: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    records: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def fetch_batch(batch: list[dict[str, Any]], label: str, depth: int = 0) -> None:
        try:
            params = {**_coords(batch), "timezone": "Europe/Rome", "wind_speed_unit": "ms", "cell_selection": "land"}
            if component == "current":
                params["current"] = CURRENT_VARS
                payload = http.get_json(FORECAST_URL, params=params)
            elif component == "forecast":
                params.update({"daily": DAILY_VARS, "past_days": "2", "forecast_days": "16"})
                payload = http.get_json(FORECAST_URL, params=params)
            elif component == "soil":
                params["current"] = SOIL_VARS
                payload = http.get_json(FORECAST_URL, params=params)
            elif component == "history_bootstrap":
                local_day = datetime.now(ZoneInfo("Europe/Rome")).date()
                end = local_day - timedelta(days=1)
                start = end - timedelta(days=99)
                params.update({"start_date": start.isoformat(), "end_date": end.isoformat(), "daily": ARCHIVE_DAILY_VARS})
                payload = http.get_json(ARCHIVE_URL, params=params)
            else:
                raise ValueError(f"Componente sconosciuto: {component}")

            values = _as_list(payload)
            if len(values) != len(batch):
                raise RuntimeError(f"{component}: risposta {len(values)} punti per {len(batch)} coordinate")
            now = datetime.now(timezone.utc).isoformat()
            batch_records: list[dict[str, Any]] = []
            for station, item in zip(batch, values):
                record = {"code": station["code"], "component": component, "fetched_at": now, "cycle_key": cycle_key, "payload": item}
                records[station["code"]] = record
                batch_records.append(record)
            if on_batch:
                on_batch(component, batch_records)
            print(f"[R6] {component}: {label} OK ({len(batch)} stazioni, totale {len(records)})", flush=True)
        except Exception as exc:
            # Lo storico e' il blocco piu' pesante. Un errore su 12 coordinate non deve
            # buttare via l'intero gruppo: lo dividiamo progressivamente e conserviamo
            # ogni sottogruppo riuscito. Il fallimento diventa definitivo solo sulla
            # singola stazione.
            if component == "history_bootstrap" and len(batch) > 1:
                mid = len(batch) // 2
                left, right = batch[:mid], batch[mid:]
                print(f"[R6] {component}: {label} fallito ({exc}); split {len(left)}+{len(right)}", flush=True)
                if pause_s > 0:
                    time.sleep(min(max(pause_s, 0.5), 3.0))
                fetch_batch(left, f"{label}.A", depth + 1)
                if pause_s > 0:
                    time.sleep(min(max(pause_s, 0.5), 3.0))
                fetch_batch(right, f"{label}.B", depth + 1)
                return
            errors.append(f"{label} {batch[0]['code']}..{batch[-1]['code']}: {exc}")
            print(f"[R6] {component}: {label} ERRORE DEFINITIVO: {exc}", flush=True)

    for batch_no, batch in enumerate(chunks(stations, batch_size), start=1):
        fetch_batch(batch, f"batch {batch_no}")
        if pause_s > 0:
            time.sleep(pause_s)
    return records, errors


def daily_rows(code: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    daily = item.get("daily") or {}
    dates = daily.get("time") or []
    rows: list[dict[str, Any]] = []
    for i, day in enumerate(dates):
        def at(key: str):
            xs = daily.get(key) or []
            return xs[i] if i < len(xs) else None
        rows.append({
            "code": code,
            "day": str(day),
            "rain_mm": at("precipitation_sum"),
            "temperature_min_c": at("temperature_2m_min"),
            "temperature_max_c": at("temperature_2m_max"),
            "temperature_mean_c": at("temperature_2m_mean"),
            "humidity_mean_pct": at("relative_humidity_2m_mean"),
            "wind_mean_ms": at("wind_speed_10m_mean"),
            "vpd_max_kpa": at("vapour_pressure_deficit_max"),
            "et0_mm": at("et0_fao_evapotranspiration"),
            "source": "open-meteo-daily",
            "quality": "model_exact_point",
        })
    return rows
