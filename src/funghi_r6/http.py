from __future__ import annotations

from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import random
import time
from typing import Any
import requests


class FetchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False,
                 reason: str = "request_error", status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason
        self.status_code = status_code


class HttpClient:
    def __init__(self, max_retries: int = 5, timeout_s: float | tuple[float, float] = 35.0):
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "FunghiToscana-R6-DataBuilder/1.0 (+https://github.com/giovanniaulicino-creator/funghi---toscana--r6)",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        })

    def _request(self, url, params, accept, *, decode_json=False,
                 max_attempts=None, timeout_s=None):
        attempts = self.max_retries if max_attempts is None else min(self.max_retries, max_attempts)
        last = None
        reason, status, retryable = "request_error", None, False
        for attempt in range(max(1, attempts)):
            response = None
            delay = min(45.0, 2 ** attempt + random.random())
            try:
                response = self.session.get(url, params=params,
                    timeout=self.timeout_s if timeout_s is None else timeout_s,
                    headers={"Accept": accept})
                status = response.status_code
                if status >= 400:
                    retryable = status in (408, 425, 429) or status >= 500
                    reason = "http_error"
                    last = f"HTTP {status}"
                    if not retryable:
                        break
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(0.0, float(retry_after))
                        except ValueError:
                            try:
                                until = parsedate_to_datetime(retry_after)
                                delay = max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError, OverflowError):
                                pass
                else:
                    response.raise_for_status()
                    if not decode_json:
                        return response
                    try:
                        return response.json()
                    except ValueError as exc:
                        reason, retryable, last = "invalid_json", True, exc
            except requests.Timeout as exc:
                reason, retryable, last, status = "timeout", True, exc, None
            except requests.ConnectionError as exc:
                reason, retryable, last, status = "connection_error", True, exc, None
            except requests.RequestException as exc:
                reason, retryable, last, status = "request_error", False, exc, None
                break
            finally:
                if response is not None and (decode_json or response.status_code >= 400):
                    close = getattr(response, "close", None)
                    if close:
                        close()
            if attempt + 1 < attempts:
                time.sleep(delay)
        label = "JSON non valido" if reason == "invalid_json" else "GET fallita"
        raise FetchError(f"{label} dopo {attempt + 1} tentativi: {url}: {last}",
                         retryable=retryable, reason=reason, status_code=status)

    def get(self, url: str, *, params: dict[str, Any] | None = None,
            accept: str = "application/json") -> requests.Response:
        return self._request(url, params, accept)

    def get_json(self, url: str, *, params: dict[str, Any] | None = None,
                 max_attempts: int | None = None,
                 timeout_s: float | tuple[float, float] | None = None) -> Any:
        # Transport and JSON parsing share one attempt budget.
        return self._request(url, params, "application/json", decode_json=True,
                             max_attempts=max_attempts, timeout_s=timeout_s)

    def get_text(self, url: str, *, params: dict[str, Any] | None = None) -> str:
        response = self.get(url, params=params, accept="text/html,text/plain;q=0.9,*/*;q=0.5")
        try:
            return response.text
        finally:
            response.close()
