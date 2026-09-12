"""AIGent's own tools, offered to an external agent engine over the Model Context Protocol.

The DeepSeek Harness engine runs the model loop; AIGent keeps the things the engine has no idea
about — the AutoRig farm, video assembly, the skill index, Telegram delivery, the supervisor
restart. This bridge lists those tools and executes them, over MCP's Streamable HTTP transport,
at ``POST /api/mcp/{sid}`` inside the connector process itself. So a tool call from the engine
runs with the session's workspace, approvals and event journal exactly as it did before, and the
engine sees the result as a native tool result (``mcp__aigent__<name>``).

Only the JSON-RPC subset a tool server needs is implemented: initialize, the initialized
notification, ping, tools/list and tools/call. The server is stateless — no session ids, no
server-initiated stream — which the MCP client SDKs accept (a GET answers 405).
"""

import json

from .agent import TOOLS
from .version import __version__

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# Core tools the engine cannot replace: the rest of TOOLS (files, commands, goals) is the engine's own.
CORE_TOOLS = ("send_file", "ask_user_async")
# Extension tools the engine already has natively; offering ours too would only confuse the model.
ENGINE_OWNED = ("spawn_subagents",)


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class ToolBridge:
    """Lists and executes the session's AIGent tools for an MCP client."""

    def __init__(self, agent):
        self.agent = agent

    # ------------------------------------------------------------------ catalogue
    def functions(self, session):
        """The session's tool schemas, in OpenAI function shape: extensions plus the core delivery tools."""
        items = [t["function"] for t in TOOLS if t["function"]["name"] in CORE_TOOLS]
        for extension in self.agent.extensions:
            for item in extension.tools(session):
                items.append(item.get("function", item))
        seen, unique = set(ENGINE_OWNED), []
        for function in items:
            if function["name"] in seen:
                continue
            seen.add(function["name"])
            unique.append(function)
        return unique

    def tools(self, session):
        return [{"name": f["name"], "description": f.get("description", ""),
                 "inputSchema": f.get("parameters") or {"type": "object", "properties": {}}}
                for f in self.functions(session)]

    # ------------------------------------------------------------------ execution
    async def call(self, session, name, arguments):
        """Run one tool through Agent.execute: same approvals, same workspace, same events."""
        known = {f["name"] for f in self.functions(session)}
        if name not in known:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        try:
            result = await self.agent.execute(session, name, arguments if isinstance(arguments, dict) else {})
        except Exception as exc:  # the engine gets an honest error text, never a dead connection
            text = self.agent.config.redact(f"{type(exc).__name__}: {exc}".rstrip(": "))
            hint = self.agent.error_hint(exc, arguments)
            return {"content": [{"type": "text", "text": text + (f"\nHint: {hint}" if hint else "")}],
                    "isError": True}
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        failed = isinstance(result, dict) and bool(result.get("error"))
        content = [{"type": "text", "text": text}]
        # A tool that queued pictures for the model (screenshots, view_image) hands them over as MCP
        # image blocks; the engine shows them to a model that can see.
        for part in self.agent.pending_images.pop(session["id"], []) or []:
            url = str((part.get("image_url") or {}).get("url", "")) if isinstance(part, dict) else ""
            head, _, data = url.partition(",")
            if data:
                content.append({"type": "image", "data": data, "mimeType": head.removeprefix("data:").split(";")[0] or "image/png"})
        self.agent.pending_image_paths.pop(session["id"], None)
        return {"content": content, "isError": failed}

    # ------------------------------------------------------------------ JSON-RPC
    async def handle(self, session, message):
        """Answer one JSON-RPC message (or a batch). Returns (status, body); body None means no content."""
        if isinstance(message, list):
            answers = [await self.handle_one(session, item) for item in message]
            answers = [a for a in answers if a is not None]
            return (200, answers) if answers else (202, None)
        answer = await self.handle_one(session, message)
        return (200, answer) if answer is not None else (202, None)

    async def handle_one(self, session, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if method is None:
            return None  # a response to a server request; we never send any
        if request_id is None:
            return None  # notifications (initialized, cancelled, progress) need no answer
        if method == "initialize":
            wanted = str(params.get("protocolVersion") or "")
            version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1]
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "aigent", "version": __version__},
                "instructions": "AIGent's farm, media, skill and delivery tools for this session. "
                                "Results are JSON text; a long farm render waits inside its tool."}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools(session)}}
        if method == "tools/call":
            name = str(params.get("name") or "")
            result = await self.call(session, name, params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        return _error(request_id, -32601, f"Method not found: {method}")
