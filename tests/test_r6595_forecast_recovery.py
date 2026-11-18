"""Exercise production HTTP, probe, cache and recovery with controlled failures."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
import requests

from funghi_r6 import cli, forecast_readiness as readiness, open_meteo
from funghi_r6.http import FetchError, HttpClient
from test_r6584_daily import DAY, CODES, db, publish, setup_cli
from test_r6587_provider_guard import forecast


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)

    def raise_for_status(self):
        assert self.status_code < 400

    def close(self):
        self.closed = True


class Session:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.answer(url, kwargs, len(self.calls))
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def pauses(monkeypatch):
    sleeps = []
    monkeypatch.setattr("funghi_r6.http.time.sleep", sleeps.append)
    return sleeps


def http_with(answer):
    http = HttpClient(max_retries=5)
    http.session = Session(answer)
    return http


def test_transport_and_bad_json_share_one_retry_budget(pauses):
    answers = [Response(ValueError("truncated")), requests.ReadTimeout("timeout"),
               Response(ValueError("truncated")), requests.ConnectionError("reset"),
               requests.ReadTimeout("timeout")]
    http = http_with(lambda url, options, count: answers[count-1])
    with pytest.raises(FetchError) as error:
        http.get_json("https://example.invalid")
    assert error.value.reason == "timeout" and error.value.retryable
    assert len(http.session.calls) == 5
    assert answers[0].closed and answers[2].closed
    assert len(pauses) == 4


@pytest.mark.parametrize("status,retryable,attempts", [(400, False, 1), (401, False, 1),
    (403, False, 1), (404, False, 1), (429, True, 2), (503, True, 2)])
def test_status_classification_and_retry_after(status, retryable, attempts, pauses):
    responses = []
    def answer(*args):
        response = Response({}, status, {"Retry-After": "7"})
        responses.append(response)
        return response
    http = http_with(answer)
    with pytest.raises(FetchError) as error:
        http.get_json("https://example.invalid", max_attempts=2)
    assert error.value.status_code == status and error.value.retryable is retryable
    assert len(http.session.calls) == attempts
    assert all(response.closed for response in responses)
    assert pauses == ([7] if retryable else [])


@pytest.mark.parametrize("kind", ["timeout", "connection_error", "rate_limit", "server_error"])
@pytest.mark.parametrize("hour,forced,waiting", [(5,False,True), (6,False,False), (5,True,False)])
def test_actual_transport_failure_waits_with_evidence_until_deadline(db, monkeypatch, tmp_path,
                                                                   pauses, kind, hour, forced, waiting):
    old = publish(db, day="2026-11-03", generation="yesterday")
    args = ["--provider-readiness-guard"] + (["--force-rebuild"] if forced else [])
    setup_cli(monkeypatch, tmp_path, db, *args)
    instant = datetime.fromisoformat(DAY + f"T{hour:02d}:30:00+00:00")
    monkeypatch.setattr(readiness, "utc_now", lambda: instant)
    monkeypatch.setattr(cli, "fetch_catalog", lambda *a, **k: (CODES, {}))
    responses = {"timeout":requests.ReadTimeout("35s"),
                 "connection_error":requests.ConnectionError("reset"),
                 "rate_limit":Response({},429), "server_error":Response({},503)}
    http = http_with(lambda *a: responses[kind])
    monkeypatch.setattr(cli, "HttpClient", lambda **k: http)
    monkeypatch.setattr(cli, "fetch_official_rain", lambda *a, **k: pytest.fail("Must wait before full acquisition"))
    if waiting:
        assert cli.main() == 0
    else:
        with pytest.raises(SystemExit, match="forecast preliminare incompleto"):
            cli.main()
    result = json.loads((tmp_path / "daily-receipt.json").read_text())
    assert result["status"] == ("WAITING_FORECAST_TRANSPORT" if waiting else "BLOCKED_FORECAST_PREFLIGHT")
    detail = result["forecast_readiness"]
    assert detail["issues"][0]["reason"] != "invalid_daily_object"
    assert detail["transport_failures"][0]["retryable"] is True
    assert db.active_generation() == "yesterday"
    assert db.daily_active_manifest("2026-11-03",418) == old
    assert len(http.session.calls) <= 2
    for url, options in http.session.calls:
        assert set(options["params"]["daily"].split(",")) == set(readiness.FIELDS)
        assert options["params"]["forecast_days"] == "16"
        assert options["timeout"] == (5,20)
    assert not (tmp_path / "snapshot.json").exists()
    assert db.load_component_map("forecast",DAY+"T00-full") == {}


@pytest.mark.parametrize("payload,status", [(forecast(),401), ({"daily": []},200), (ValueError("json"),200)])
def test_bad_schema_json_or_credentials_still_block(db, monkeypatch, tmp_path, pauses, payload, status):
    http = http_with(lambda *a:Response(payload,status))
    monkeypatch.setattr(readiness,"utc_now",lambda:datetime.fromisoformat(DAY+"T01:00:00+00:00"))
    with pytest.raises(SystemExit):
        setup_cli(monkeypatch,tmp_path,db,"--provider-readiness-guard")
        monkeypatch.setattr(cli,"HttpClient",lambda **k:http)
        monkeypatch.setattr(cli,"fetch_catalog",lambda *a,**k:(CODES,{}))
        cli.main()
    assert json.loads((tmp_path/"daily-receipt.json").read_text())["status"]=="BLOCKED_FORECAST_PREFLIGHT"


def full_forecast():
    value = forecast()
    value.update(latitude=43.0,longitude=11.0,elevation=300,utc_offset_seconds=3600)
    for index, field in enumerate(open_meteo.DAILY_VARS.split(",")):
        value["daily"].setdefault(field, [index + day/10 for day in range(15)])
    value["daily"]["temperature_2m_min"][4] = None
    value["daily_units"] = {field:"fixture" for field in value["daily"]}
    return value


def test_probe_recovers_then_full_fetch_keeps_all_optional_fields(db, monkeypatch, tmp_path, pauses):
    original = full_forecast()

    def answer(url, options, count):
        if count == 1:
            return requests.ReadTimeout("first attempt")
        fields = options["params"]["daily"].split(",")
        payload = deepcopy(original)
        payload["daily"] = {"time": payload["daily"]["time"],
                            **{field: payload["daily"][field] for field in fields}}
        return Response(payload)

    http = http_with(answer)
    monkeypatch.setattr(readiness, "utc_now", lambda: datetime.fromisoformat(DAY + "T05:00:00+00:00"))
    status, cached = cli._forecast_preflight(http, db, CODES, DAY + "T00-full", DAY, tmp_path, forced=False)
    assert status is None and cached == {}
    assert db.load_component_map("forecast", DAY + "T00-full") == {}
    records, errors = open_meteo.fetch_component(http, CODES[:1], component="forecast", batch_size=1,
        pause_s=0, cycle_key=DAY + "T00-full", on_batch=db.persist_component_batch)
    assert not errors and len(http.session.calls) == 3
    assert records[CODES[0]["code"]]["payload"]["daily"] == original["daily"]
    stored = db.load_component_map("forecast", DAY + "T00-full")
    assert stored[CODES[0]["code"]]["payload"]["daily"]["temperature_2m_min"] == original["daily"]["temperature_2m_min"]


@pytest.mark.parametrize("corruption",[None,"grid","dates","short_series","still_offline"])
def test_single_point_recovers_all_variables_with_strict_grid_dates_and_nulls(pauses,corruption):
    original = full_forecast()
    def answer(url, options, count):
        fields = options["params"]["daily"].split(",")
        if len(fields)>6 or corruption=="still_offline":
            return requests.ReadTimeout("large request")
        payload = deepcopy(original)
        payload["daily"] = {"time":payload["daily"]["time"],**{f:payload["daily"][f] for f in fields}}
        payload["daily_units"] = {"time":"iso8601",**{f:original["daily_units"][f] for f in fields}}
        if "wind_speed_10m_max" in fields:
            if corruption=="grid":payload["latitude"]=43.1
            if corruption=="dates":payload["daily"]["time"][0]="2000-01-01"
            if corruption=="short_series":payload["daily"][fields[0]]=[]
        return Response(payload)
    http = http_with(answer)
    persisted = []
    records, errors = open_meteo.fetch_component(http,CODES[:1],component="forecast",batch_size=1,
        pause_s=0,cycle_key=DAY+"T00-full",on_batch=lambda c,r:persisted.extend(r))
    if corruption:
        assert not records and not persisted and len(errors)==1
    else:
        assert not errors and len(persisted)==1
        result=records[CODES[0]["code"]]["payload"]
        assert result["daily"]==original["daily"]
        assert result["daily"]["temperature_2m_min"][4] is None
        assert readiness.forecast_issues(result,DAY)==[]
        assert len(http.session.calls)==8 # five initial attempts, three smaller requests


@pytest.mark.parametrize("status",[400,401,429])
def test_rate_limit_or_permanent_error_does_not_explode_into_coordinate_splits(pauses,status):
    http=http_with(lambda *a:Response({},status))
    records,errors=open_meteo.fetch_component(http,CODES[:12],component="forecast",batch_size=12,pause_s=0)
    assert records=={} and len(errors)==1
    assert len(http.session.calls)==(5 if status==429 else 1)
