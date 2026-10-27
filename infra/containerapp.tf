resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${var.name_prefix}-mcp"
  location                   = azurerm_resource_group.this.location
  resource_group_name        = azurerm_resource_group.this.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  tags                       = local.tags
}

resource "azurerm_container_app" "mcp" {
  name                         = local.app_name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.mcp.id]
  }

  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.mcp.id
  }

  secret {
    name                = "jwt-signing-key"
    key_vault_secret_id = azurerm_key_vault_secret.jwt_signing_key.versionless_id
    identity            = azurerm_user_assigned_identity.mcp.id
  }

  secret {
    name                = "storage-encryption-key"
    key_vault_secret_id = azurerm_key_vault_secret.storage_encryption_key.versionless_id
    identity            = azurerm_user_assigned_identity.mcp.id
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name   = "mcp"
      image  = "${azurerm_container_registry.this.login_server}/azure-entra-mcp:${var.image_tag}"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "AZURE_TENANT_ID"
        value = var.tenant_id
      }
      env {
        name  = "ENTRA_APP_CLIENT_ID"
        value = azuread_application.mcp.client_id
      }
      env {
        name  = "PUBLIC_BASE_URL"
        value = local.base_url
      }
      env {
        name  = "MANAGED_IDENTITY_CLIENT_ID"
        value = azurerm_user_assigned_identity.mcp.client_id
      }
      env {
        name  = "TOKEN_STORE_ACCOUNT"
        value = azurerm_storage_account.tokens.name
      }
      env {
        name = "ADDITIONAL_SCOPES"
        value = join(" ", concat(
          [for s in var.graph_delegated_scopes : "https://graph.microsoft.com/${s}" if s != "offline_access"],
          ["https://management.azure.com/user_impersonation"],
        ))
      }
      env {
        name        = "JWT_SIGNING_KEY"
        secret_name = "jwt-signing-key"
      }
      env {
        name        = "STORAGE_ENCRYPTION_KEY"
        secret_name = "storage-encryption-key"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.mcp_acr_pull,
    azurerm_role_assignment.mcp_kv_secrets_user,
    azurerm_role_assignment.mcp_table_data,
    azuread_application_federated_identity_credential.aca_identity,
  ]
}
