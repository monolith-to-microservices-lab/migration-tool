"""Shared HTTP client: explicit timeout, bounded retries, small backoff.

Retry policy
------------
Retried (transient):   connection errors, timeouts, HTTP 429, HTTP >= 500
NOT retried (final):    every other 4xx - notably 409 (conflict) and 422
                        (validation). These are deterministic; retrying cannot
                        change the answer.

Because the import endpoints are idempotent, retrying a POST /import is safe.
"""

from __future__ import annotations

import time

import httpx

from .logging_config import get_logger

logger = get_logger("migration_tool.http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    """Raised when a request ultimately fails (after exhausting retries)."""

    def __init__(self, message: str, *, attempts: int, last_status: int | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status


class RetryingClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        max_retries: int = 3,
        backoff_base: float = 0.2,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "migration-tool/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RetryingClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.max_retries:
            attempt += 1
            started = time.perf_counter()
            try:
                response = self._client.request(method, path, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                logger.warning(
                    "http.attempt",
                    extra={
                        "method": method,
                        "path": path,
                        "attempt": attempt,
                        "result": "transport_error",
                        "error": str(exc),
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
                if attempt > self.max_retries:
                    break
                self._backoff(attempt)
                continue

            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if response.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                logger.warning(
                    "http.attempt",
                    extra={
                        "method": method,
                        "path": path,
                        "attempt": attempt,
                        "result": "retryable_status",
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                    },
                )
                self._backoff(attempt)
                continue

            logger.info(
                "http.attempt",
                extra={
                    "method": method,
                    "path": path,
                    "attempt": attempt,
                    "result": "response",
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            return response

        raise HttpError(
            f"{method} {path} failed after {attempt} attempt(s): {last_exc}",
            attempts=attempt,
            last_status=None,
        )

    def _backoff(self, attempt: int) -> None:
        # small exponential backoff, capped
        delay = min(self.backoff_base * (2 ** (attempt - 1)), 2.0)
        self._sleep(delay)
