import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from funghi_r6 import cli
from funghi_r6.d1 import D1
from funghi_r6.daily import DAILY_COVERAGE, complete_daily_manifest, validate_day

DAY = "2026-11-04"
CODES = [{"code": f"TOS{i:08d}", "lat": 43.0, "lon": 11.0} for i in range(418)]


class SQLiteD1(D1):
    """Execute production SQL against SQLite, with the D1 REST result envelope."""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def query(self, sql, params=None):
        with self.conn:
            cur = self.conn.execute(sql, params or [])
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        return [{"success": True, "results": rows}]

    def batch(self, statements):
        result = []
        with self.conn:
            for sql, params in statements:
                cur = self.conn.execute(sql, params)
                result.append({"success": True, "results": [dict(r) for r in cur.fetchall()] if cur.description else []})
        return result


@pytest.fixture
def db():
    value = SQLiteD1()
    value.init_schema(Path(cli.__file__).with_name("schema.sql").read_text())
    return value


def publish(db, day=DAY, generation="R6-test"):
    raw = json.dumps({"meta": {"generation": generation, "target_day": day, "scientifically_complete": True},
                      "data": CODES}, separators=(",", ":")).encode()
    manifest = {"architecture": "R6", "generation": generation, "target_day": day,
                "station_count": 418, "scientifically_complete": True,
                "coverage": {k: 418 for k in DAILY_COVERAGE}, "sha256": hashlib.sha256(raw).hexdigest(),
                "snapshot_bytes": len(raw)}
    db.publish_generation(generation, day, CODES, manifest, raw, expected_stations=418)
    return db.daily_active_manifest(day, 418)


def setup_cli(monkeypatch, tmp_path, db, *args):
    monkeypatch.setattr(sys, "argv", ["builder", "--daily", "--target-day", DAY, "--out", str(tmp_path), *args])
    monkeypatch.setattr(cli, "rome_day", lambda: DAY)
    monkeypatch.setattr("funghi_r6.d1.rome_day", lambda: DAY)
    monkeypatch.setattr(cli, "D1", lambda *a, **k: db)
    monkeypatch.setattr(cli, "SETTINGS", SimpleNamespace(d1_enabled=True, cf_account_id="test",
        cf_d1_database_id="test", cf_api_token="test", max_retries=1, expected_stations=418,
        timezone="Europe/Rome", batch_size=12, batch_pause_s=0, model_version="unchanged"))


def test_real_publication_is_reused_without_reading_snapshot_payload(db):
    manifest = publish(db)
    assert complete_daily_manifest(manifest, DAY, 418)
    real = db.query

    def metadata_only(sql, params=None):
        assert "chunk_text" not in sql
        return real(sql, params)

    db.query = metadata_only
    assert db.daily_active_manifest(DAY, 418) == manifest
    assert db.daily_active_manifest("2026-11-05", 418) is None


@pytest.mark.parametrize("damage", [
    "UPDATE generation_snapshot SET verified=0",
    "UPDATE generation_snapshot SET sha256='bad'",
    "UPDATE generation_snapshot SET byte_length=byte_length+1",
    "DELETE FROM generation_snapshot_chunk WHERE chunk_index=0",
    "UPDATE generation_snapshot_chunk SET chunk_index=chunk_index+1",
    "DELETE FROM generation_station WHERE code='TOS00000001'",
    "UPDATE generation_station SET complete=0 WHERE code='TOS00000001'",
    "UPDATE generation_runs SET manifest_json='bad'",
    "UPDATE generation_runs SET status='STAGING'",
    "UPDATE generation_runs SET scientific_total=417",
    "UPDATE generation_runs SET manifest_json=json_set(manifest_json,'$.coverage.forecast_15',417)",
    "UPDATE generation_runs SET manifest_json=json_set(manifest_json,'$.d1_verification.readback_verified',0)",
])
def test_broken_or_incomplete_active_is_not_accepted_as_today_ready(db, damage):
    publish(db)
    db.query(damage)
    assert db.daily_active_manifest(DAY, 418) is None


def test_second_daily_cli_attempt_has_no_provider_calls_or_snapshot(db, monkeypatch, tmp_path):
    active = publish(db)
    setup_cli(monkeypatch, tmp_path, db)

    def forbidden(*a, **k):
        pytest.fail("provider/catalog called after complete daily ACTIVE")

    monkeypatch.setattr(cli, "fetch_catalog", forbidden)
    assert cli.main() == 0
    receipt = json.loads((tmp_path / "daily-receipt.json").read_text())
    assert receipt["status"] == "DAILY_ALREADY_READY"
    assert receipt["generation"] == active["generation"]
    assert not (tmp_path / "snapshot.json").exists()


@pytest.mark.parametrize("day", ["2026-11-03", "2026-11-05"])
def test_expired_or_future_request_never_contacts_d1_or_providers(db, monkeypatch, tmp_path, day):
    setup_cli(monkeypatch, tmp_path, db, "--target-day", day)

    def forbidden(*a, **k):
        pytest.fail("expired request touched D1 or a provider")

    monkeypatch.setattr(cli, "D1", forbidden)
    assert cli.main() == 0
    assert json.loads((tmp_path / "daily-receipt.json").read_text())["status"] == "EXPIRED_REQUEST"


def test_midnight_during_d1_staging_preserves_previous_active(db, monkeypatch, tmp_path):
    publish(db, day="2026-11-03", generation="previous")
    setup_cli(monkeypatch, tmp_path, db)
    fake_successful_sources(monkeypatch, db)
    monkeypatch.setattr("funghi_r6.d1.rome_day", lambda: "2026-11-05")
    with pytest.raises(RuntimeError, match="richiesta scaduta durante lo staging"):
        cli.main()
    assert db.active_generation() == "previous"


@pytest.mark.parametrize("day", ["2026-2-03", "2026-02-30", "$(touch /tmp/no)", "", "2026-11-04T00:00:00"])
def test_target_day_is_strict_iso_calendar_date(day):
    with pytest.raises(ValueError):
        validate_day(day)


@pytest.mark.parametrize("payload", [None, [], {"daily": ["unexpected"]}, {"daily": {"time": [DAY], "precipitation_sum": 12}}])
def test_malformed_bootstrap_cache_is_excluded_instead_of_blocking_retry(payload):
    from funghi_r6.cache_policy import reusable_history_payload
    assert reusable_history_payload(CODES[0], payload, DAY, None) is False


def test_force_is_an_explicit_maintenance_escape(db, monkeypatch, tmp_path):
    publish(db)
    setup_cli(monkeypatch, tmp_path, db, "--force-rebuild")

    def catalogue_reached(*a, **k):
        raise RuntimeError("FORCED_BUILD_REACHED")

    monkeypatch.setattr(cli, "fetch_catalog", catalogue_reached)
    with pytest.raises(RuntimeError, match="FORCED_BUILD_REACHED"):
        cli.main()


def test_failed_acquisition_retains_previous_active_and_can_retry(db, monkeypatch, tmp_path):
    publish(db, day="2026-11-03", generation="yesterday")
    setup_cli(monkeypatch, tmp_path, db)
    calls = []

    def unavailable(*a, **k):
        calls.append("provider")
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(cli, "fetch_catalog", unavailable)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="source unavailable"):
            cli.main()
        assert db.active_generation() == "yesterday"
    assert calls == ["provider", "provider"]


def fake_successful_sources(monkeypatch, db):
    monkeypatch.setattr(cli, "fetch_catalog", lambda *a, **k: (CODES, {}))
    monkeypatch.setattr(cli, "fetch_official_rain", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cli, "fetch_component", lambda *a, **k: ({}, []))
    monkeypatch.setattr(cli, "daily_rows", lambda *a: [])
    monkeypatch.setattr(db, "load_history_map", lambda *a: {s["code"]: [{}] * 95 for s in CODES})
    monkeypatch.setattr(cli, "fetch_copernicus_bundle", lambda *a: ({}, {}))
    monkeypatch.setattr(cli, "assemble_station", lambda station, **k: dict(station))
    monkeypatch.setattr(cli, "coverage", lambda rows: {k: 418 for k in DAILY_COVERAGE})


def test_next_day_publishes_once_then_all_retries_reuse_exact_generation(db, monkeypatch, tmp_path):
    publish(db, day="2026-11-03", generation="yesterday")
    setup_cli(monkeypatch, tmp_path, db)
    fake_successful_sources(monkeypatch, db)
    assert cli.main() == 0
    active = db.daily_active_manifest(DAY, 418)
    assert active and active["generation"] != "yesterday"
    assert db.query("SELECT value FROM system_state WHERE key='ACTIVE_PREVIOUS'")[0]["results"][0]["value"] == "yesterday"
    assert json.loads((tmp_path / "daily-receipt.json").read_text())["status"] == "DAILY_PUBLISHED"
    assert cli.main() == 0
    assert db.daily_active_manifest(DAY, 418) == active
    assert json.loads((tmp_path / "daily-receipt.json").read_text())["status"] == "DAILY_ALREADY_READY"


def test_midnight_during_acquisition_never_promotes_yesterdays_candidate(db, monkeypatch, tmp_path):
    publish(db, day="2026-11-03", generation="previous")
    setup_cli(monkeypatch, tmp_path, db)
    fake_successful_sources(monkeypatch, db)
    days = iter([DAY, DAY, "2026-11-05"])
    # Explicit target: first read at admission, second after acquisition.
    monkeypatch.setattr(cli, "rome_day", lambda: next(days))
    assert cli.main() == 0
    assert db.active_generation() == "previous"
    assert json.loads((tmp_path / "daily-receipt.json").read_text())["status"] == "EXPIRED_REQUEST"


def test_valid_http_with_partial_forecast_is_refetched_selectively_on_next_attempt(db, monkeypatch, tmp_path):
    from datetime import date, timedelta
    from copy import deepcopy
    from funghi_r6.assemble import assemble_station, coverage
    from funghi_r6.cache_policy import reusable_component

    publish(db, day="2026-11-03", generation="previous")
    setup_cli(monkeypatch, tmp_path, db)
    cli.SETTINGS.expected_stations = 2
    stations = CODES[:2]
    cycle = DAY + "T00-full"
    dates = [(date.fromisoformat(DAY) + timedelta(days=i)).isoformat() for i in range(-2, 16)]
    forecast = {"daily": {"time": dates, "precipitation_sum": [1] * 18,
        "temperature_2m_mean": [15] * 18, "et0_fao_evapotranspiration": [1] * 18}}
    bad = deepcopy(forecast)
    bad["daily"]["precipitation_sum"][-1] = None  # Only +15 is missing; HTTP and JSON are valid.
    assert reusable_component(stations[0], "forecast", bad, DAY) is False
    assert reusable_component(stations[1], "forecast", forecast, DAY) is True
    current = {"current": {"temperature_2m": 15, "relative_humidity_2m": 80, "wind_speed_10m": 2}}
    soil = {"current": {"soil_moisture_3_to_9cm": 0.2, "soil_moisture_9_to_27cm": 0.3, "evapotranspiration": 1}}
    for component, payload in [("current", current), ("soil", soil), ("forecast", forecast)]:
        db.persist_component_batch(component, [{"code": st["code"], "payload": bad if component == "forecast" and i == 0 else payload,
                                               "cycle_key": cycle} for i, st in enumerate(stations)])
    history = [{"day": (date.fromisoformat(DAY) - timedelta(days=i)).isoformat(), "rain_mm": 1, "et0_mm": 1}
               for i in range(1, 96)]
    monkeypatch.setattr(cli, "fetch_catalog", lambda *a, **k: (stations, {}))
    monkeypatch.setattr(cli, "fetch_official_rain", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(db, "load_history_map", lambda *a: {s["code"]: history for s in stations})
    monkeypatch.setattr(cli, "fetch_copernicus_bundle", lambda *a: ({}, {}))
    monkeypatch.setattr(cli, "assemble_station", assemble_station)
    monkeypatch.setattr(cli, "coverage", coverage)
    fetched = []

    def fetch(http, targets, **kw):
        if not targets:
            return {}, []
        assert kw["component"] == "forecast"
        assert [s["code"] for s in targets] == [stations[0]["code"]]
        fetched.append(stations[0]["code"])
        rows = [{"code": stations[0]["code"], "component": "forecast", "payload": bad if len(fetched) == 1 else forecast,
                 "cycle_key": kw["cycle_key"]}]
        kw["on_batch"]("forecast", rows)
        return {r["code"]: r for r in rows}, []

    monkeypatch.setattr(cli, "fetch_component", fetch)
    with pytest.raises(SystemExit, match="incompleta"):
        cli.main()
    assert db.active_generation() == "previous"
    assert cli.main() == 0
    assert db.daily_active_manifest(DAY, 2)
    assert fetched == [stations[0]["code"]] * 2
