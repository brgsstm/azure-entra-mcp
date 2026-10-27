resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

locals {
  suffix   = random_string.suffix.result
  app_name = "${var.name_prefix}-mcp"
  tags     = merge({ workload = "azure-entra-mcp", managed_by = "terraform" }, var.tags)

  # ACA app FQDN is deterministic: <app-name>.<environment default domain>, so the
  # public URL (and therefore the Entra redirect URI) is known before the app exists.
  default_fqdn = "${local.app_name}.${azurerm_container_app_environment.this.default_domain}"
  public_host  = coalesce(var.custom_domain, local.default_fqdn)
  base_url     = "https://${local.public_host}"
}

data "azuread_client_config" "current" {}

resource "azurerm_resource_group" "this" {
  name     = "rg-${var.name_prefix}-mcp"
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${var.name_prefix}-mcp-${local.suffix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

# The app's only identity. It holds NO Azure RBAC on your subscriptions: every
# Graph/ARM call runs as the signed-in user via OBO. It only needs to pull the image,
# read two Key Vault secrets, use the token table, and act as the app registration's
# federated credential.
resource "azurerm_user_assigned_identity" "mcp" {
  name                = "id-${var.name_prefix}-mcp"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}
