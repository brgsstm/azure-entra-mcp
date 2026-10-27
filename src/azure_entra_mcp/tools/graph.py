"""Microsoft Entra (Graph) tools – read-only, delegated as the signed-in user.

What a user can see is governed by their own directory permissions, exactly as in the
Entra portal. The app's consented Graph scopes are the ceiling, not the grant.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.server.auth.providers.azure import EntraOBOToken
from pydantic import Field

from ..clients import graph_get, graph_list, graph_search_clause
from ..config import GRAPH
from .common import READ_ONLY

GRAPH_SCOPES = [f"{GRAPH}/.default"]

USER_FIELDS = (
    "id,displayName,userPrincipalName,mail,jobTitle,department,officeLocation,"
    "accountEnabled,userType,createdDateTime"
)
GROUP_FIELDS = (
    "id,displayName,description,mail,mailEnabled,securityEnabled,groupTypes,"
    "membershipRule,createdDateTime"
)
APP_FIELDS = (
    "id,appId,displayName,signInAudience,createdDateTime,"
    "passwordCredentials,keyCredentials,web,identifierUris"
)

Limit = Annotated[int, Field(ge=1, le=500, description="Maximum results to return")]


def _app_summary(app: dict[str, Any]) -> dict[str, Any]:
    """Graph never returns secret values; keep just the expiry metadata."""

    def creds(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "displayName": c.get("displayName"),
                "keyId": c.get("keyId"),
                "endDateTime": c.get("endDateTime"),
            }
            for c in items or []
        ]

    return {
        "id": app.get("id"),
        "appId": app.get("appId"),
        "displayName": app.get("displayName"),
        "signInAudience": app.get("signInAudience"),
        "createdDateTime": app.get("createdDateTime"),
        "identifierUris": app.get("identifierUris"),
        "redirectUris": (app.get("web") or {}).get("redirectUris"),
        "secrets": creds(app.get("passwordCredentials")),
        "certificates": creds(app.get("keyCredentials")),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def entra_whoami(token: str = EntraOBOToken(GRAPH_SCOPES)) -> dict[str, Any]:
        """Show the signed-in user's Entra profile. Useful to confirm whose identity
        the other tools are acting as."""
        return await graph_get(token, "/me", {"$select": USER_FIELDS})

    @mcp.tool(annotations=READ_ONLY)
    async def entra_list_my_groups(
        limit: Limit = 100,
        token: str = EntraOBOToken(GRAPH_SCOPES),
    ) -> list[dict[str, Any]]:
        """List groups the signed-in user belongs to, including nested membership."""
        return await graph_list(
            token,
            "/me/transitiveMemberOf/microsoft.graph.group",
            {"$select": "id,displayName,mail,securityEnabled,groupTypes", "$top": min(limit, 999)},
            limit=limit,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def entra_search_users(
        query: Annotated[str, Field(description="Name, email or UPN fragment to search for")],
        limit: Limit = 25,
        token: str = EntraOBOToken(GRAPH_SCOPES),
    ) -> list[dict[str, Any]]:
        """Search the directory for users by display name, email or UPN."""
        return await graph_list(
            token,
            "/users",
            {
                "$search": graph_search_clause(query, "displayName", "mail", "userPrincipalName"),
                "$select": USER_FIELDS,
                "$top": min(limit, 999),
            },
            limit=limit,
            advanced_query=True,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def entra_get_user(
        user: Annotated[str, Field(description="Object ID or userPrincipalName")],
        include_manager: Annotated[
            bool, Field(description="Also return the user's manager")
        ] = False,
        token: str = EntraOBOToken(GRAPH_SCOPES),
    ) -> dict[str, Any]:
        """Get one user's directory profile, optionally with their manager."""
        params: dict[str, Any] = {"$select": USER_FIELDS}
        if include_manager:
            params["$expand"] = "manager($select=id,displayName,userPrincipalName)"
        return await graph_get(token, f"/users/{quote(user.strip(), safe='@')}", params)

    @mcp.tool(annotations=READ_ONLY)
    async def entra_search_groups(
        query: Annotated[str, Field(description="Group name or email fragment")],
        limit: Limit = 25,
        token: str = EntraOBOToken(GRAPH_SCOPES),
    ) -> list[dict[str, Any]]:
        """Search Entra groups by display name or email."""
        return await graph_list(
            token,
            "/groups",
            {
                "$search": graph_search_clause(query, "displayName", "mail"),
                "$select": GROUP_FIELDS,
                "$top": min(limit, 999),
            },
            limit=limit,
            advanced_query=True,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def entra_list_group_members(
        group_id: Annotated[str, Field(description="Group object ID")],
        transitive: Annotated[bool, Field(description="Include members of nested groups")] = False,
        limit: Limit = 200,
        token: str = EntraOBOToken(GRAPH_SCOPES),
    ) -> list[dict[str, Any]]:
        """List the members of a group (users, groups, service principals, devices)."""
        rel = "transitiveMembers" if transitive else "members"
        return await graph_list(
            token,
            f"/groups/{quote(group_id.strip(), safe='')}/{rel}",
            {"$select": "id,displayName,userPrincipalName,mail", "$top": min(limit, 999)},
            limit=limit,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def entra_search_app_registrations(
        query: Annotated[str, Field(description="App display name fragment")],
        limit: Limit = 25,
        token: str = EntraOBOToken(GRAPH_SCOPES),
    ) -> list[dict[str, Any]]:
        """Find app registrations by name, including secret and certificate expiry dates
        (never the secret values). Handy for spotting credentials about to expire."""
        apps = await graph_list(
            token,
            "/applications",
            {
                "$search": graph_search_clause(query, "displayName"),
                "$select": APP_FIELDS,
                "$top": min(limit, 999),
            },
            limit=limit,
            advanced_query=True,
        )
        return [_app_summary(a) for a in apps]
