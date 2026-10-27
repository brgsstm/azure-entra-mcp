"""Pins the FastMCP internals that auth.py and server.py rely on.

``ManagedIdentityAzureProvider`` overrides private FastMCP hooks (``fastmcp<5`` is pinned
for this reason). If one of these tests fails after a FastMCP upgrade, re-check the
override it names in auth.py against the new FastMCP source before changing the test.
"""

from __future__ import annotations

import inspect
from collections import OrderedDict
from types import SimpleNamespace

import fastmcp.server.dependencies as fastmcp_deps
import httpx2
import pytest
from fastmcp.server.auth.oauth_proxy.proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.upstream import AsyncOAuth2Client
from fastmcp.server.auth.providers.azure import AzureProvider, EntraOBOToken
from test_auth import APP, _settings  # tests/ is on sys.path under pytest

from azure_entra_mcp import auth as auth_mod
from azure_entra_mcp import clients as clients_mod
from azure_entra_mcp.auth import (
    ClientAssertionOAuth2Client,
    ManagedIdentityAzureProvider,
    build_auth,
)
from azure_entra_mcp.server import build_http_app


@pytest.fixture
def rotating_mi(monkeypatch):
    """Fake MI assertion that changes on every fetch, counting the fetches."""
    calls = []

    async def fake_get(self):
        calls.append(1)
        self._token = f"mi-assertion-{len(calls)}"
        return self._token

    monkeypatch.setattr(auth_mod.ManagedIdentityAssertion, "get", fake_get)
    return calls


def _capture_body(client: AsyncOAuth2Client) -> list[str]:
    bodies: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content.decode())
        return httpx2.Response(200, json={"access_token": "a", "expires_in": 3600})

    client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return bodies


# --- Upstream token client (code exchange / refresh) ---------------------------------


def test_overridden_hooks_still_exist_upstream():
    # If FastMCP renames these, our overrides silently become dead code.
    assert "_create_upstream_oauth_client" in OAuthProxy.__dict__
    assert "get_obo_credential" in AzureProvider.__dict__
    for name in ("_apply_client_auth", "fetch_token", "refresh_token"):
        assert name in AsyncOAuth2Client.__dict__, name


async def test_proxy_builds_upstream_client_through_our_hook(rotating_mi):
    provider = build_auth(_settings())
    async with provider._upstream_oauth_client() as client:
        assert isinstance(client, ClientAssertionOAuth2Client)
    assert rotating_mi == []  # building the client must not need the assertion yet


async def test_refresh_sends_fresh_assertion_each_request(rotating_mi):
    provider = build_auth(_settings())
    async with provider._upstream_oauth_client() as client:
        bodies = _capture_body(client)
        await client.refresh_token("https://login.example/token", refresh_token="rt")
        await client.fetch_token("https://login.example/token", code="c")

    assert "grant_type=refresh_token" in bodies[0]
    assert "client_assertion=mi-assertion-1" in bodies[0]
    assert "client_assertion=mi-assertion-2" in bodies[1]
    assert all("client_secret" not in b for b in bodies)


# --- OBO --------------------------------------------------------------------------


def test_obo_private_state_shape():
    provider = build_auth(_settings())
    assert isinstance(provider._obo_credentials, OrderedDict)
    assert isinstance(provider._obo_max_credentials, int)
    for name in ("_tenant_id", "_upstream_client_id", "_base_authority"):
        assert isinstance(getattr(provider, name), str), name
    assert provider._upstream_client_id == APP


async def test_obo_cache_eviction_closes_credentials(rotating_mi):
    provider = build_auth(_settings())
    provider._obo_max_credentials = 1
    first = await provider.get_obo_credential("user-1")
    closed = []

    async def fake_close():
        closed.append(first)

    first.close = fake_close
    await provider.get_obo_credential("user-2")

    assert len(provider._obo_credentials) == 1
    assert closed == [first]
    await provider.close_obo_credentials()


async def test_entra_obo_token_dependency_calls_our_override(rotating_mi, monkeypatch):
    provider = build_auth(_settings())
    seen = {}

    async def spy(**kwargs):
        # Keyword-only call: FastMCP passes user_assertion=..., so our parameter name
        # must keep matching.
        real = await ManagedIdentityAzureProvider.get_obo_credential(provider, **kwargs)
        seen["credential"] = real
        return SimpleNamespace(get_token=_fake_get_token)

    async def _fake_get_token(*scopes):
        seen["scopes"] = scopes
        return SimpleNamespace(token="downstream-token")

    monkeypatch.setattr(provider, "get_obo_credential", spy)
    monkeypatch.setattr(
        fastmcp_deps, "get_access_token", lambda: SimpleNamespace(token="user-entra-token")
    )
    monkeypatch.setattr(fastmcp_deps, "get_server", lambda: SimpleNamespace(auth=provider))

    dependency = EntraOBOToken(["https://graph.microsoft.com/.default"])
    assert await dependency.__aenter__() == "downstream-token"
    assert seen["scopes"] == ("https://graph.microsoft.com/.default",)
    assert list(provider._obo_credentials.values()) == [seen["credential"]]
    assert (
        "user_assertion"
        in inspect.signature(ManagedIdentityAzureProvider.get_obo_credential).parameters
    )
    await provider.close_obo_credentials()


# --- HTTP transport Origin guard (server.py) ----------------------------------------


def _http_client(base_url: str) -> httpx2.AsyncClient:
    settings = _settings(client_secret="local-secret", managed_identity_client_id=None)
    app = build_http_app(settings)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=base_url)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://evil.example", 403),  # spec: present + untrusted Origin => 403
        ("https://mcp.example.com", 401),  # our own origin passes the guard, then auth
        (None, 401),  # non-browser MCP clients send no Origin
    ],
)
async def test_origin_guard_on_mcp_endpoint(origin, expected):
    headers = dict(MCP_HEADERS, **({"origin": origin} if origin else {}))
    async with _http_client("https://mcp.example.com") as client:
        response = await client.post("/mcp", json=INIT, headers=headers)
    assert response.status_code == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "http://10.0.0.5:8000",  # ACA liveness/readiness probes hit the pod IP
        "http://127.0.0.1:8000",  # Docker HEALTHCHECK
    ],
)
async def test_health_probes_pass_host_guard(base_url):
    async with _http_client(base_url) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200


async def test_app_shutdown_closes_shared_http_client():
    # FastMCP must run the server lifespan inside the HTTP app's ASGI lifespan.
    settings = _settings(client_secret="local-secret", managed_identity_client_id=None)
    app = build_http_app(settings)
    async with app.router.lifespan_context(app):
        client = clients_mod.http()
        assert not client.is_closed
    assert client.is_closed
    assert clients_mod._client is None
