from datetime import date, timedelta
from funghi_r6.assemble import assemble_station, coverage


def _daily():
    start = date.today() - timedelta(days=34)
    return [{"code":"TOS1","day":(start+timedelta(days=i)).isoformat(),"rain_mm":1,"temperature_mean_c":15,"humidity_mean_pct":80,"wind_mean_ms":2,"et0_mm":1} for i in range(35)]


def _forecast():
    start = date.today() - timedelta(days=2)
    days = [(start+timedelta(days=i)).isoformat() for i in range(18)]
    n=len(days)
    return {"daily":{"time":days,"precipitation_sum":[1]*n,"temperature_2m_mean":[15]*n,"relative_humidity_2m_mean":[80]*n,"wind_speed_10m_mean":[2]*n,"et0_fao_evapotranspiration":[1]*n}}


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
