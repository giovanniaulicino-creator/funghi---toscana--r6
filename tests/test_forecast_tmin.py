from datetime import date, timedelta
import json
import os
from pathlib import Path

from funghi_r6.assemble import assemble_station, coverage


def assembled(minima, skip=None):
    start = date(2026, 11, 26)
    indices = [i for i in range(17) if i != skip]
    daily = {
        "time": [(start + timedelta(days=i)).isoformat() for i in indices],
        "temperature_2m_mean": [18] * len(indices),
        "temperature_2m_max": [21] * len(indices),
    }
    if minima is not None:
        daily["temperature_2m_min"] = [minima[i] for i in indices]
    return assemble_station(
        {"code": "TOS1", "lat": 43.0, "lon": 11.0},
        current=None, official_rain=None, soil=None, history=[],
        forecast={"daily": daily}, target_day=start.isoformat(),
    )


def test_tmin_windows_follow_dates_and_preserve_zero():
    row = assembled([-99, 0, None, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, -99], skip=5)
    assert row["forecast_temperature_min_mean_7d_c"] == 4
    assert row["forecast_temperature_min_7d_valid_days"] == 5
    assert row["forecast_temperature_min_target_7d_c"] == 6.5
    assert row["forecast_temperature_min_target_7d_valid_days"] == 2
    assert row["forecast_temperature_min_target_15d_c"] == 13.5
    assert row["forecast_temperature_min_15d_valid_days"] == 13
    assert coverage([row])["forecast_tmin_15"] == 1
    assert coverage([row])["forecast_tmin_full_15"] == 0


def test_missing_and_invalid_tmin_stay_missing():
    for raw in (None, [None] * 17, [True] * 17, [float("inf")] * 17):
        row = assembled(raw)
        assert row["forecast_temperature_min_mean_7d_c"] is None
        assert row["forecast_temperature_min_target_15d_c"] is None
        assert row["forecast_temperature_min_7d_valid_days"] == 0
        assert coverage([row])["forecast_tmin_7"] == 0
        assert row["forecast_temperature_max_mean_7d_c"] == 21


def test_full_tmin_export_for_motor():
    mild, cold = assembled([15] * 17), assembled([-5] * 17)
    assert coverage([mild, cold])["forecast_tmin_full_7"] == 2
    assert coverage([mild, cold])["forecast_tmin_full_15"] == 2
    assert cold["forecast_temperature_min_mean_15d_c"] == -5
    output = os.environ.get("TOSCANA_TMIN_TEST_OUTPUT")
    if output:
        Path(output).write_text(json.dumps({"mild": mild, "cold": cold}), encoding="utf-8")
