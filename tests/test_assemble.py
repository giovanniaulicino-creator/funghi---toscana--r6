from datetime import date, timedelta
from funghi_r6.assemble import assemble_station, coverage


def _daily():
    start = date.today() - timedelta(days=89)
    return [{"code":"TOS1","day":(start+timedelta(days=i)).isoformat(),"rain_mm":1,"temperature_mean_c":15,"humidity_mean_pct":80,"wind_mean_ms":2,"et0_mm":1} for i in range(90)]


def _forecast():
    start = date.today() - timedelta(days=2)
    days = [(start+timedelta(days=i)).isoformat() for i in range(18)]
    n=len(days)
    return {"daily":{"time":days,"precipitation_sum":[1]*n,"precipitation_probability_max":[80]*n,"temperature_2m_min":[10]*n,"temperature_2m_max":[20]*n,"temperature_2m_mean":[15]*n,"relative_humidity_2m_mean":[80]*n,"relative_humidity_2m_max":[95]*n,"wind_speed_10m_mean":[2]*n,"wind_gusts_10m_mean":[4]*n,"vapour_pressure_deficit_max":[0.7]*n,"et0_fao_evapotranspiration":[1]*n}}


def test_official_rain_wins_and_station_is_complete():
    s=assemble_station(
        {"code":"TOS1","name":"x","lat":43,"lon":11},
        official_rain={"rain_5d_mm":9,"rain_7d_mm":11,"rain_15d_mm":22,"rain_30d_mm":44,"rain_observed_label":"now"},
        current={"current":{"time":"now","temperature_2m":15,"relative_humidity_2m":80,"wind_speed_10m":2}},
        forecast=_forecast(),
        soil={"current":{"time":"now","soil_moisture_3_to_9cm":0.2,"soil_moisture_9_to_27cm":0.3,"evapotranspiration":0.5}},
        history=_daily(),
    )
    assert s["rain_30d_mm"] == 44
    assert s["provenance"]["rain_30d_mm"]["quality"] == "official"
    assert coverage([s])["scientific_complete"] == 1


def test_r6401_forecast_timing_buckets_are_disjoint_and_complete():
    s=assemble_station(
        {"code":"TOS1","name":"x","lat":43,"lon":11},
        official_rain={"rain_5d_mm":9,"rain_7d_mm":11,"rain_15d_mm":22,"rain_30d_mm":44,"rain_observed_label":"now"},
        current={"current":{"time":"now","temperature_2m":15,"relative_humidity_2m":80,"wind_speed_10m":2}},
        forecast=_forecast(),
        soil={"current":{"time":"now","soil_moisture_3_to_9cm":0.2,"soil_moisture_9_to_27cm":0.3,"evapotranspiration":0.5}},
        history=_daily(),
    )
    assert s["forecast_precipitation_7d_mm"] == 7
    assert s["forecast_precipitation_15d_mm"] == 15
    assert [s["forecast_precipitation_days1_2_7d_mm"],s["forecast_precipitation_days3_4_7d_mm"],s["forecast_precipitation_days5_7_7d_mm"]] == [2,2,3]
    assert [s["forecast_precipitation_days1_3_15d_mm"],s["forecast_precipitation_days4_7_15d_mm"],s["forecast_precipitation_days8_11_15d_mm"],s["forecast_precipitation_days12_15_15d_mm"]] == [3,4,4,4]
    assert s["forecast_precipitation_probability_mean_7d_pct"] == 80
    assert s["forecast_precipitation_probability_days1_3_15d_pct"] == 80
    assert s["forecast_precipitation_7d_valid_days"] == 7
    assert s["forecast_precipitation_15d_valid_days"] == 15
    cov=coverage([s])
    assert cov["forecast_timing_7"] == 1
    assert cov["forecast_timing_15"] == 1


def test_r6402_target_window_climate_is_published():
    s=assemble_station(
        {"code":"TOS1","name":"x","lat":43,"lon":11},
        official_rain={"rain_5d_mm":9,"rain_7d_mm":11,"rain_15d_mm":22,"rain_30d_mm":44,"rain_observed_label":"now"},
        current={"current":{"time":"now","temperature_2m":15,"relative_humidity_2m":80,"wind_speed_10m":2}},
        forecast=_forecast(),
        soil={"current":{"time":"now","soil_moisture_3_to_9cm":0.2,"soil_moisture_9_to_27cm":0.3,"evapotranspiration":0.5}},
        history=_daily(),
    )
    assert s["forecast_temperature_target_7d_c"] == 15
    assert s["forecast_temperature_max_target_7d_c"] == 20
    assert s["forecast_humidity_target_7d_pct"] == 80
    assert s["forecast_humidity_max_target_7d_pct"] == 95
    assert s["forecast_wind_target_7d_ms"] == 2
    assert s["forecast_wind_gust_target_7d_ms"] == 4
    assert s["forecast_vpd_max_target_7d_kpa"] == 0.7
    assert s["forecast_diurnal_temperature_range_target_7d_c"] == 10
    assert s["forecast_target_window_7d_valid_days"] == 3
    assert s["forecast_temperature_target_15d_c"] == 15
    assert s["forecast_target_window_15d_valid_days"] == 4


def test_r641_source_priority_and_conflict_diagnostics():
    s=assemble_station(
        {"code":"TOS1","name":"x","lat":43,"lon":11},
        official_rain={"rain_5d_mm":30,"rain_7d_mm":32,"rain_15d_mm":35,"rain_30d_mm":40,"rain_observed_label":"now"},
        current={"current":{"time":"now","temperature_2m":15,"relative_humidity_2m":80,"wind_speed_10m":2}},
        forecast=_forecast(),
        soil={"current":{"time":"now","soil_moisture_3_to_9cm":0.2,"soil_moisture_9_to_27cm":0.3,"evapotranspiration":0.5}},
        history=_daily(),
    )
    assert s["source_priority_version"] == "r641-source-priority-v1"
    assert s["source_decisions"]["rain_5d_mm"]["chosen"] == "SIR/CFR official"
    assert s["rain_5d_mm"] == 30
    assert s["rain_openmeteo_5d_mm"] == 5
    assert any(x["field"] == "rain_5d_mm" for x in s["source_conflicts"])
    assert s["source_decisions"]["rain_60_90d"]["bias_correction_applied"] is False


def test_r641_openmeteo_is_explicit_fallback_when_official_missing():
    s=assemble_station(
        {"code":"TOS1","name":"x","lat":43,"lon":11},
        official_rain={},
        current={"current":{"time":"now","temperature_2m":15,"relative_humidity_2m":80,"wind_speed_10m":2}},
        forecast=_forecast(),
        soil={"current":{"time":"now","soil_moisture_3_to_9cm":0.2,"soil_moisture_9_to_27cm":0.3,"evapotranspiration":0.5}},
        history=_daily(),
    )
    assert s["source_decisions"]["rain_7d_mm"]["chosen"] == "Open-Meteo exact-point daily fallback"
    assert s["rain_7d_mm"] == 7
