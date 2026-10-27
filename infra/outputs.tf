output "mcp_url" {
  description = "Add this URL to Claude / VS Code as a remote MCP server."
  value       = "${local.base_url}/mcp"
}

output "base_url" {
  value = local.base_url
}

output "tenant_id" {
  value = var.tenant_id
}

output "app_client_id" {
  value = azuread_application.mcp.client_id
}

output "app_object_id" {
  description = "Use with `az ad app credential reset --id` to create a short-lived local-dev secret."
  value       = azuread_application.mcp.object_id
}

output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "container_app_name" {
  value = azurerm_container_app.mcp.name
}

output "acr_name" {
  value = azurerm_container_registry.this.name
}

output "acr_login_server" {
  value = azurerm_container_registry.this.login_server
}

output "managed_identity_client_id" {
  value = azurerm_user_assigned_identity.mcp.client_id
}
