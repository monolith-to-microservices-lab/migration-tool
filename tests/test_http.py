"""HTTP client: retry transient errors, never retry deterministic ones."""

from __future__ import annotations

import httpx
import pytest

from migration_tool.http import HttpError, RetryingClient


def _client(handler, **kw):
    return RetryingClient(
        "http://svc.test",
        transport=httpx.MockTransport(handler),
        backoff_base=0.0,
        sleep=lambda _s: None,
        **kw,
    )


def test_retries_transient_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"detail": "later"})
        return httpx.Response(201, json={"ok": True})

    r = _client(handler, max_retries=3).request("POST", "/internal/x/import", json={})
    assert r.status_code == 201
    assert calls["n"] == 3


def test_does_not_retry_409():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(409, json={"detail": "conflict"})

    r = _client(handler, max_retries=3).request("POST", "/internal/x/import", json={})
    assert r.status_code == 409
    assert calls["n"] == 1


def test_does_not_retry_422():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(422, json={"detail": "bad"})

    r = _client(handler, max_retries=3).request("POST", "/x", json={})
    assert r.status_code == 422
    assert calls["n"] == 1


def test_gives_up_after_max_retries_on_persistent_5xx():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500)

    c = _client(handler, max_retries=2)
    r = c.request("GET", "/health")
    # last response returned (not raised) so callers can inspect it
    assert r.status_code == 500
    assert calls["n"] == 3  # 1 + 2 retries


def test_raises_on_transport_error():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(HttpError):
        _client(handler, max_retries=1).request("GET", "/health")


def test_retries_transient_timeout_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.TimeoutException("read timed out", request=request)
        return httpx.Response(201, json={"ok": True})

    r = _client(handler, max_retries=3).request("POST", "/internal/x/import", json={})
    assert r.status_code == 201
    assert calls["n"] == 2


def test_gives_up_after_max_retries_on_persistent_timeout():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.TimeoutException("read timed out", request=request)

    with pytest.raises(HttpError):
        _client(handler, max_retries=2).request("GET", "/health")
    assert calls["n"] == 3  # 1 + 2 retries
