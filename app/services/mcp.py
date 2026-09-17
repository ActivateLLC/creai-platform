"""
A minimal Model Context Protocol client (Streamable HTTP transport).

Enough of the spec to list and call tools on a remote server on a customer's
behalf: initialize, tools/list (paginated) and tools/call, with JSON or
event-stream responses and the Mcp-Session-Id header.
"""

import itertools
import json

import httpx

PROTOCOL = "2025-06-18"


class MCPError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Unauthorized(MCPError):
    pass


class MCPClient:
    def __init__(self, url: str, token: str, *, timeout: float = 60):
        self.url, self.token, self.timeout = url, token, timeout
        self.session_id: str | None = None
        self._ids = itertools.count(1)
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(timeout=self.timeout)
        await self._rpc("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "creai", "version": "1.0"}})
        await self._notify("notifications/initialized")
        return self

    async def __aexit__(self, *exc):
        if self._http:
            if self.session_id:
                try:
                    await self._http.delete(self.url, headers=self._headers())
                except httpx.HTTPError:
                    pass
            await self._http.aclose()

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}",
             "Accept": "application/json, text/event-stream",
             "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _post(self, payload: dict) -> httpx.Response:
        r = await self._http.post(self.url, json=payload, headers=self._headers())
        if r.status_code == 401:
            raise Unauthorized("the connection needs to be re-authorised", 401)
        if r.status_code >= 400:
            raise MCPError(f"server returned {r.status_code}: {r.text[:200]}", r.status_code)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        return r

    async def _notify(self, method: str, params: dict | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _rpc(self, method: str, params: dict) -> dict:
        rid = next(self._ids)
        r = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        msg = None
        if "text/event-stream" in r.headers.get("content-type", ""):
            for block in r.text.split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in block.splitlines()
                                 if line.startswith("data:"))
                if not data:
                    continue
                try:
                    candidate = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if candidate.get("id") == rid:
                    msg = candidate
                    break
        else:
            msg = r.json()
        if not msg:
            raise MCPError(f"no response to {method}")
        if "error" in msg:
            raise MCPError((msg["error"] or {}).get("message", "error"))
        return msg.get("result") or {}

    async def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})


def result_text(result: dict, limit: int = 24000) -> str:
    """Flatten a tools/call result into text the model can read, capped."""
    parts = []
    for block in result.get("content") or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        else:
            parts.append(f"[{block.get('type')} content]")
    if result.get("structuredContent") and not parts:
        parts.append(json.dumps(result["structuredContent"]))
    text = "\n".join(parts)
    if len(text) > limit:
        text = text[:limit] + f"\n…[truncated {len(text) - limit} characters]"
    return ("ERROR: " if result.get("isError") else "") + text
