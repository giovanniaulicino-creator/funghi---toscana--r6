from funghi_r6.open_meteo import fetch_component
from funghi_r6.sir import fetch_official_rain
from funghi_r6.assemble import coverage


class SplitHttp:
    def get_json(self, url, *, params=None):
        count = len(str(params["latitude"]).split(","))
        if count > 6:
            raise RuntimeError("simulated large archive batch failure")
        return [{"daily": {"time": ["2026-09-01"]}} for _ in range(count)]


def test_history_batch_failure_is_split_and_recovered():
    stations = [{"code": f"TOS{i:08d}", "lat": 43 + i / 10000, "lon": 10 + i / 10000} for i in range(12)]
    records, errors = fetch_component(SplitHttp(), stations, component="history_bootstrap", batch_size=12, pause_s=0)
    assert len(records) == 12
    assert errors == []


class ReachableButUnusableSir:
    def get_text(self, url):
        return "<html><body>SIR raggiungibile ma nessuna tabella stazioni</body></html>"


def test_sir_reachable_is_not_same_as_data_acquired():
    data, health = fetch_official_rain(ReachableButUnusableSir(), minimum_records=100)
    assert data == {}
    assert health["reachable"] is True
    assert health["acquired"] == 0
    assert health["acquisition_complete"] is False
    assert health["last_error"]


def test_et0_has_its_own_coverage_counter():
    station = {
        "code": "TOS00000001", "lat": 43.0, "lon": 10.0,
        "temperature_c": 10, "humidity_pct": 80, "wind_speed_ms": 1,
        "rain_5d_mm": 1, "rain_7d_mm": 2, "rain_15d_mm": 3, "rain_30d_mm": 4,
        "forecast_precipitation_7d_mm": 1, "forecast_precipitation_15d_mm": 2,
        "forecast_temperature_mean_7d_c": 10, "forecast_temperature_mean_15d_c": 11,
        "forecast_et0_7d_mm": 3, "forecast_et0_15d_mm": 6, "et0_30d_mm": 20,
        "soil_moisture_3_9": .2, "soil_moisture_9_27": .3,
        "weather_daily_30d": [{}] * 30,
    }
    assert coverage([station])["et0"] == 1
