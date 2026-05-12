"""Tests for the /healthz and /healthz/deep endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """A TestClient with deterministic env so settings load cleanly."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-please")
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    # Make sure get_settings() cache picks up the env we just set.
    from utils.config import get_settings

    get_settings.cache_clear()

    import main

    return TestClient(main.app)


def test_healthz_returns_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_request_id_header_is_echoed(client):
    """RequestIDMiddleware must emit X-Request-ID on the response."""
    r = client.get("/healthz")
    assert "x-request-id" in {k.lower() for k in r.headers}


def test_request_id_header_is_honoured(client):
    """Inbound X-Request-ID is reused (for tracing through a gateway)."""
    r = client.get("/healthz", headers={"X-Request-ID": "abcd1234"})
    rid = r.headers.get("x-request-id") or r.headers.get("X-Request-ID")
    assert rid == "abcd1234"


def test_healthz_deep_returns_200_when_both_upstreams_ok(client):
    """Both Groq and Google APIs reachable -> 200 with status='ok'."""
    fake_resp = type("R", (), {"status_code": 200})()

    async def fake_get(self, url, headers=None):
        return fake_resp

    with patch("httpx.AsyncClient.get", new=fake_get):
        r = client.get("/healthz/deep")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["groq"]["ok"] is True
    assert body["checks"]["google_calendar_api"]["ok"] is True


def test_healthz_deep_returns_503_when_groq_is_unreachable(client):
    """Groq down -> overall status=degraded, HTTP 503."""
    import httpx

    async def first_call_fails(self, url, headers=None):
        if "groq" in url:
            raise httpx.ConnectError("nope")
        return type("R", (), {"status_code": 200})()

    with patch("httpx.AsyncClient.get", new=first_call_fails):
        r = client.get("/healthz/deep")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["groq"]["ok"] is False
    assert "unreachable" in body["checks"]["groq"]["detail"]


def test_healthz_deep_returns_503_when_groq_key_missing(monkeypatch, client):
    """No GROQ_API_KEY -> the deep check fails fast without an HTTP call."""
    monkeypatch.setenv("GROQ_API_KEY", "")
    from utils.config import get_settings

    get_settings.cache_clear()
    # Re-build the app so it picks up empty key
    import importlib

    import main

    importlib.reload(main)
    fresh_client = TestClient(main.app)

    fake_resp = type("R", (), {"status_code": 200})()

    async def fake_get(self, url, headers=None):
        return fake_resp

    with patch("httpx.AsyncClient.get", new=fake_get):
        r = fresh_client.get("/healthz/deep")
    assert r.status_code == 503
    assert "not configured" in r.json()["checks"]["groq"]["detail"]
    # restore env so subsequent tests aren't affected
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    get_settings.cache_clear()
