"""R6.55 regression: failed multi-point Open-Meteo batches recover point-by-point."""
from __future__ import annotations

import pytest
from funghi_r6 import open_meteo


def stations(n=12):
    return [{"code": f"ST{i:03d}", "lat": 40.0 + i / 10000, "lon": 10.0 + i / 10000}
            for i in range(n)]


class SimulatedHttp:
    def __init__(self, fail_when, malformed_when=lambda codes: False):
        self.fail_when = fail_when
        self.malformed_when = malformed_when
        self.calls = []

    def get_json(self, url, *, params):
        lats = params["latitude"].split(",")
        codes = [f"ST{round((float(lat)-40)*10000):03d}" for lat in lats]
        self.calls.append((url, tuple(codes)))
        if self.fail_when(codes):
            raise TimeoutError("simulated 35s timeout / five client retries")
        items = [{"latitude": float(lat), "value": len(codes)} for lat in lats]
        if self.malformed_when(codes):
            items = items[:-1]
        return items if len(items) != 1 else items[0]


@pytest.mark.parametrize("component", ["current", "forecast", "soil", "history_bootstrap"])
def test_split_recovers_twelve_after_one_batch_timeout(component, monkeypatch):
    monkeypatch.setattr(open_meteo.time, "sleep", lambda *_: None)
    http = SimulatedHttp(fail_when=lambda codes: len(codes) >= 12)
    stored = {}

    def persist(_component, rows):
        assert _component == component
        for row in rows:
            stored[row["code"]] = row

    result, errors = open_meteo.fetch_component(
        http, stations(), component=component, batch_size=12, pause_s=1.5,
        cycle_key="2026-10-15T00-full", on_batch=persist,
    )
    assert errors == []
    assert len(result) == len(stored) == 12
    assert len(http.calls) == 3, http.calls
    assert set(result) == {s["code"] for s in stations()}
    assert all(rec["cycle_key"] == "2026-10-15T00-full" for rec in result.values())


def test_isolate_just_one_permanently_broken_station(monkeypatch):
    monkeypatch.setattr(open_meteo.time, "sleep", lambda *_: None)
    bad = "ST004"
    http = SimulatedHttp(fail_when=lambda codes: bad in codes)
    result, errors = open_meteo.fetch_component(http, stations(), component="current", batch_size=12, pause_s=0)
    assert len(result) == 11
    assert bad not in result
    assert len(errors) == 1 and "ST004..ST004" in errors[0]
    assert len(http.calls) > 3


def test_malformed_batch_length_gets_recursively_recovered(monkeypatch):
    monkeypatch.setattr(open_meteo.time, "sleep", lambda *_: None)
    http = SimulatedHttp(fail_when=lambda _codes: False, malformed_when=lambda codes: len(codes) > 1)
    result, errors = open_meteo.fetch_component(http, stations(7), component="forecast", batch_size=7, pause_s=0)
    assert errors == [] and len(result) == 7
    assert len(http.calls) >= 7


def test_successful_results_survive_other_failed_batches(monkeypatch):
    monkeypatch.setattr(open_meteo.time, "sleep", lambda *_: None)
    http = SimulatedHttp(fail_when=lambda codes: "ST014" in codes)
    result, errors = open_meteo.fetch_component(http, stations(24), component="soil", batch_size=12, pause_s=0)
    assert len(result) == 23
    assert "ST014" not in result
    assert len(errors) == 1 and "ST014..ST014" in errors[0]


def test_storage_failure_propagates_and_never_marks_rows_acquired(monkeypatch):
    monkeypatch.setattr(open_meteo.time, "sleep", lambda *_: None)
    http = SimulatedHttp(fail_when=lambda _codes: False)
    def persist_failure(component, rows):
        raise OSError("D1 refused write")
    with pytest.raises(OSError, match="D1 refused write"):
        open_meteo.fetch_component(http, stations(4), component="forecast", batch_size=4,
                                  pause_s=0, on_batch=persist_failure)
    assert len(http.calls) == 1, "Do not confuse D1 write failures with transient HTTP errors"


def test_empty_targets_skips_http_calls():
    http = SimulatedHttp(fail_when=lambda _: True)
    result, errors = open_meteo.fetch_component(http, [], component="current", batch_size=12, pause_s=0)
    assert result == {} and errors == [] and http.calls == []
