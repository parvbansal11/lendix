"""Minimal client for Nonilion's MCP server (Streamable HTTP, JSON-RPC tools/call).

Authentication is a personal token from Mission Control -> MCP. The token is never logged.
"""
import json
import logging

import httpx

log = logging.getLogger("lendix.nonilion")
ENDPOINT = "https://www.nonilion.com/mcp"


class NonilionError(RuntimeError):
    """A tool call failed. The message never contains the token."""


class NonilionMCP:
    def __init__(self, token, endpoint=ENDPOINT, transport=None, timeout=45.0):
        if not token:
            raise ValueError("A Nonilion personal token is required.")
        self.endpoint = endpoint
        self._client = httpx.AsyncClient(
            timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream", "User-Agent": "lendix-office/1.0"})
        self._id = 0

    @staticmethod
    def _parse(response):
        if "text/event-stream" in response.headers.get("content-type", ""):
            frames = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
            return json.loads(frames[-1]) if frames else {}
        return response.json()

    async def call(self, name, arguments=None):
        """Run one tool. Returns (text, data): the human line and the trailing JSON block, if any."""
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}}}
        try:
            response = await self._client.post(self.endpoint, json=body)
        except httpx.HTTPError as exc:
            raise NonilionError(f"{name}: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise NonilionError(f"{name}: HTTP {response.status_code}")
        message = self._parse(response)
        if "error" in message:
            raise NonilionError(f"{name}: {message['error'].get('message', 'error')}")
        result = message.get("result", {})
        text = "\n".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
        if result.get("isError"):
            raise NonilionError(f"{name}: {text[:200]}")
        head, _, tail = text.partition("\n\n")
        data = None
        if tail.strip().startswith(("{", "[")):
            try:
                data = json.loads(tail)
            except ValueError:
                data = None
        return (head if data is not None else text), data

    async def close(self):
        await self._client.aclose()
