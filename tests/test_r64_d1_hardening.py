import hashlib
import json

import pytest

from funghi_r6.d1 import split_utf8_chunks, validate_snapshot_contract
from funghi_r6.assemble import assemble_station


def _manifest(n: int, raw: bytes, generation="R6-test", target_day="2026-10-02"):
    coverage = {
        "total": n,
        "structural": n,
        "current": n,
        "rain_5_7_15_30": n,
        "history_30": n,
        "forecast_7": n,
        "forecast_15": n,
        "et0": n,
        "soil": n,
        "scientific_complete": n,
    }
    return {
        "generation": generation,
        "target_day": target_day,
        "station_count": n,
        "scientifically_complete": True,
        "coverage": coverage,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_bytes": len(raw),
    }


def test_utf8_chunking_roundtrip_is_byte_exact():
    raw = (("àèìòù🍄Toscana-" * 10000) + "fine").encode("utf-8")
    chunks = split_utf8_chunks(raw, max_bytes=4096)
    rebuilt = b"".join(text.encode("utf-8") for text, _ in chunks)
    assert rebuilt == raw
    assert all(raw_bytes <= 4096 for _, raw_bytes in chunks)


def test_publication_gate_accepts_only_exact_complete_snapshot():
    stations = [
        {"code": "TOS1"},
        {"code": "TOS2"},
    ]
    payload = {
        "meta": {
            "generation": "R6-test",
            "target_day": "2026-10-02",
            "scientifically_complete": True,
        },
        "data": stations,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    manifest = _manifest(2, raw)

    result = validate_snapshot_contract(
        "R6-test", "2026-10-02", stations, manifest, raw, 2
    )
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()


def test_publication_gate_blocks_incomplete_coverage():
    stations = [{"code": "TOS1"}, {"code": "TOS2"}]
    payload = {
        "meta": {
            "generation": "R6-test",
            "target_day": "2026-10-02",
            "scientifically_complete": True,
        },
        "data": stations,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    manifest = _manifest(2, raw)
    manifest["coverage"]["history_30"] = 1

    with pytest.raises(RuntimeError, match="history_30"):
        validate_snapshot_contract(
            "R6-test", "2026-10-02", stations, manifest, raw, 2
        )


def test_target_day_drives_forecast_not_runner_utc_date():
    days = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    forecast = {
        "daily": {
            "time": days,
            "precipitation_sum": [100, 100, 1, 2],
            "temperature_2m_mean": [99, 99, 10, 20],
            "relative_humidity_2m_mean": [99, 99, 70, 80],
            "wind_speed_10m_mean": [99, 99, 2, 3],
            "et0_fao_evapotranspiration": [99, 99, 1, 2],
        }
    }
    history = [
        {
            "day": f"2026-09-{i:02d}",
            "rain_mm": 1,
            "temperature_mean_c": 15,
            "humidity_mean_pct": 80,
            "wind_mean_ms": 2,
            "et0_mm": 1,
        }
        for i in range(1, 29)
    ]
    station = assemble_station(
        {"code": "TOS1", "lat": 43, "lon": 11},
        official_rain={
            "rain_5d_mm": 5,
            "rain_7d_mm": 7,
            "rain_15d_mm": 15,
            "rain_30d_mm": 30,
        },
        current={
            "current": {
                "temperature_2m": 15,
                "relative_humidity_2m": 80,
                "wind_speed_10m": 2,
            }
        },
        forecast=forecast,
        soil={
            "current": {
                "soil_moisture_3_to_9cm": 0.2,
                "soil_moisture_9_to_27cm": 0.3,
                "evapotranspiration": 0.5,
            }
        },
        history=history,
        target_day="2026-10-02",
    )
    assert station["forecast_precipitation_7d_mm"] == 3
    assert station["forecast_temperature_mean_7d_c"] == 15
