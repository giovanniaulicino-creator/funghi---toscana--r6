from __future__ import annotations

import random
import time
from typing import Any
import requests


class FetchError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, max_retries: int = 5, timeout_s: float = 35.0):
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "FunghiToscana-R6-DataBuilder/1.0 (+https://github.com/giovanniaulicino-creator/funghi---toscana--r6)",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        })

    def get(self, url: str, *, params: dict[str, Any] | None = None, accept: str = "application/json") -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout_s,
                    headers={"Accept": accept},
                )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else min(90.0, 2 ** attempt * 2.0 + random.random() * 2.0)
                    time.sleep(delay)
                    continue
                if 500 <= response.status_code < 600:
                    time.sleep(min(45.0, 2 ** attempt + random.random()))
                    continue
                response.raise_for_status()
                return response
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(min(45.0, 2 ** attempt + random.random()))
        raise FetchError(f"GET fallita dopo {self.max_retries} tentativi: {url}: {last}")

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.get(url, params=params).json()

    def get_text(self, url: str, *, params: dict[str, Any] | None = None) -> str:
        return self.get(url, params=params, accept="text/html,text/plain;q=0.9,*/*;q=0.5").text
