"""Thin async MCP client for the Swiggy Food server.

Uses the MCP Python SDK's streamable-HTTP transport. Callers get a
``SwiggyClient`` context manager; inside it, call any Swiggy tool by name.

Usage:
    async with SwiggyClient(access_token) as client:
        menu = await client.call("get_restaurant_menu", restaurantId="668678")
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "https://mcp.swiggy.com/food"


class SwiggyClientError(RuntimeError):
    """Raised when a Swiggy MCP tool call fails or returns an error."""


class SwiggyClient:
    """Async context manager wrapping a live Swiggy MCP session.

    async with SwiggyClient(token) as client:
        result = await client.call("get_restaurant_menu", restaurantId="668678")
    """

    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._session: ClientSession | None = None
        self._exit_stack = None

    async def __aenter__(self) -> "SwiggyClient":
        from contextlib import AsyncExitStack

        self._exit_stack = AsyncExitStack()
        headers = {"Authorization": f"Bearer {self._token}"}
        transport = await self._exit_stack.enter_async_context(
            streamablehttp_client(MCP_URL, headers=headers)
        )
        read, write, _ = transport
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()

    async def call(self, tool_name: str, **kwargs: Any) -> Any:
        """Call a Swiggy MCP tool, return the parsed JSON result."""
        if self._session is None:
            raise SwiggyClientError("not inside an async with block")
        result = await self._session.call_tool(tool_name, arguments=kwargs)
        if result.isError:
            raise SwiggyClientError(f"{tool_name} returned error: {result.content}")
        text = result.content[0].text if result.content else "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
