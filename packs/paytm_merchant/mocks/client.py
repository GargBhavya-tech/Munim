"""Thin client for the mocks. The executor (#25) is the only production caller; tests use it directly."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class MockTimeout(Exception):
    """The call may or may not have taken effect. Retry with the SAME idempotency key."""


class MockServerError(Exception):
    """The call failed before taking effect (HTTP 5xx)."""


@dataclass(frozen=True)
class MockResponse:
    status: int
    body: Any
    replayed: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class MockClient:
    def __init__(self, http):
        self.http = http  # an httpx.Client (or fastapi TestClient)

    def _wrap(self, r) -> MockResponse:
        if r.status_code == 504:
            raise MockTimeout(r.text)
        if r.status_code >= 500:
            raise MockServerError(r.text)
        return MockResponse(r.status_code, r.json(), r.headers.get("Idempotent-Replay") == "true")

    def get(self, path: str, **params) -> MockResponse:
        return self._wrap(self.http.get(path, params=params or None))

    def write(self, method: str, path: str, *, key: str, json: dict | None = None) -> MockResponse:
        return self._wrap(self.http.request(method, path, json=json, headers={"Idempotency-Key": key}))

    def write_with_retry(self, method: str, path: str, *, key: str, json: dict | None = None, attempts: int = 30) -> tuple[MockResponse, int]:
        """Retry timeouts and 5xx with the SAME key until one attempt returns. Returns (response, attempts_used)."""
        last: Exception | None = None
        for i in range(1, attempts + 1):
            try:
                return self.write(method, path, key=key, json=json), i
            except (MockTimeout, MockServerError) as e:
                last = e
        raise RuntimeError(f"gave up after {attempts} attempts: {last}")
