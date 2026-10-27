# --- Container registry ----------------------------------------------------------

resource "azurerm_container_registry" "this" {
  name                = "cr${var.name_prefix}mcp${local.suffix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_role_assignment" "mcp_acr_pull" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.mcp.principal_id
  principal_type       = "ServicePrincipal"
}

# --- Key Vault: JWT signing key + token-store encryption key ---------------------
# Both must be identical on every replica and stable across restarts, otherwise
# users are signed out whenever the app scales or redeploys.

resource "azurerm_key_vault" "this" {
  name                       = "kv-${var.name_prefix}-mcp-${local.suffix}"
  location                   = azurerm_resource_group.this.location
  resource_group_name        = azurerm_resource_group.this.name
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 7
  tags                       = local.tags
}

resource "azurerm_role_assignment" "deployer_kv_officer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azuread_client_config.current.object_id
}

resource "azurerm_role_assignment" "mcp_kv_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.mcp.principal_id
  principal_type       = "ServicePrincipal"
}

resource "random_password" "jwt_signing_key" {
  length  = 64
  special = false
}

resource "random_password" "storage_encryption_key" {
  length  = 64
  special = false
}

resource "azurerm_key_vault_secret" "jwt_signing_key" {
  name         = "jwt-signing-key"
  value        = random_password.jwt_signing_key.result
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.deployer_kv_officer]
}

resource "azurerm_key_vault_secret" "storage_encryption_key" {
  name         = "storage-encryption-key"
  value        = random_password.storage_encryption_key.result
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.deployer_kv_officer]
}

# --- Token store: Azure Table Storage, Entra auth only ----------------------------
# Holds the OAuth proxy's client registrations and (Fernet-encrypted) upstream tokens.
# The server creates its table on first start using the managed identity.

resource "azurerm_storage_account" "tokens" {
  name                            = "st${var.name_prefix}mcp${local.suffix}"
  location                        = azurerm_resource_group.this.location
  resource_group_name             = azurerm_resource_group.this.name
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  default_to_oauth_authentication = true
  tags                            = local.tags
}

resource "azurerm_role_assignment" "mcp_table_data" {
  scope                = azurerm_storage_account.tokens.id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.mcp.principal_id
  principal_type       = "ServicePrincipal"
}
