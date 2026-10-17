"""Stdio MCP client used by the deterministic controller to drive logistics tools.

This spins the PARADOX MCP server as a stdio subprocess and calls its tools over
the Model Context Protocol -- the same transport ADK's `McpToolset` uses. Every
call is recorded as a redacted trace entry so the report can prove genuine MCP
tool invocation (tool name, redacted args, status, duration).

Usage (async):
    async with ParadoxMcpClient(db_path) as client:
        res = await client.call("seed_world", {"world_json": ...})
    client.trace  # list of mcp_tool_invocation records
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional

from mcp import Client
from mcp.client.stdio import StdioServerParameters

# Keys whose values must never appear in the redacted trace.
_REDACT_KEYS = {"world_json", "api_key", "key", "token"}


def _redact(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if k in _REDACT_KEYS:
            out[k] = f"<redacted:{len(str(v))} chars>"
        else:
            out[k] = v
    return out


class ParadoxMcpClient:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self.trace: list[dict] = []
        self._client: Optional[Client] = None
        self._cm = None

    async def __aenter__(self) -> "ParadoxMcpClient":
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "paradox.mcp_server"] + (["--db", self.db_path] if self.db_path else []),
            env=None,
        )
        self._client = Client(params, raise_exceptions=False)
        self._cm = self._client
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)

    async def list_tools(self) -> list[str]:
        result = await self._client.list_tools()
        tools = getattr(result, "tools", result)
        return [t.name for t in tools]

    async def call(self, tool: str, args: dict[str, Any]) -> dict:
        """Invoke an MCP tool; parse and return its JSON payload; record trace."""
        t0 = time.time()
        result = await self._client.call_tool(tool, args or {})
        duration_ms = int((time.time() - t0) * 1000)
        payload = self._extract(result)
        status = payload.get("status", "ok") if isinstance(payload, dict) else "ok"
        self.trace.append({
            "tool": tool,
            "args": _redact(args),
            "status": status,
            "duration_ms": duration_ms,
            "error": payload.get("error") if isinstance(payload, dict) else None,
        })
        return payload

    @staticmethod
    def _extract(result: Any) -> dict:
        """Pull the JSON text out of an MCP CallToolResult."""
        # mcp 2.x returns an object with .content (list of TextContent) and/or
        # .structured_content. Prefer text payload (our tools return JSON strings).
        content = getattr(result, "content", None)
        if content:
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {"status": "ok", "raw": text}
        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None)
        if structured:
            return structured
        return {"status": "ok", "raw": str(result)}
