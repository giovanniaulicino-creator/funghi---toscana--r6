from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from funghi_r6 import cli, forecast_readiness as readiness
from funghi_r6.cache_policy import reusable_component
from test_r6584_daily import DAY, CODES, db, publish, setup_cli


def forecast(day=DAY):
    return {"timezone": "Europe/Rome", "daily": {
        "time": [(date.fromisoformat(day) + timedelta(days=i)).isoformat() for i in range(1, 16)],
        "precipitation_sum": list(range(1, 16)),
        "temperature_2m_mean": [12] * 15,
        "et0_fao_evapotranspiration": [1] * 15,
    }}


def tail_gap():
    value = forecast()
    for field in readiness.FIELDS:
        value["daily"][field][-1] = None
    return value


def record(payload, code=CODES[0]["code"]):
    return {"code": code, "component": "forecast", "fetched_at": DAY + "T00:15:00+00:00",
            "cycle_key": DAY + "T00-full", "payload": deepcopy(payload)}


def prepare(monkeypatch, tmp_path, db, payload, *, hour=1, forced=False, errors=None):
    args = ["--provider-readiness-guard"] + (["--force-rebuild"] if forced else [])
    setup_cli(monkeypatch, tmp_path, db, *args)
    monkeypatch.setattr(readiness, "utc_now", lambda: datetime.fromisoformat(DAY + f"T{hour:02d}:00:00+00:00"))
    monkeypatch.setattr(cli, "fetch_catalog", lambda *a, **k: (CODES, {}))
    calls = []

    def fetch(http, stations, **options):
        calls.append((options["component"], len(stations)))
        assert options["component"] == "forecast" and len(stations) == 1
        return ({stations[0]["code"]: record(payload, stations[0]["code"])} if payload else {}), errors or []

    monkeypatch.setattr(cli, "fetch_component", fetch)

    def forbidden(*args, **kwargs):
        pytest.fail("Heavy acquisition reached before forecast readiness")

    monkeypatch.setattr(cli, "fetch_official_rain", forbidden)
    monkeypatch.setattr(cli, "fetch_copernicus_bundle", forbidden)
    return calls


def test_archived_failure_and_recovery_keep_original_validator_and_dates():
    cases = json.loads((Path(__file__).parent / "fixtures/r6587_provider_runs.json").read_text())
    payloads = [{"daily": item["daily"]} if "daily" in item else item["payload"] for item in cases]
    assert [reusable_component(CODES[0], "forecast", p, "2026-11-05") for p in payloads] == [False, True, True]
    issues = readiness.forecast_issues(payloads[0], "2026-11-05")
    assert {item["day"] for item in issues} == {"2026-11-20"}
    assert {item["field"] for item in issues} == set(readiness.FIELDS)
    assert readiness.tail_not_ready(issues)
    assert readiness.forecast_issues(payloads[1], "2026-11-05") == []
    assert readiness.forecast_issues(payloads[2], "2026-11-05") == []


def test_wait_stops_after_one_point_without_changing_active_or_cached_batches(db, monkeypatch, tmp_path):
    active = publish(db, day="2026-11-03", generation="yesterday")
    db.persist_component_batch("current", [{**record({"current": {}}), "component": "current"}])
    original_components = db.query("SELECT * FROM component_cache")
    calls = prepare(monkeypatch, tmp_path, db, tail_gap())
    assert cli.main() == 0
    assert calls == [("forecast", 1)]
    receipt = json.loads((tmp_path / "daily-receipt.json").read_text())
    assert receipt["status"] == "WAITING_FORECAST_PROVIDER"
    assert receipt["generation"] is None
    assert db.active_generation() == "yesterday"
    assert db.daily_active_manifest("2026-11-03", 418) == active
    assert db.query("SELECT * FROM component_cache") == original_components
    assert not (tmp_path / "snapshot.json").exists()
    detail = json.loads((tmp_path / "forecast-readiness.json").read_text())
    assert detail["published"] is False
    assert detail["evidence"]["daily"]["precipitation_sum"][-1] is None
    assert detail["deadline_utc"] == DAY + "T06:00:00+00:00"


@pytest.mark.parametrize("hour,forced", [(6, False), (8, False), (1, True)])
def test_tail_gap_after_deadline_or_forced_build_is_a_real_failure(db, monkeypatch, tmp_path, hour, forced):
    publish(db, day="2026-11-03", generation="yesterday")
    prepare(monkeypatch, tmp_path, db, tail_gap(), hour=hour, forced=forced)
    with pytest.raises(SystemExit, match="forecast preliminare incompleto"):
        cli.main()
    assert json.loads((tmp_path / "daily-receipt.json").read_text())["status"] == "BLOCKED_FORECAST_PREFLIGHT"
    assert db.active_generation() == "yesterday"


@pytest.mark.parametrize("damage", ["hole", "duplicate_tail", "string", "nan", "inf", "missing_series", "schema", "short_array",
                                    "later_date", "reversed", "invalid_date"])
def test_other_corruption_is_never_reclassified_as_wait(db, monkeypatch, tmp_path, damage):
    payload = tail_gap()
    daily = payload["daily"]
    if damage == "hole":
        daily["time"][4] = "2026-01-01"
    elif damage == "duplicate_tail":
        daily["time"].append(daily["time"][-1])
    elif damage == "string":
        daily["temperature_2m_mean"][-1] = "unavailable"
    elif damage == "nan":
        daily["temperature_2m_mean"][-1] = float("nan")
    elif damage == "inf":
        daily["temperature_2m_mean"][-1] = float("inf")
    elif damage == "missing_series":
        del daily["temperature_2m_mean"]
    elif damage == "short_array":
        daily["temperature_2m_mean"] = [12]
    elif damage == "later_date":
        daily["time"].append((date.fromisoformat(DAY) + timedelta(days=16)).isoformat())
        for field in readiness.FIELDS:
            daily[field].append(1)
    elif damage == "reversed":
        daily["time"].reverse()
    elif damage == "invalid_date":
        daily["time"].insert(0, "not-a-date")
    else:
        payload["daily"] = []
    prepare(monkeypatch, tmp_path, db, payload)
    with pytest.raises(SystemExit):
        cli.main()
    report = json.loads((tmp_path / "forecast-readiness.json").read_text())
    assert report["status"] == "BLOCKED_FORECAST_PREFLIGHT"
    json.dumps(report, allow_nan=False)


def test_transport_failure_is_not_wait(db, monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path, db, None, errors=["HTTP 503 from provider"])
    with pytest.raises(SystemExit):
        cli.main()
    report = json.loads((tmp_path / "daily-receipt.json").read_text())
    assert report["status"] == "BLOCKED_FORECAST_PREFLIGHT"
    assert report["forecast_readiness"]["errors"] == ["HTTP 503 from provider"]


@pytest.mark.parametrize("day,instant,expected", [
    ("2026-11-05", "2026-11-05T05:59:59+00:00", True),
    ("2026-11-05", "2026-11-05T06:00:00+00:00", False),
    ("2026-06-05", "2026-06-04T23:15:00+00:00", True),
    ("2026-06-05", "2026-06-05T06:00:00+00:00", False),
    ("2026-03-29", "2026-03-29T05:59:00+00:00", True),
    ("2026-10-25", "2026-10-25T06:00:00+00:00", False),
])
def test_deadline_is_full_utc_instant_across_dst(day, instant, expected):
    assert readiness.may_wait(day, datetime.fromisoformat(instant), forced=False) is expected


def test_available_probe_is_reused_but_all_418_still_require_validation(db, monkeypatch, tmp_path):
    publish(db, day="2026-11-03", generation="yesterday")
    prepare(monkeypatch, tmp_path, db, forecast())
    monkeypatch.setattr(cli, "fetch_official_rain", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cli, "fetch_copernicus_bundle", lambda *a: ({}, {}))
    calls = []

    def only_probe_ready(http, stations, **options):
        calls.append((options["component"], len(stations)))
        if options["component"] == "forecast" and len(stations) == 1:
            return {stations[0]["code"]: record(forecast(), stations[0]["code"])}, []
        if options["component"] == "forecast":
            assert CODES[0]["code"] not in {s["code"] for s in stations}
            return {s["code"]: record(tail_gap(), s["code"]) for s in stations}, []
        return {}, ["fixture: other components unavailable"]

    monkeypatch.setattr(cli, "fetch_component", only_probe_ready)
    with pytest.raises(SystemExit, match="R6 incompleta"):
        cli.main()
    assert calls[:3] == [("forecast", 1), ("current", 418), ("forecast", 417)]
    assert db.active_generation() == "yesterday"
    rejects = json.loads((tmp_path / "forecast-rejections.json").read_text())["rejections"]
    assert len(rejects) == 417
    assert all(item["issues"][0]["offset"] == 15 for item in rejects)


def test_complete_cached_probe_needs_no_live_probe(db, monkeypatch, tmp_path):
    calls = prepare(monkeypatch, tmp_path, db, None, errors=["must not fetch"])
    db.persist_component_batch("forecast", [record(forecast())])
    monkeypatch.setattr(cli, "fetch_official_rain", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("NORMAL_BUILD_REACHED")))
    with pytest.raises(RuntimeError, match="NORMAL_BUILD_REACHED"):
        cli.main()
    assert calls == []
    report = json.loads((tmp_path / "forecast-readiness.json").read_text())
    assert report["from_cache"] is True
    assert report["status"] == "PROBE_READY_FULL_VALIDATION_REQUIRED"


def test_full_real_assembly_publishes_once_and_next_attempt_uses_active(db, monkeypatch, tmp_path):
    publish(db, day="2026-11-03", generation="yesterday")
    prepare(monkeypatch, tmp_path, db, forecast())
    calls = []
    history = [{"day": (date.fromisoformat(DAY) - timedelta(days=90-i)).isoformat(),
                "rain_mm": 1, "temperature_mean_c": 12, "humidity_mean_pct": 80,
                "wind_mean_ms": 2, "et0_mm": 1} for i in range(90)]
    monkeypatch.setattr(db, "load_history_map", lambda *a: {s["code"]: history for s in CODES})
    monkeypatch.setattr(cli, "fetch_official_rain", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cli, "fetch_copernicus_bundle", lambda *a: ({}, {}))

    def sources(http, stations, **options):
        component = options["component"]
        calls.append((component, len(stations)))
        assert component in {"forecast", "current", "soil"}
        payload = forecast() if component == "forecast" else {"current":
            {"time": DAY + "T05:00", "temperature_2m": 12, "relative_humidity_2m": 80, "wind_speed_10m": 2}
            if component == "current" else {"time": DAY + "T05:00", "soil_moisture_3_to_9cm": 0.2,
                                           "soil_moisture_9_to_27cm": 0.3}}
        rows = [{**record(payload, s["code"]), "component": component} for s in stations]
        if options.get("on_batch"):
            options["on_batch"](component, rows)
        return {row["code"]: row for row in rows}, []

    monkeypatch.setattr(cli, "fetch_component", sources)
    assert cli.main() == 0
    active = db.daily_active_manifest(DAY, 418)
    assert active is not None
    assert active["coverage"]["scientific_complete"] == 418
    snapshot = json.loads((tmp_path / "snapshot.json").read_text())
    assert snapshot["data"][0]["forecast_precipitation_15d_mm"] == sum(range(1, 16))
    assert calls == [("forecast", 1), ("current", 418), ("forecast", 417), ("soil", 418)]
    assert json.loads((tmp_path / "daily-receipt.json").read_text())["status"] == "DAILY_PUBLISHED"
    calls.clear()
    assert cli.main() == 0
    assert calls == []
    assert db.daily_active_manifest(DAY, 418) == active
    assert json.loads((tmp_path / "daily-receipt.json").read_text())["status"] == "DAILY_ALREADY_READY"
