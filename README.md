# azure-entra-mcp

A team MCP server for Azure and Microsoft Entra ID, hosted on Azure Container Apps.

People sign in with their own Entra account in the browser (the MCP client handles
this). Every tool then calls Microsoft Graph or Azure Resource Manager **as that
person**, using the on-behalf-of (OBO) flow, so results respect their own Azure RBAC
and directory permissions. The server itself has no Azure roles and **no client
secret**: it authenticates to Entra with its managed identity via a federated
identity credential.

```
 Claude / VS Code / Claude Code
        │  1. 401 + metadata → browser sign-in (Entra, via the server's OAuth proxy)
        │  2. MCP requests with bearer token
        ▼
 ┌─────────────────────────── Azure Container App ──────────────────────────┐
 │ FastMCP + AzureProvider (OAuth proxy)                                     │
 │   • code exchange / refresh ── MI client assertion ──▶ Entra ID           │
 │   • per tool call: OBO ── MI client assertion ──▶ Graph token / ARM token │
 └──────┬──────────────────────────────┬─────────────────────────────────────┘
        │ user-assigned managed identity
        ├─▶ Key Vault        JWT signing key, token-store encryption key
        ├─▶ Table Storage    OAuth proxy state (Fernet-encrypted, shared by replicas)
        └─▶ ACR              image pull
```

## Tools (all read-only)

| Tool | What it does |
| --- | --- |
| `entra_whoami` | The signed-in user's profile – confirms whose identity tools act as |
| `entra_list_my_groups` | The user's groups, including nested membership |
| `entra_search_users` / `entra_get_user` | Directory lookup by name, email, UPN or ID (optionally with manager) |
| `entra_search_groups` / `entra_list_group_members` | Group lookup and membership (direct or transitive) |
| `entra_search_app_registrations` | App registrations with secret/certificate **expiry dates** (never values) |
| `azure_list_subscriptions` | Subscriptions the user can see |
| `azure_list_resource_groups` / `azure_list_resources` | Resource groups and resources, filterable by type |
| `azure_get_resource` | Full definition of any resource; API version resolved automatically |
| `azure_resource_graph_query` | Azure Resource Graph KQL across subscriptions – best for inventory questions |

To add a tool, define a function inside `register()` in `src/azure_entra_mcp/tools/graph.py`
or `arm.py`, decorate it with `@mcp.tool(annotations=READ_ONLY)`, take
`token: str = EntraOBOToken(GRAPH_SCOPES)` (or `ARM_SCOPES`) as the last parameter, and
call the helpers in `clients.py`.

## Prerequisites

* Terraform ≥ 1.6 and the Azure CLI (`az login` into the target tenant)
* On the target subscription: **Owner** (or Contributor + User Access Administrator),
  because Terraform creates role assignments
* In Entra: **Cloud Application Administrator** (or Application Administrator) to create
  the app registration, plus the right to grant tenant-wide consent for the delegated
  Graph permissions (**Privileged Role Administrator** or Global Administrator for
  `*.Read.All` scopes). If you can't grant consent yourself, see
  [Splitting the Entra part](#splitting-the-entra-part).
* One or more Entra **groups** whose members may use the server

## Deploy

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars   # fill in tenant, subscription, groups
./scripts/deploy.sh
```

The script:

1. runs the tests (`uv run --locked pytest`) and stops on failure or a stale `uv.lock`,
2. creates the resource group and container registry on the first run,
3. builds the image in ACR with `az acr build` (no local Docker needed),
4. applies the rest of the infrastructure with the new image tag and prints the MCP URL.

Run it again to ship a new version; each run rolls a new revision.

### What Terraform creates

| Resource | Purpose |
| --- | --- |
| App registration + enterprise app | Exposes `api://<app-id>/mcp-access`; delegated Graph + ARM permissions; **admin consent granted**; sign-in limited to `allowed_group_object_ids` |
| Federated identity credential | Trusts the Container App's managed identity as the app's credential, so there's no client secret |
| User-assigned managed identity | AcrPull, Key Vault Secrets User, Storage Table Data Contributor – **nothing on your subscriptions** |
| Key Vault (RBAC) | JWT signing key and token-store encryption key, referenced as Container App secrets |
| Storage account (Entra auth only, shared keys off) | Table for OAuth proxy state |
| Container Apps environment + app, Log Analytics, ACR | Hosting |

The generated keys are in Terraform state, so use a locked-down remote backend (see
`infra/versions.tf`).

## Connecting clients

The server URL is `terraform -chdir=infra output -raw mcp_url` (ends in `/mcp`).

**Claude (claude.ai, desktop, mobile).** On a Team or Enterprise plan an Owner adds it
once under *Organization settings → Connectors → Add → Custom*, using the server URL.
For *OAuth client* choose **Use Claude's published identity** (the server supports
Client ID Metadata Documents). Each member then goes to *Customize → Connectors*,
finds the connector and clicks **Connect**.

**Claude Code**

```bash
claude mcp add --transport http azure-entra https://<your-app>/mcp
# then inside Claude Code:
/mcp      # select azure-entra → sign in in the browser
```

Add `--scope project` to share it with the repo via `.mcp.json`.

**VS Code** (`.vscode/mcp.json`)

```json
{ "servers": { "azure-entra": { "type": "http", "url": "https://<your-app>/mcp" } } }
```

**First sign-in:** the server shows a short consent page (it protects against one
client reusing another's authorisation), then Entra's account picker. Users don't see
an Entra permissions prompt because consent is granted tenant-wide.

## Running locally

Local runs can't use the managed identity, so give the same app registration a
short-lived secret:

```bash
APP=$(terraform -chdir=infra output -raw app_object_id)
END=$(date -u -v+7d +%Y-%m-%d 2>/dev/null || date -u -d '+7 days' +%Y-%m-%d)  # macOS || Linux
az ad app credential reset --id "$APP" --append --display-name local-dev \
  --end-date "$END" --query password -o tsv

cp .env.example .env    # set AZURE_TENANT_ID, ENTRA_APP_CLIENT_ID, ENTRA_CLIENT_SECRET
uv sync                 # creates .venv from uv.lock, including dev tools
set -a && . ./.env && set +a && uv run azure-entra-mcp
claude mcp add --transport http azure-entra-local http://localhost:8000/mcp
```

`http://localhost:8000/auth/callback` is already a registered redirect URI. Without
`TOKEN_STORE_ACCOUNT` the server keeps OAuth state in memory, so restarting signs you
out. Delete the `local-dev` secret when you're done.

Tests (no Azure needed): `uv run pytest`. `scripts/deploy.sh` runs them first and stops
on failure. GitHub Actions (`.github/workflows/ci.yml`) runs ruff, the tests and a Docker
build on every pull request and push to `main`, and Dependabot (`.github/dependabot.yml`)
opens pull requests for dependency updates, so those are tested before merge.

Dependencies are pinned in `uv.lock`, and the image installs exactly those versions.
After editing `pyproject.toml` run `uv lock`; to take newer versions within the allowed
ranges run `uv lock --upgrade` (or `--upgrade-package fastmcp`), then the tests, and
commit the lockfile.

## Configuration

| Variable | Required | Notes |
| --- | --- | --- |
| `AZURE_TENANT_ID` | yes | |
| `ENTRA_APP_CLIENT_ID` | yes | App registration client ID |
| `PUBLIC_BASE_URL` | yes | External URL without `/mcp`; must match the redirect URI host |
| `MANAGED_IDENTITY_CLIENT_ID` | prod | Selects secretless mode |
| `ENTRA_CLIENT_SECRET` | local | Selects secret mode; takes precedence |
| `JWT_SIGNING_KEY`, `STORAGE_ENCRYPTION_KEY` | prod | Must be identical across replicas and stable |
| `TOKEN_STORE_ACCOUNT` | prod | Storage account for the table; omit for in-memory |
| `TOKEN_STORE_TABLE` | | Default `mcpoauth` |
| `TOKEN_STORE_ENDPOINT` | | Table endpoint override, for Azurite or sovereign clouds |
| `ADDITIONAL_SCOPES` | | Space-separated delegated scopes requested at sign-in; Terraform sets this from `graph_delegated_scopes` |
| `MCP_SCOPE` | | Default `mcp-access` |
| `LOG_LEVEL`, `HOST`, `PORT` | | Defaults `INFO`, `0.0.0.0`, `8000` |

## Changing permissions

To let tools use another Graph permission (say `AuditLog.Read.All`), add it to
`graph_delegated_scopes` in `terraform.tfvars` and re-apply. That updates the app
registration, the admin-consent grant and `ADDITIONAL_SCOPES` together. Also add it to
`DEFAULT_ADDITIONAL_SCOPES` in `src/azure_entra_mcp/config.py`, which local runs use when
`ADDITIONAL_SCOPES` isn't set. Users need to reconnect once to pick it up.

The consented scopes are a ceiling, not a grant: someone without a directory role
that allows reading sign-in logs still gets a 403.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `AADSTS65001` on a tool call | Admin consent missing for that scope – re-apply Terraform or grant consent in the portal |
| `AADSTS65005` right after a change | Entra propagation lag on new scopes; wait 2–15 minutes |
| `AADSTS70021` / "No matching federated identity record" | FIC subject or issuer doesn't match the managed identity – check `azuread_application_federated_identity_credential` |
| `AADSTS50105` at sign-in | User isn't in an allowed group |
| Everyone signed out after a deploy | `JWT_SIGNING_KEY` or `STORAGE_ENCRYPTION_KEY` changed, or `TOKEN_STORE_ACCOUNT` unset |
| Tool returns ARM/Graph 403 | Working as intended – the user lacks the role |
| Revision fails to start | Check `az containerapp logs show -n <app> -g <rg>`; the server fails fast on missing config |

## Splitting the Entra part

If an Entra admin has to create the app registration, move `infra/entra.tf` into its
own Terraform root they apply, and pass `app_client_id`, the app object ID and the
managed identity principal ID across as variables. The Container App only needs the
client ID.

## Hardening ideas

* Custom domain on the Container App (set `custom_domain` once it's bound)
* Private endpoints for Key Vault and Storage, with VNet integration for the environment
* Restrict `allowed_client_redirect_uris` on the provider to the clients you use
* Before adding write tools: use MCP's destructive-hint annotations, keep writes narrow,
  and consider requiring a confirmation parameter
