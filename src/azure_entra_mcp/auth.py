"""Entra authentication for the MCP server.

FastMCP's ``AzureProvider`` runs an OAuth proxy in front of Entra ID (Entra has no
Dynamic Client Registration, so MCP clients register with the proxy instead). Clients
sign users in through the browser; the server receives the user's Entra token and
exchanges it on-behalf-of (OBO) for Graph / ARM tokens inside each tool.

Out of the box ``AzureProvider`` authenticates *itself* to Entra with a client secret.
``ManagedIdentityAzureProvider`` replaces that with a client assertion minted by the
Container App's user-assigned managed identity, trusted by the app registration
through a federated identity credential (FIC). Both upstream calls are covered:

* the authorization-code / refresh-token exchange (``_create_upstream_oauth_client``)
* the OBO exchange (``get_obo_credential``)

Both are the hooks FastMCP documents for this. tests/test_fastmcp_contract.py pins the
FastMCP internals they rely on, so an incompatible FastMCP upgrade fails the test suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

from azure.identity.aio import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    OnBehalfOfCredential,
)
from fastmcp.server.auth.oauth_proxy.proxy import HTTP_TIMEOUT_SECONDS
from fastmcp.server.auth.oauth_proxy.upstream import AsyncOAuth2Client
from fastmcp.server.auth.providers.azure import AzureProvider
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.azure_tables import AzureTablesSanitizationStrategy, AzureTablesStore
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from .config import Settings

log = logging.getLogger(__name__)

# Audience Entra expects on a managed-identity token used as a federated credential.
FIC_AUDIENCE_SCOPE = "api://AzureADTokenExchange/.default"
JWT_BEARER_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
STORAGE_SALT = "azure-entra-mcp/oauth-proxy-storage/v1"


class ManagedIdentityAssertion:
    """Caches a managed-identity token for use as a client assertion.

    MI tokens live for hours; we refresh five minutes before expiry. The cached value
    is also handed to azure-identity's synchronous ``client_assertion_func``, so it must
    be refreshed (asynchronously) before any code path that may call that function.
    """

    _REFRESH_MARGIN_SECONDS = 300

    def __init__(self, managed_identity_client_id: str) -> None:
        self._credential = ManagedIdentityCredential(client_id=managed_identity_client_id)
        self._token: str | None = None
        self._expires_on: int = 0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if self._token and time.time() < self._expires_on - self._REFRESH_MARGIN_SECONDS:
            return self._token
        async with self._lock:
            if self._token and time.time() < self._expires_on - self._REFRESH_MARGIN_SECONDS:
                return self._token
            token = await self._credential.get_token(FIC_AUDIENCE_SCOPE)
            self._token, self._expires_on = token.token, token.expires_on
            log.debug("Refreshed managed identity client assertion")
            return self._token

    def current(self) -> str:
        if not self._token:
            raise RuntimeError("Managed identity assertion requested before it was fetched")
        return self._token

    async def close(self) -> None:
        await self._credential.close()


class ClientAssertionOAuth2Client(AsyncOAuth2Client):
    """FastMCP's upstream token client, authenticating with a JWT client assertion.

    FastMCP builds this client synchronously, so the assertion is refreshed in the async
    request methods, before ``_apply_client_auth`` reads it.
    """

    def __init__(
        self, client_id: str, assertion: ManagedIdentityAssertion, timeout: float | None = None
    ):
        super().__init__(client_id=client_id, client_secret=None, timeout=timeout)
        self._assertion = assertion

    async def fetch_token(self, url: str, **kwargs: Any) -> dict[str, Any]:
        await self._assertion.get()
        return await super().fetch_token(url, **kwargs)

    async def refresh_token(self, url: str, **kwargs: Any) -> dict[str, Any]:
        await self._assertion.get()
        return await super().refresh_token(url, **kwargs)

    def _apply_client_auth(self, data: dict[str, Any], headers: dict[str, str]) -> None:
        data["client_id"] = self.client_id
        data["client_assertion_type"] = JWT_BEARER_ASSERTION_TYPE
        data["client_assertion"] = self._assertion.current()


class ManagedIdentityAzureProvider(AzureProvider):
    """``AzureProvider`` that uses a managed identity FIC instead of a client secret."""

    def __init__(self, *, managed_identity_client_id: str, **kwargs: Any) -> None:
        kwargs.pop("client_secret", None)
        super().__init__(client_secret=None, **kwargs)
        self._mi_assertion = ManagedIdentityAssertion(managed_identity_client_id)

    def _create_upstream_oauth_client(self) -> AsyncOAuth2Client:
        return ClientAssertionOAuth2Client(
            client_id=self._upstream_client_id,
            assertion=self._mi_assertion,
            timeout=HTTP_TIMEOUT_SECONDS,
        )

    async def get_obo_credential(self, user_assertion: str) -> OnBehalfOfCredential:
        # Refresh first: OnBehalfOfCredential calls client_assertion_func synchronously.
        await self._mi_assertion.get()

        key = hashlib.sha256(user_assertion.encode()).hexdigest()
        if key in self._obo_credentials:
            self._obo_credentials.move_to_end(key)
            return self._obo_credentials[key]

        credential = OnBehalfOfCredential(
            tenant_id=self._tenant_id,
            client_id=self._upstream_client_id,
            user_assertion=user_assertion,
            client_assertion_func=self._mi_assertion.current,
            authority=f"https://{self._base_authority}",
        )
        self._obo_credentials[key] = credential
        while len(self._obo_credentials) > self._obo_max_credentials:
            _, evicted = self._obo_credentials.popitem(last=False)
            await evicted.close()
        return credential


class TableKeySanitizer(AzureTablesSanitizationStrategy):
    """Make any OAuth proxy key a valid Azure Tables RowKey.

    Keys containing ``/``, ``\\``, ``#`` or ``?`` (e.g. URL client IDs from Client ID
    Metadata Documents, as Claude Code uses) are hashed. The base strategy *rejects*
    keys starting with its reserved ``H_``/``S_`` prefixes, which FastMCP's random
    ``token_urlsafe`` IDs occasionally do, so those are hashed too instead.
    """

    def validate(self, value: str) -> None:
        pass

    def sanitize(self, value: str) -> str:
        if value.startswith(("H_", "S_")):
            return "H_" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        return super().sanitize(value)


def build_storage(settings: Settings) -> AsyncKeyValue:
    """OAuth proxy state: client registrations, upstream tokens, auth transactions.

    Production uses Azure Table Storage (shared by all replicas, accessed with the
    managed identity). Locally, without ``TOKEN_STORE_ACCOUNT``, state is in memory.
    Either way it is Fernet-encrypted at rest.
    """
    store: AsyncKeyValue
    if settings.token_store_account:
        credential = (
            ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
            if settings.use_managed_identity
            else DefaultAzureCredential()
        )
        store = AzureTablesStore(
            account_name=settings.token_store_account,
            credential=credential,
            table_name=settings.token_store_table,
            endpoint=settings.token_store_endpoint,
            key_sanitization_strategy=TableKeySanitizer(),
        )
        log.info(
            "OAuth state: Azure Table %s/%s",
            settings.token_store_account,
            settings.token_store_table,
        )
    else:
        store = MemoryStore()
        log.warning("OAuth state: in-memory (single process, lost on restart)")

    return FernetEncryptionWrapper(
        key_value=store,
        source_material=settings.storage_encryption_key,
        salt=STORAGE_SALT,
    )


def build_auth(settings: Settings) -> AzureProvider:
    common: dict[str, Any] = dict(
        client_id=settings.app_client_id,
        tenant_id=settings.tenant_id,
        base_url=settings.public_base_url,
        required_scopes=[settings.mcp_scope],
        additional_authorize_scopes=settings.additional_scopes,
        jwt_signing_key=settings.jwt_signing_key,
        client_storage=build_storage(settings),
    )
    if settings.use_managed_identity:
        log.info("Entra auth: managed identity federated credential (no client secret)")
        return ManagedIdentityAzureProvider(
            managed_identity_client_id=settings.managed_identity_client_id, **common
        )
    log.info("Entra auth: client secret (local dev mode)")
    return AzureProvider(client_secret=settings.client_secret, **common)
