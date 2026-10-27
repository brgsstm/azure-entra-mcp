variable "tenant_id" {
  type        = string
  description = "Entra tenant ID."
}

variable "subscription_id" {
  type        = string
  description = "Subscription to deploy the Container App and supporting resources into."
}

variable "location" {
  type    = string
  default = "uksouth"
}

variable "name_prefix" {
  type        = string
  default     = "azmcp"
  description = "Short lowercase prefix for resource names (letters and digits, 3-8 chars)."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,7}$", var.name_prefix))
    error_message = "name_prefix must be 3-8 lowercase letters/digits, starting with a letter."
  }
}

variable "app_display_name" {
  type    = string
  default = "Azure Entra MCP Server"
}

variable "allowed_group_object_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Entra groups allowed to sign in. When non-empty, user assignment is required on the
    enterprise app and only members of these groups can use the server. Leave empty to
    allow anyone in the tenant (not recommended).
  EOT
}

variable "graph_delegated_scopes" {
  type = list(string)
  default = [
    "User.Read",
    "User.Read.All",
    "GroupMember.Read.All",
    "Group.Read.All",
    "Application.Read.All",
    "offline_access",
  ]
  description = "Delegated Graph permissions the server may use on behalf of users (admin consented). Keep in sync with ADDITIONAL_SCOPES / config.py."
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the azure-entra-mcp image in ACR."
}

variable "custom_domain" {
  type        = string
  default     = null
  description = "Optional custom hostname (e.g. mcp.example.co.uk), already bound to the Container App. Changes the public URL and redirect URI."
}

variable "min_replicas" {
  type    = number
  default = 1
}

variable "max_replicas" {
  type    = number
  default = 3
}

variable "tags" {
  type    = map(string)
  default = {}
}
