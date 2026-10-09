from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import funghi_r6.http as http_mod
from funghi_r6.http import FetchError, HttpClient


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def get(self, *args, **kwargs):
        idx = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return FakeResponse(self.payloads[idx])


def _client(payloads, retries=3):
    client = HttpClient(max_retries=retries, timeout_s=1)
    fake = FakeSession(payloads)
    client.session = fake
    return client, fake


def test_json_200_malformed_then_valid_is_retried(monkeypatch):
    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    client, fake = _client([ValueError("truncated json"), {"ok": True}], retries=3)
    assert client.get_json("https://example.invalid") == {"ok": True}
    assert fake.calls == 2


def test_json_200_always_malformed_raises_fetcherror(monkeypatch):
    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    client, fake = _client([ValueError("bad json")], retries=3)
    try:
        client.get_json("https://example.invalid")
    except FetchError as exc:
        assert "JSON non valido dopo 3 tentativi" in str(exc)
    else:
        raise AssertionError("FetchError atteso")
    assert fake.calls == 3
