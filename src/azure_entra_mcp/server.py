"""Entrypoint: builds the FastMCP server and serves it over streamable HTTP."""

from __future__ import annotations

import logging

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth import build_auth
from .clients import close_http
from .config import Settings
from .tools import register_all

INSTRUCTIONS = """\
Tools for Azure and Microsoft Entra ID. Every call runs as the signed-in user via
on-behalf-of tokens, so results reflect that user's own Azure RBAC and directory
permissions. A 403 means the user lacks access, not that the tool is broken.
Prefer azure_resource_graph_query for inventory questions across subscriptions.
"""


@lifespan
async def http_client_lifespan(_: FastMCP):
    yield {}
    await close_http()


def create_server(settings: Settings) -> FastMCP:
    mcp = FastMCP(
        name="azure-entra-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=build_auth(settings),
        lifespan=http_client_lifespan,
    )

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    register_all(mcp)
    return mcp


def create_app():
    """ASGI app factory (``uvicorn azure_entra_mcp.server:create_app --factory``)."""
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return build_http_app(settings)


def build_http_app(settings: Settings):
    mcp = create_server(settings)
    # stateless_http: any replica can serve any request, so no session affinity needed.
    # Origin guard (MCP transport spec: a present, untrusted Origin gets 403): "auto" plus
    # an explicit allowed_origins validates Origin on every request; Host is only checked
    # on loopback connections. Requests without an Origin header (non-browser MCP clients)
    # are unaffected.
    return mcp.http_app(
        path="/mcp",
        stateless_http=True,
        host_origin_protection="auto",
        allowed_origins=[settings.public_base_url],
    )


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "azure_entra_mcp.server:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,  # ACA terminates TLS and sets X-Forwarded-*
        forwarded_allow_ips="*",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
