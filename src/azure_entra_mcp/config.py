"""Runtime configuration, read from environment variables.

Two auth modes are supported, chosen automatically:

* **Managed identity (production)** – ``MANAGED_IDENTITY_CLIENT_ID`` is set and
  ``ENTRA_CLIENT_SECRET`` is not. The Container App's user-assigned managed identity
  signs the Entra token exchange and OBO calls via a federated identity credential.
  No client secret exists anywhere.
* **Client secret (local dev)** – ``ENTRA_CLIENT_SECRET`` is set. Use a short-lived
  secret on the same app registration for running the server on your laptop.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com"
ARM = "https://management.azure.com"

# Delegated scopes requested at sign-in so Entra records consent for the downstream
# APIs. Admin consent is granted by Terraform, so users never see a consent prompt.
DEFAULT_ADDITIONAL_SCOPES = [
    f"{GRAPH}/User.Read",
    f"{GRAPH}/User.Read.All",
    f"{GRAPH}/GroupMember.Read.All",
    f"{GRAPH}/Group.Read.All",
    f"{GRAPH}/Application.Read.All",
    f"{ARM}/user_impersonation",
]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    app_client_id: str
    public_base_url: str
    mcp_scope: str = "mcp-access"
    additional_scopes: list[str] = field(default_factory=lambda: list(DEFAULT_ADDITIONAL_SCOPES))

    # Credentials – exactly one of these is used
    client_secret: str | None = None
    managed_identity_client_id: str | None = None

    # OAuth proxy state
    jwt_signing_key: str = ""
    storage_encryption_key: str = ""
    token_store_account: str | None = None
    token_store_table: str = "mcpoauth"  # noqa: S105 (a table name, not a secret)
    token_store_endpoint: str | None = None  # override for Azurite / sovereign clouds

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @property
    def use_managed_identity(self) -> bool:
        return self.client_secret is None

    @classmethod
    def from_env(cls) -> Settings:
        client_secret = _optional("ENTRA_CLIENT_SECRET")
        mi_client_id = _optional("MANAGED_IDENTITY_CLIENT_ID")
        if client_secret is None and mi_client_id is None:
            raise RuntimeError(
                "Set MANAGED_IDENTITY_CLIENT_ID (production) or ENTRA_CLIENT_SECRET (local dev)."
            )

        jwt_key = _optional("JWT_SIGNING_KEY")
        enc_key = _optional("STORAGE_ENCRYPTION_KEY")
        store_account = _optional("TOKEN_STORE_ACCOUNT")

        if store_account and not (jwt_key and enc_key):
            raise RuntimeError(
                "TOKEN_STORE_ACCOUNT is set, so JWT_SIGNING_KEY and STORAGE_ENCRYPTION_KEY "
                "must be fixed values shared by every replica."
            )
        if not jwt_key or not enc_key:
            log.warning(
                "JWT_SIGNING_KEY / STORAGE_ENCRYPTION_KEY not set – generating ephemeral keys. "
                "Every restart will sign everyone out. Fine for local dev only."
            )
            jwt_key = jwt_key or secrets.token_urlsafe(48)
            enc_key = enc_key or secrets.token_urlsafe(48)

        extra = _optional("ADDITIONAL_SCOPES")
        additional = extra.replace(",", " ").split() if extra else list(DEFAULT_ADDITIONAL_SCOPES)

        return cls(
            tenant_id=_required("AZURE_TENANT_ID"),
            app_client_id=_required("ENTRA_APP_CLIENT_ID"),
            public_base_url=_required("PUBLIC_BASE_URL").rstrip("/"),
            mcp_scope=_optional("MCP_SCOPE") or "mcp-access",
            additional_scopes=additional,
            client_secret=client_secret,
            managed_identity_client_id=mi_client_id,
            jwt_signing_key=jwt_key,
            storage_encryption_key=enc_key,
            token_store_account=store_account,
            token_store_table=_optional("TOKEN_STORE_TABLE") or "mcpoauth",
            token_store_endpoint=_optional("TOKEN_STORE_ENDPOINT"),
            host=_optional("HOST") or "0.0.0.0",
            port=int(_optional("PORT") or 8000),
            log_level=(_optional("LOG_LEVEL") or "INFO").upper(),
        )
