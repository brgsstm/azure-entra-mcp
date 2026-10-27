"""Azure Resource Manager tools – read-only, delegated as the signed-in user.

Every call carries the user's own token, so Azure RBAC decides what each person can
see. The Container App's managed identity has no Azure RBAC role at all.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.server.auth.providers.azure import EntraOBOToken
from pydantic import Field

from ..clients import (
    ARM_PROVIDERS_API,
    ARM_SUBSCRIPTIONS_API,
    GUID_PATTERN,
    RESOURCE_GRAPH_API,
    arm_get,
    arm_list,
    arm_post,
    parse_resource_id,
    resolve_api_version,
)
from ..config import ARM
from .common import READ_ONLY

ARM_SCOPES = [f"{ARM}/.default"]

# Interpolated into request paths, so it is validated rather than trusted.
SubscriptionId = Annotated[
    str, Field(pattern=GUID_PATTERN, description="Azure subscription ID (GUID)")
]
Limit = Annotated[int, Field(ge=1, le=1000, description="Maximum results to return")]


def _slim(resource: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "name", "type", "location", "kind", "sku", "tags", "resourceGroup")
    return {k: resource[k] for k in keep if k in resource}


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def azure_list_subscriptions(
        token: str = EntraOBOToken(ARM_SCOPES),
    ) -> list[dict[str, Any]]:
        """List Azure subscriptions the signed-in user can access."""
        subs = await arm_list(token, "/subscriptions", ARM_SUBSCRIPTIONS_API)
        return [
            {
                "subscriptionId": s.get("subscriptionId"),
                "displayName": s.get("displayName"),
                "state": s.get("state"),
                "tenantId": s.get("tenantId"),
            }
            for s in subs
        ]

    @mcp.tool(annotations=READ_ONLY)
    async def azure_list_resource_groups(
        subscription_id: SubscriptionId,
        token: str = EntraOBOToken(ARM_SCOPES),
    ) -> list[dict[str, Any]]:
        """List resource groups in a subscription."""
        groups = await arm_list(
            token, f"/subscriptions/{subscription_id}/resourcegroups", ARM_PROVIDERS_API
        )
        return [
            {
                "name": g.get("name"),
                "location": g.get("location"),
                "tags": g.get("tags"),
                "provisioningState": (g.get("properties") or {}).get("provisioningState"),
            }
            for g in groups
        ]

    @mcp.tool(annotations=READ_ONLY)
    async def azure_list_resources(
        subscription_id: SubscriptionId,
        resource_group: Annotated[
            str | None, Field(description="Limit to one resource group")
        ] = None,
        resource_type: Annotated[
            str | None, Field(description="Filter by type, e.g. Microsoft.Storage/storageAccounts")
        ] = None,
        limit: Limit = 200,
        token: str = EntraOBOToken(ARM_SCOPES),
    ) -> list[dict[str, Any]]:
        """List resources in a subscription or resource group, optionally filtered by type."""
        path = f"/subscriptions/{subscription_id}"
        if resource_group:
            path += f"/resourceGroups/{quote(resource_group.strip(), safe='()')}"
        params = None
        if resource_type:
            safe_type = resource_type.replace("'", "''")
            params = {"$filter": f"resourceType eq '{safe_type}'"}
        resources = await arm_list(
            token, f"{path}/resources", ARM_PROVIDERS_API, params, limit=limit
        )
        return [_slim(r) for r in resources]

    @mcp.tool(annotations=READ_ONLY)
    async def azure_get_resource(
        resource_id: Annotated[str, Field(description="Full ARM resource ID")],
        token: str = EntraOBOToken(ARM_SCOPES),
    ) -> dict[str, Any]:
        """Get the full definition of one resource (properties, SKU, tags, identity).
        The API version is resolved automatically from the resource provider."""
        sub, rg, ns, rtype = parse_resource_id(resource_id)
        if ns and rtype:
            api = await resolve_api_version(token, sub, ns, rtype)
        elif rg:
            api = ARM_PROVIDERS_API
        else:
            api = ARM_SUBSCRIPTIONS_API
        return await arm_get(token, resource_id, api)

    @mcp.tool(annotations=READ_ONLY)
    async def azure_resource_graph_query(
        query: Annotated[
            str,
            Field(
                description=(
                    'Azure Resource Graph KQL, e.g. "Resources | where type =~ '
                    "'microsoft.compute/virtualmachines' | project name, location, "
                    'resourceGroup". '
                    "Tables include Resources, ResourceContainers, AuthorizationResources, "
                    "AdvisorResources and PolicyResources."
                )
            ),
        ],
        subscription_ids: Annotated[
            list[str] | None,
            Field(description="Restrict to these subscriptions; default is all you can access"),
        ] = None,
        limit: Limit = 100,
        token: str = EntraOBOToken(ARM_SCOPES),
    ) -> dict[str, Any]:
        """Run an Azure Resource Graph query across subscriptions – the fastest way to
        answer inventory questions (what exists, where, tagged how, in what state)."""
        body: dict[str, Any] = {
            "query": query,
            "options": {"$top": limit, "resultFormat": "objectArray"},
        }
        if subscription_ids:
            body["subscriptions"] = subscription_ids
        result = await arm_post(
            token, "/providers/Microsoft.ResourceGraph/resources", RESOURCE_GRAPH_API, body
        )
        return {
            "totalRecords": result.get("totalRecords"),
            "count": result.get("count"),
            "truncated": bool(result.get("$skipToken")),
            "data": result.get("data", []),
        }
