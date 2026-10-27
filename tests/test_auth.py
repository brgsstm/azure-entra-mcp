"""Tests for the managed-identity auth path and helpers. No network or Azure needed."""

from __future__ import annotations

import httpx2
import pytest
from fastmcp.exceptions import ToolError

from azure_entra_mcp import auth as auth_mod
from azure_entra_mcp import clients as clients_mod
from azure_entra_mcp.auth import (
    JWT_BEARER_ASSERTION_TYPE,
    ClientAssertionOAuth2Client,
    ManagedIdentityAzureProvider,
    TableKeySanitizer,
    build_auth,
)
from azure_entra_mcp.clients import graph_list, graph_search_clause, parse_resource_id
from azure_entra_mcp.config import Settings

TENANT = "11111111-1111-1111-1111-111111111111"
APP = "22222222-2222-2222-2222-222222222222"
UAMI = "33333333-3333-3333-3333-333333333333"


def _settings(**overrides) -> Settings:
    base = dict(
        tenant_id=TENANT,
        app_client_id=APP,
        public_base_url="https://mcp.example.com",
        managed_identity_client_id=UAMI,
        jwt_signing_key="k" * 48,
        storage_encryption_key="e" * 48,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def fake_mi(monkeypatch):
    async def fake_get(self):
        self._token = "mi-assertion-jwt"
        return self._token

    monkeypatch.setattr(auth_mod.ManagedIdentityAssertion, "get", fake_get)


def test_build_auth_selects_mode():
    assert isinstance(build_auth(_settings()), ManagedIdentityAzureProvider)
    secret_mode = build_auth(_settings(client_secret="s", managed_identity_client_id=None))
    assert not isinstance(secret_mode, ManagedIdentityAzureProvider)


async def test_upstream_client_sends_client_assertion(fake_mi):
    provider = build_auth(_settings())
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = request.content.decode()
        captured["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, json={"access_token": "a", "expires_in": 3600})

    async with provider._upstream_oauth_client() as client:
        assert isinstance(client, ClientAssertionOAuth2Client)
        client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        await client.fetch_token(
            "https://login.example/token",
            code="abc",
            redirect_uri="https://mcp.example.com/auth/callback",
        )

    body = captured["body"]
    assert "client_assertion=mi-assertion-jwt" in body
    assert f"client_id={APP}" in body
    assert "client_secret" not in body
    assert captured["auth"] is None  # no basic auth header
    assert JWT_BEARER_ASSERTION_TYPE.replace(":", "%3A") in body


async def test_obo_credential_uses_assertion_and_is_cached(fake_mi):
    provider = build_auth(_settings())
    cred1 = await provider.get_obo_credential("user-token-1")
    cred2 = await provider.get_obo_credential("user-token-1")
    cred3 = await provider.get_obo_credential("user-token-2")
    assert cred1 is cred2
    assert cred1 is not cred3
    assert provider._mi_assertion.current() == "mi-assertion-jwt"
    await provider.close_obo_credentials()


@pytest.mark.parametrize(
    "key", ["https://claude.ai/oauth/claude-code-client-metadata", "H_abc", "S_x"]
)
def test_table_key_sanitizer_hashes_unsafe_and_reserved_keys(key):
    s = TableKeySanitizer()
    s.validate(key)  # never rejects
    out = s.sanitize(key)
    assert out.startswith("H_")
    assert len(out) == 66
    assert out == s.sanitize(key)
    assert out != s.sanitize(key + "x")


def test_table_key_sanitizer_keeps_safe_keys():
    key = "9f8b6c1e-0000-4000-8000-123456789abc"
    assert TableKeySanitizer().sanitize(key) == key


def test_settings_require_fixed_keys_with_shared_store(monkeypatch):
    for k, v in {
        "AZURE_TENANT_ID": TENANT,
        "ENTRA_APP_CLIENT_ID": APP,
        "PUBLIC_BASE_URL": "https://x",
        "MANAGED_IDENTITY_CLIENT_ID": UAMI,
        "TOKEN_STORE_ACCOUNT": "stacct",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SIGNING_KEY"):
        Settings.from_env()


SUB = "44444444-4444-4444-4444-444444444444"


@pytest.mark.parametrize(
    ("rid", "expected"),
    [
        (f"/subscriptions/{SUB}", (SUB, None, None, None)),
        (f"/subscriptions/{SUB}/resourceGroups/rg1", (SUB, "rg1", None, None)),
        (
            f"/subscriptions/{SUB}/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/st1",
            (SUB, "rg1", "Microsoft.Storage", "storageAccounts"),
        ),
        (
            f"/subscriptions/{SUB}/resourceGroups/rg1/providers/Microsoft.Sql/servers/sv/databases/db",
            (SUB, "rg1", "Microsoft.Sql", "servers/databases"),
        ),
    ],
)
def test_parse_resource_id(rid, expected):
    assert parse_resource_id(rid) == expected


@pytest.mark.parametrize(
    "rid",
    [
        "/not/a/resource",
        "/subscriptions/s1/resourceGroups/rg1",  # subscription not a GUID
        f"/subscriptions/{SUB}/resourceGroups/rg1?$expand=x",  # query injection
        f"/subscriptions/{SUB}/resourceGroups/rg1#frag",
        f"/subscriptions/{SUB}/resourceGroups/rg1/providers/A.B/c/../../../x/y",  # traversal
    ],
)
def test_parse_resource_id_rejects_unsafe_ids(rid):
    with pytest.raises(ToolError):
        parse_resource_id(rid)


async def test_paging_refuses_next_link_to_another_host(monkeypatch):
    seen_hosts = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_hosts.append(request.url.host)
        return httpx2.Response(
            200,
            json={
                "value": [{"id": 1}],
                "@odata.nextLink": "https://attacker.example/v1.0/users",
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    monkeypatch.setattr(clients_mod, "_client", client)
    with pytest.raises(ToolError, match="another host"):
        await graph_list("user-token", "/users", limit=10)
    assert seen_hosts == ["graph.microsoft.com"]  # token never sent to the other host


async def test_non_json_error_body_is_truncated(monkeypatch):
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(502, text="<html>" + "x" * 5000)
        )
    )
    monkeypatch.setattr(clients_mod, "_client", client)
    with pytest.raises(ToolError) as exc:
        await graph_list("user-token", "/users")
    assert len(str(exc.value)) < 600


def test_graph_search_clause_strips_quotes():
    assert graph_search_clause('jo"hn', "displayName", "mail") == (
        '"displayName:john" OR "mail:john"'
    )
    with pytest.raises(ToolError):
        graph_search_clause('""', "displayName")
