locals {
  graph_app_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph
  arm_app_id   = "797f4846-ba00-4fd7-ba43-dac1f8f63013" # Azure Service Management
  mcp_scope    = "mcp-access"
}

data "azuread_service_principal" "graph" {
  client_id = local.graph_app_id
}

data "azuread_service_principal" "arm" {
  client_id = local.arm_app_id
}

resource "random_uuid" "mcp_scope" {}

resource "azuread_application" "mcp" {
  display_name     = var.app_display_name
  owners           = [data.azuread_client_config.current.object_id]
  sign_in_audience = "AzureADMyOrg"

  api {
    requested_access_token_version = 2 # required by FastMCP's Azure provider

    oauth2_permission_scope {
      id                         = random_uuid.mcp_scope.result
      value                      = local.mcp_scope
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Use the Azure/Entra MCP server"
      admin_consent_description  = "Allows MCP clients to call the Azure/Entra MCP server as the signed-in user."
      user_consent_display_name  = "Use the Azure/Entra MCP server"
      user_consent_description   = "Allows your MCP client to call the Azure/Entra MCP server as you."
    }
  }

  required_resource_access {
    resource_app_id = local.graph_app_id
    dynamic "resource_access" {
      for_each = toset(var.graph_delegated_scopes)
      content {
        id   = data.azuread_service_principal.graph.oauth2_permission_scope_ids[resource_access.value]
        type = "Scope"
      }
    }
  }

  required_resource_access {
    resource_app_id = local.arm_app_id
    resource_access {
      id   = data.azuread_service_principal.arm.oauth2_permission_scope_ids["user_impersonation"]
      type = "Scope"
    }
  }

  web {
    redirect_uris = [
      "${local.base_url}/auth/callback",
      "http://localhost:8000/auth/callback", # local development
    ]
  }

  # Managed by azuread_application_identifier_uri below (avoids a self-reference).
  lifecycle {
    ignore_changes = [identifier_uris]
  }
}

resource "azuread_application_identifier_uri" "mcp" {
  application_id = azuread_application.mcp.id
  identifier_uri = "api://${azuread_application.mcp.client_id}"
}

resource "azuread_service_principal" "mcp" {
  client_id                    = azuread_application.mcp.client_id
  owners                       = [data.azuread_client_config.current.object_id]
  app_role_assignment_required = length(var.allowed_group_object_ids) > 0
  tags                         = ["WindowsAzureActiveDirectoryIntegratedApp"]
}

# --- Tenant-wide (admin) consent, so users never see a consent prompt -------------
# Requires the Terraform identity to hold Cloud Application Administrator (or higher)
# plus DelegatedPermissionGrant.ReadWrite.All if running as a service principal.

resource "azuread_service_principal_delegated_permission_grant" "graph" {
  service_principal_object_id          = azuread_service_principal.mcp.object_id
  resource_service_principal_object_id = data.azuread_service_principal.graph.object_id
  claim_values                         = var.graph_delegated_scopes
}

resource "azuread_service_principal_delegated_permission_grant" "arm" {
  service_principal_object_id          = azuread_service_principal.mcp.object_id
  resource_service_principal_object_id = data.azuread_service_principal.arm.object_id
  claim_values                         = ["user_impersonation"]
}

# The OAuth proxy signs users in for its own API scope (api://<app>/mcp-access).
resource "azuread_service_principal_delegated_permission_grant" "self" {
  service_principal_object_id          = azuread_service_principal.mcp.object_id
  resource_service_principal_object_id = azuread_service_principal.mcp.object_id
  claim_values                         = [local.mcp_scope]

  depends_on = [azuread_application_identifier_uri.mcp]
}

# --- Who may sign in -------------------------------------------------------------

resource "azuread_app_role_assignment" "allowed_groups" {
  for_each            = toset(var.allowed_group_object_ids)
  app_role_id         = "00000000-0000-0000-0000-000000000000" # default access
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.mcp.object_id
}

# --- Secretless app credential ---------------------------------------------------
# The Container App's user-assigned identity is trusted as a credential for the app
# registration. The server presents an MI token as a client assertion for the code
# exchange, refresh and OBO calls, so the app has no client secret at all.

resource "azuread_application_federated_identity_credential" "aca_identity" {
  application_id = azuread_application.mcp.id
  display_name   = "aca-${var.name_prefix}-uami"
  description    = "Container App managed identity for ${local.app_name}"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://login.microsoftonline.com/${var.tenant_id}/v2.0"
  subject        = azurerm_user_assigned_identity.mcp.principal_id
}
