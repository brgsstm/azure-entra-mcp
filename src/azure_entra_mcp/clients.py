"""Thin async helpers for Microsoft Graph and Azure Resource Manager.

Tools receive a per-user OBO token and pass it in here; nothing in this module holds
credentials. Errors from either API are turned into ``ToolError`` with the upstream
message, so the model (and the user) see *why* a call failed – usually missing RBAC or
a directory role, which is the intended behaviour of delegated access.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import httpx2
from fastmcp.exceptions import ToolError

from .config import ARM, GRAPH

ARM_PROVIDERS_API = "2021-04-01"
ARM_SUBSCRIPTIONS_API = "2022-12-01"
RESOURCE_GRAPH_API = "2022-10-01"

GUID_PATTERN = r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$"
_GUID = re.compile(GUID_PATTERN)
_MAX_ERROR_TEXT = 500  # non-JSON error bodies (e.g. gateway HTML) are cut to this

_client: httpx2.AsyncClient | None = None
_api_version_cache: dict[tuple[str, str], str] = {}


def http() -> httpx2.AsyncClient:
    global _client
    if _client is None:
        _client = httpx2.AsyncClient(timeout=30.0)
    return _client


async def close_http() -> None:
    """Close the shared client (server shutdown). A later call to ``http()`` reopens it."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _raise_for(resp: httpx2.Response, api: str) -> None:
    if resp.status_code < 400:
        return
    try:
        err = resp.json().get("error", {})
        code, message = err.get("code", ""), err.get("message", resp.text)
    except ValueError:
        code, message = "", resp.text[:_MAX_ERROR_TEXT]
    hint = ""
    if resp.status_code == 403:
        hint = " (your account lacks the RBAC role or directory permission for this)"
    elif resp.status_code == 404:
        hint = " (not found, or not visible to your account)"
    raise ToolError(f"{api} {resp.status_code} {code}: {message}{hint}".strip())


async def _get_paged(
    url: str,
    token: str,
    *,
    api: str,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    limit: int,
    next_key: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    hdrs = {"Authorization": f"Bearer {token}", **(headers or {})}
    origin = _origin(url)
    next_url: str | None = url
    while next_url and len(items) < limit:
        resp = await http().get(next_url, params=params, headers=hdrs)
        _raise_for(resp, api)
        body = resp.json()
        items.extend(body.get("value", []))
        next_url = body.get(next_key)
        # The user's bearer token goes with every page, so only follow links that stay
        # on the API we started on.
        if next_url and _origin(next_url) != origin:
            raise ToolError(f"{api} returned a next-page link to another host; not following")
        params = None  # nextLink already carries the query
    return items[:limit]


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


# --------------------------------------------------------------------------- Graph


async def graph_get(
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    resp = await http().get(
        f"{GRAPH}/v1.0{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
    )
    _raise_for(resp, "Graph")
    return resp.json()


async def graph_list(
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    limit: int = 100,
    advanced_query: bool = False,
) -> list[dict[str, Any]]:
    headers = {"ConsistencyLevel": "eventual"} if advanced_query else None
    return await _get_paged(
        f"{GRAPH}/v1.0{path}",
        token,
        api="Graph",
        params=params,
        headers=headers,
        limit=limit,
        next_key="@odata.nextLink",
    )


def graph_search_clause(query: str, *properties: str) -> str:
    """Build a $search value like '"displayName:foo" OR "mail:foo"'."""
    safe = query.replace('"', "").replace("\\", "").strip()
    if not safe:
        raise ToolError("Search query must not be empty")
    return " OR ".join(f'"{p}:{safe}"' for p in properties)


# ----------------------------------------------------------------------------- ARM


async def arm_get(
    token: str, path: str, api_version: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = await http().get(
        f"{ARM}{path}",
        params={"api-version": api_version, **(params or {})},
        headers={"Authorization": f"Bearer {token}"},
    )
    _raise_for(resp, "ARM")
    return resp.json()


async def arm_list(
    token: str,
    path: str,
    api_version: str,
    params: dict[str, Any] | None = None,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    return await _get_paged(
        f"{ARM}{path}",
        token,
        api="ARM",
        params={"api-version": api_version, **(params or {})},
        headers=None,
        limit=limit,
        next_key="nextLink",
    )


async def arm_post(token: str, path: str, api_version: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = await http().post(
        f"{ARM}{path}",
        params={"api-version": api_version},
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    _raise_for(resp, "ARM")
    return resp.json()


_RID = re.compile(
    r"^/subscriptions/(?P<sub>[^/]+)(?:/resourceGroups/(?P<rg>[^/]+))?"
    r"(?:/providers/(?P<ns>[^/]+)/(?P<rest>.+))?$",
    re.IGNORECASE,
)


def parse_resource_id(resource_id: str) -> tuple[str, str | None, str | None, str | None]:
    """Return (subscription, resource_group, namespace, resource_type) for an ARM ID.

    The ID is used verbatim as the request path, so anything that could change the
    query string or walk out of the resource (``?``, ``#``, ``.``/``..`` segments) is
    rejected.
    """
    m = _RID.match(resource_id.strip().rstrip("/"))
    if (
        not m
        or not _GUID.match(m["sub"])
        or any(c in resource_id for c in "?#")
        or any(seg in (".", "..") for seg in resource_id.split("/"))
    ):
        raise ToolError(f"Not a valid ARM resource ID: {resource_id}")
    rtype = None
    if m["rest"]:
        segments = m["rest"].split("/")
        if len(segments) % 2:
            raise ToolError(f"Resource ID has an odd number of type/name segments: {resource_id}")
        rtype = "/".join(segments[0::2])
    return m["sub"], m["rg"], m["ns"], rtype


async def resolve_api_version(token: str, subscription: str, namespace: str, rtype: str) -> str:
    """Pick the newest non-preview API version ARM advertises for a resource type."""
    key = (namespace.lower(), rtype.lower())
    if key in _api_version_cache:
        return _api_version_cache[key]
    provider = await arm_get(
        token, f"/subscriptions/{subscription}/providers/{namespace}", ARM_PROVIDERS_API
    )
    for entry in provider.get("resourceTypes", []):
        if entry.get("resourceType", "").lower() == rtype.lower():
            versions = entry.get("apiVersions", [])
            stable = [v for v in versions if "preview" not in v.lower()]
            chosen = (sorted(stable or versions, reverse=True) or [None])[0]
            if chosen:
                _api_version_cache[key] = chosen
                return chosen
    raise ToolError(f"Could not find an API version for {namespace}/{rtype}")
