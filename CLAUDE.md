# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastMCP server (Python ≥3.13) exposing read-only Microsoft Graph (Entra) and Azure Resource Manager tools, deployed to Azure Container Apps via Terraform. Every tool call runs **as the signed-in user** via the on-behalf-of (OBO) flow; the server itself has no Azure RBAC roles and, in production, no client secret.

## Commands

```bash
uv sync                                                  # .venv from uv.lock (dev group included)
uv run pytest                                            # all tests (no Azure/network needed)
uv run pytest tests/test_auth.py::test_parse_resource_id # single test
uv run ruff check . && uv run ruff format .              # lint / format (line length 100)
uv lock --upgrade-package fastmcp                        # deliberate dependency bump; then test + commit uv.lock

set -a && . ./.env && set +a && uv run azure-entra-mcp   # run locally on :8000, MCP at /mcp
./scripts/deploy.sh                                      # tests, build in ACR + terraform apply (run from repo root)
terraform -chdir=infra output -raw mcp_url
```

pytest uses `asyncio_mode = "auto"`, so async tests need no decorator. Local runs need a short-lived `ENTRA_CLIENT_SECRET` on the app registration (see README "Running locally"); without `TOKEN_STORE_ACCOUNT` OAuth state is in memory and lost on restart.

CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, `ruff check`, `ruff format --check`, pytest and a `docker build` on every PR and push to `main`, so run ruff format before committing. Dependabot (`.github/dependabot.yml`) opens the dependency-bump PRs.

## Architecture

**Request flow:** MCP client → FastMCP `AzureProvider` OAuth proxy (Entra has no Dynamic Client Registration, so clients register with the proxy) → tool receives the user's Entra token → OBO exchange for a Graph or ARM token → `clients.py` HTTP helpers.

- `config.py` — `Settings.from_env()`. Auth mode is chosen implicitly: `ENTRA_CLIENT_SECRET` set ⇒ secret mode (local dev, takes precedence); otherwise `MANAGED_IDENTITY_CLIENT_ID` ⇒ managed-identity mode (`use_managed_identity` is just `client_secret is None`). Fails fast on missing config; if `TOKEN_STORE_ACCOUNT` is set, `JWT_SIGNING_KEY`/`STORAGE_ENCRYPTION_KEY` must be fixed values, else ephemeral keys are generated.
- `auth.py` — `ManagedIdentityAzureProvider` subclasses FastMCP's `AzureProvider` and overrides two private hooks to replace the client secret with an MI-minted client assertion (federated identity credential): `_create_upstream_oauth_client` (code/refresh exchange) and `get_obo_credential` (OBO), the hooks FastMCP documents for this. `ManagedIdentityAssertion.get()` must be awaited **before** anything that calls the synchronous `current()` (azure-identity's `client_assertion_func`, and `ClientAssertionOAuth2Client._apply_client_auth`, whose async `fetch_token`/`refresh_token` do the await). These overrides depend on FastMCP internals (`fastmcp<5` is pinned); `tests/test_fastmcp_contract.py` pins them, so run it first when upgrading FastMCP. `build_storage` wraps Azure Table Storage or an in-memory store in Fernet encryption.
- `server.py` — `create_app()` is an ASGI factory (`build_http_app(settings)` is the testable part); HTTP is `stateless_http=True` (any replica serves any request). The FastMCP Host/Origin guard runs in `"auto"` mode with `allowed_origins=[PUBLIC_BASE_URL]`, so a foreign `Origin` gets 403 as the MCP transport spec requires; requests without `Origin` pass. `/healthz` is used by the Docker healthcheck and ACA probes.
- `clients.py` — stateless Graph/ARM helpers taking a token; paging via `_get_paged`; all upstream errors become `ToolError` with a 403/404 hint (403 means the user lacks access, which is expected behaviour). `resolve_api_version` picks the newest non-preview ARM API version per resource type and caches it.
- `tools/graph.py`, `tools/arm.py` — each has a `register(mcp)` function wired up in `tools/__init__.py::register_all`.

### Adding a tool

Define it inside the relevant `register()` with `@mcp.tool(annotations=READ_ONLY)` (from `tools/common.py`), take `token: str = EntraOBOToken(GRAPH_SCOPES)` (Graph tools) or `EntraOBOToken(ARM_SCOPES)` (ARM tools) as the last parameter, describe args with `Annotated[..., Field(description=...)]`, and call the `clients.py` helpers. Trim responses to useful fields (see `_slim`, `_app_summary`, the `*_FIELDS` `$select` strings). All tools are currently read-only; the README suggests destructive-hint annotations and narrow scope before adding any write tools.

### Permissions are defined in three places

A new Graph delegated scope must be consistent across:
1. `infra/variables.tf` `graph_delegated_scopes` (app registration permissions + admin-consent grant in `infra/entra.tf`),
2. the `ADDITIONAL_SCOPES` env var built from it in `infra/containerapp.tf` (prefixes `https://graph.microsoft.com/`, drops `offline_access`, appends ARM `user_impersonation`),
3. `DEFAULT_ADDITIONAL_SCOPES` in `config.py` (fallback used when `ADDITIONAL_SCOPES` is unset, e.g. local dev).

Users must reconnect once to pick up a new scope.

## Infrastructure

`infra/` is a single Terraform root: `entra.tf` (app registration exposing `api://<app-id>/mcp-access`, FIC trusting the user-assigned MI, group-restricted enterprise app, redirect URIs including `http://localhost:8000/auth/callback`), `platform.tf` (Key Vault, Table Storage with shared keys off, ACR, Log Analytics), `containerapp.tf` (env vars and Key Vault-referenced secrets). `scripts/deploy.sh` runs `uv run --locked pytest` first (fails if `uv.lock` is stale), then bootstraps the RG + ACR with `-target` on first run, builds with `az acr build` (no local Docker), then applies with `image_tag` = git short SHA. Generated keys live in Terraform state. `terraform.tfvars` is gitignored; start from `terraform.tfvars.example`.

Changing `JWT_SIGNING_KEY` or `STORAGE_ENCRYPTION_KEY` signs every user out. The README has a troubleshooting table for common `AADSTS` errors.
