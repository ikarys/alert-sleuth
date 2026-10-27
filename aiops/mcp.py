from __future__ import annotations

import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aiops.config import Settings


def _headers(settings: Settings) -> dict[str, str]:
    key = settings.mcp_api_key
    if not key:
        return {}
    return {
        "Authorization": f"Bearer {key}",
        "x-litellm-api-key": f"Bearer {key}",
    }


async def _call(settings: Settings, name: str, arguments: dict) -> object:
    async with create_mcp_http_client(headers=_headers(settings)) as http:
        async with streamable_http_client(settings.mcp_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(name, arguments)


async def _list_tools(settings: Settings) -> object:
    async with create_mcp_http_client(headers=_headers(settings)) as http:
        async with streamable_http_client(settings.mcp_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.list_tools()


def _texts(result: object) -> list[str]:
    texts: list[str] = []
    content = getattr(result, "content", None)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                texts.append(str(text))
    return texts


def call_tool(settings: Settings, name: str, arguments: dict) -> list[str]:
    if not settings.mcp_url:
        raise RuntimeError("LITELLM_MCP_URL not set")

    result = asyncio.run(
        asyncio.wait_for(_call(settings, name, arguments), timeout=settings.collector_timeout)
    )

    if getattr(result, "is_error", False):
        texts = _texts(result)
        first = texts[0] if texts else ""
        raise RuntimeError(f"MCP tool {name} error: {first[:200]}")

    return _texts(result)


def list_tools(settings: Settings) -> list[dict]:
    if not settings.mcp_url:
        raise RuntimeError("LITELLM_MCP_URL not set")

    result = asyncio.run(
        asyncio.wait_for(_list_tools(settings), timeout=settings.collector_timeout)
    )

    tools = getattr(result, "tools", None)
    if not isinstance(tools, list):
        tools = []

    rows: list[dict] = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None)
        if not isinstance(schema, dict):
            schema = {}

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}

        description = str(getattr(tool, "description", "") or "").strip()
        first_line = description.splitlines()[0][:120] if description else ""

        rows.append(
            {
                "name": str(getattr(tool, "name", "")),
                "description": first_line,
                "params": sorted(properties.keys()),
                "required": schema.get("required", []),
            }
        )

    return rows


def parse_json_texts(texts: list[str]) -> list[object]:
    parsed: list[object] = []
    for text in texts:
        try:
            parsed.append(json.loads(text))
        except (TypeError, ValueError):
            continue
    return parsed
