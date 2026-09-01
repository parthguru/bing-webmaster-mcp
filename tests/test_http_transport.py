"""Tests for the Streamable HTTP transport: bearer auth and per-tenant API
key/allowlist routing.

MCP_AUTH_TOKEN/BWT_TENANTS-derived globals are read at import time (same
pattern as test_read_only_gate.py and test_site_allowlist.py), so each test
that needs a different env state must reload the `main` module after
setting the env var(s) via monkeypatch.
"""

import importlib
from unittest.mock import patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import main


def _reload(monkeypatch, *, auth_token: str = "", tenants: str = "", read_only: str = "true"):
    monkeypatch.setenv("MCP_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("BWT_TENANTS", tenants)
    monkeypatch.setenv("BWT_READ_ONLY", read_only)
    monkeypatch.setenv("BWT_ALLOWED_SITES", "")
    return importlib.reload(main)


TENANTS_JSON = (
    '{"tok_client1":{"api_key":"KEY1","allowed_sites":["https://client1.com"]},'
    '"tok_client2":{"api_key":"KEY2","allowed_sites":["https://client2.com","https://client2b.com"]}}'
)


# ---------------------------------------------------------------------------
# 1. Contextvar propagation, proven with a minimal standalone Starlette app
#    using the real BearerTenantMiddleware plus a dummy echo route.
# ---------------------------------------------------------------------------


def _build_echo_app(mod):
    async def echo(request):
        key = mod._request_api_key.get()
        sites = mod._request_allowed_sites.get()
        return JSONResponse(
            {
                "api_key": key,
                "allowed_sites": sorted(sites) if sites is not None else None,
            }
        )

    app = Starlette(routes=[Route("/echo", echo)])
    app.add_middleware(mod.BearerTenantMiddleware)
    return app


async def test_contextvar_propagation_tenant_and_admin_and_unknown(monkeypatch):
    mod = _reload(monkeypatch, auth_token="tok_admin", tenants=TENANTS_JSON)
    app = _build_echo_app(mod)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Tenant token 1: gets its own api_key + allowed_sites.
        r = await client.get("/echo", headers={"Authorization": "Bearer tok_client1"})
        assert r.status_code == 200
        body = r.json()
        assert body["api_key"] == "KEY1"
        assert body["allowed_sites"] == ["https://client1.com"]

        # Tenant token 2, called right after tenant token 1: must reflect
        # only its own values (catches shared-mutable-state leakage).
        r = await client.get("/echo", headers={"Authorization": "Bearer tok_client2"})
        assert r.status_code == 200
        body = r.json()
        assert body["api_key"] == "KEY2"
        assert body["allowed_sites"] == ["https://client2.com", "https://client2b.com"]

        # Back to tenant 1 again: still correct, not leaked from tenant 2.
        r = await client.get("/echo", headers={"Authorization": "Bearer tok_client1"})
        assert r.status_code == 200
        body = r.json()
        assert body["api_key"] == "KEY1"
        assert body["allowed_sites"] == ["https://client1.com"]

        # Admin (MCP_AUTH_TOKEN) token: falls through unscoped, contextvars
        # stay at their defaults (None) so _make_request uses the globals.
        r = await client.get("/echo", headers={"Authorization": "Bearer tok_admin"})
        assert r.status_code == 200
        body = r.json()
        assert body["api_key"] is None
        assert body["allowed_sites"] is None

        # Unknown token: 401, route never reached.
        r = await client.get("/echo", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

        # No token at all: 401.
        r = await client.get("/echo")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. Real app smoke test against mcp.streamable_http_app() + middleware.
# ---------------------------------------------------------------------------


async def test_health_check_no_auth_required(monkeypatch):
    mod = _reload(monkeypatch, auth_token="tok_admin")
    http_app = mod.mcp.streamable_http_app()
    http_app.add_middleware(mod.BearerTenantMiddleware)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http_app), base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.text == "ok"


async def test_mcp_endpoint_requires_auth(monkeypatch):
    mod = _reload(monkeypatch, auth_token="tok_admin")
    http_app = mod.mcp.streamable_http_app()
    http_app.add_middleware(mod.BearerTenantMiddleware)

    async with app_lifespan(http_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http_app), base_url="http://test") as client:
            # No auth header -> 401.
            r = await client.post("/mcp", json={})
            assert r.status_code == 401

            # Wrong bearer token -> 401.
            r = await client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401

            # Correct token -> clears the auth gate (not 401/403); the
            # request reaches the underlying MCP app, which then responds
            # with its own (non-auth) error for the incomplete JSON-RPC body.
            r = await client.post(
                "/mcp",
                json={},
                headers={
                    "Authorization": "Bearer tok_admin",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert r.status_code not in (401, 403)


class app_lifespan:
    """Runs a Starlette app's lifespan (startup/shutdown) around a block of
    requests. httpx.ASGITransport does not drive the lifespan protocol on
    its own, and the MCP streamable-http session manager requires its
    lifespan startup to have run before it will handle a request."""

    def __init__(self, app):
        self._app = app
        self._ctx = None

    async def __aenter__(self):
        self._ctx = self._app.router.lifespan_context(self._app)
        await self._ctx.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        await self._ctx.__aexit__(*exc_info)


async def test_tenant_key_reaches_tool_execution(monkeypatch):
    """The echo-route test above proves contextvars survive the
    BaseHTTPMiddleware dispatch -> downstream-ASGI hop. It does NOT prove
    they reach a *tool's* execution: the streamable-HTTP session manager
    runs a session across multiple requests (initialize, then tools/call)
    under a long-lived task group created at lifespan startup, so a tool
    body might run in the context captured at session-creation time rather
    than the current request's context. This drives a real tools/call
    through the actual MCP app and asserts the contextvar value observed
    *inside* BingWebmasterAPI._make_request during that call.
    """
    mod = _reload(monkeypatch, auth_token="", tenants=TENANTS_JSON)
    seen = {}

    async def fake_make_request(self, endpoint, method="GET", json_data=None, params=None):
        seen["api_key"] = mod._request_api_key.get()
        seen["allowed_sites"] = mod._request_allowed_sites.get()
        return []

    monkeypatch.setattr(mod.BingWebmasterAPI, "_make_request", fake_make_request)

    mod.mcp.settings.json_response = True  # plain JSON responses, no SSE parsing needed
    http_app = mod.mcp.streamable_http_app()
    http_app.add_middleware(mod.BearerTenantMiddleware)

    async with app_lifespan(http_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http_app), base_url="http://test") as client:
            headers = {
                "Authorization": "Bearer tok_client1",
                "Accept": "application/json, text/event-stream",
            }
            r = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert r.status_code == 200
            headers["Mcp-Session-Id"] = r.headers["mcp-session-id"]

            r = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert r.status_code == 202

            r = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_sites", "arguments": {}},
                },
            )
            assert r.status_code == 200

    # get_sites has no site param, so this isolates key routing from the
    # allowlist check — the tool body genuinely ran with tenant 1's context.
    assert seen["api_key"] == "KEY1"
    assert seen["allowed_sites"] == frozenset({"https://client1.com"})


# ---------------------------------------------------------------------------
# 3. BWT_TENANTS parsing validation.
# ---------------------------------------------------------------------------


def test_tenants_malformed_json_raises(monkeypatch):
    monkeypatch.setenv("BWT_TENANTS", "{not valid json")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    monkeypatch.setenv("BWT_READ_ONLY", "true")
    monkeypatch.setenv("BWT_ALLOWED_SITES", "")
    with pytest.raises(ValueError, match="not valid JSON"):
        importlib.reload(main)


def test_tenants_missing_api_key_raises(monkeypatch):
    monkeypatch.setenv(
        "BWT_TENANTS", '{"tok1":{"allowed_sites":["https://a.com"]}}'
    )
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    monkeypatch.setenv("BWT_READ_ONLY", "true")
    monkeypatch.setenv("BWT_ALLOWED_SITES", "")
    with pytest.raises(ValueError, match="api_key"):
        importlib.reload(main)


def test_tenants_empty_allowed_sites_raises(monkeypatch):
    monkeypatch.setenv(
        "BWT_TENANTS", '{"tok1":{"api_key":"KEY1","allowed_sites":[]}}'
    )
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    monkeypatch.setenv("BWT_READ_ONLY", "true")
    monkeypatch.setenv("BWT_ALLOWED_SITES", "")
    with pytest.raises(ValueError, match="allowed_sites"):
        importlib.reload(main)


def test_tenants_invalid_site_url_raises(monkeypatch):
    monkeypatch.setenv(
        "BWT_TENANTS", '{"tok1":{"api_key":"KEY1","allowed_sites":["not-a-url"]}}'
    )
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    monkeypatch.setenv("BWT_READ_ONLY", "true")
    monkeypatch.setenv("BWT_ALLOWED_SITES", "")
    with pytest.raises(ValueError, match="scheme or host"):
        importlib.reload(main)


# ---------------------------------------------------------------------------
# 4. run_http() fails closed when no auth is configured.
# ---------------------------------------------------------------------------


def test_require_http_auth_configured_fails_closed(monkeypatch):
    mod = _reload(monkeypatch, auth_token="", tenants="")
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN and/or BWT_TENANTS"):
        mod._require_http_auth_configured()


def test_require_http_auth_configured_passes_with_auth_token(monkeypatch):
    mod = _reload(monkeypatch, auth_token="tok_admin", tenants="")
    mod._require_http_auth_configured()  # must not raise


def test_require_http_auth_configured_passes_with_tenants(monkeypatch):
    mod = _reload(monkeypatch, auth_token="", tenants=TENANTS_JSON)
    mod._require_http_auth_configured()  # must not raise


def test_run_http_wires_uvicorn_and_middleware(monkeypatch):
    """run_http() itself is otherwise unexercised: prove it builds the app,
    attaches the middleware, configures uvicorn with the right host/port/
    log level, and calls .run() — without actually binding a socket."""
    mod = _reload(monkeypatch, auth_token="tok_admin", tenants="")

    with patch("uvicorn.Config") as mock_config_cls, patch("uvicorn.Server") as mock_server_cls:
        mod.run_http()

    assert mock_config_cls.call_count == 1
    _, kwargs = mock_config_cls.call_args
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8080
    assert kwargs["log_level"] == "info"

    app_arg = mock_config_cls.call_args[0][0]
    assert any(
        getattr(m, "cls", None) is mod.BearerTenantMiddleware for m in app_arg.user_middleware
    ), "BearerTenantMiddleware should be attached to the app passed to uvicorn.Config"

    mock_server_cls.assert_called_once_with(mock_config_cls.return_value)
    mock_server_cls.return_value.run.assert_called_once_with()


def test_run_http_fails_closed_without_binding(monkeypatch):
    mod = _reload(monkeypatch, auth_token="", tenants="")
    with patch("uvicorn.Config") as mock_config_cls, patch("uvicorn.Server") as mock_server_cls:
        with pytest.raises(ValueError, match="MCP_AUTH_TOKEN and/or BWT_TENANTS"):
            mod.run_http()
    mock_config_cls.assert_not_called()
    mock_server_cls.assert_not_called()


# ---------------------------------------------------------------------------
# 5. stdio path unaffected: app()'s stdio branch still calls
#    mcp.run(transport="stdio") with nothing else in that branch.
# ---------------------------------------------------------------------------


def test_stdio_path_calls_mcp_run_only(monkeypatch):
    monkeypatch.setenv("BING_WEBMASTER_API_KEY", "dummy")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("BWT_READ_ONLY", "true")
    mod = importlib.reload(main)

    with patch.object(mod, "run_http") as mock_run_http, patch.object(mod.mcp, "run") as mock_run:
        mod.app()
        mock_run.assert_called_once_with(transport="stdio")
        mock_run_http.assert_not_called()


def test_stdio_is_default_transport(monkeypatch):
    monkeypatch.setenv("BING_WEBMASTER_API_KEY", "dummy")
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    monkeypatch.setenv("BWT_READ_ONLY", "true")
    mod = importlib.reload(main)

    with patch.object(mod.mcp, "run") as mock_run:
        mod.app()
        mock_run.assert_called_once_with(transport="stdio")


def test_invalid_transport_raises(monkeypatch):
    monkeypatch.setenv("BING_WEBMASTER_API_KEY", "dummy")
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    monkeypatch.setenv("BWT_READ_ONLY", "true")
    mod = importlib.reload(main)

    with pytest.raises(ValueError, match="Invalid MCP_TRANSPORT"):
        mod.app()
