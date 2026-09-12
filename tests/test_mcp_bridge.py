"""AIGent's tools reach an external engine over MCP, through the same execution path as before."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.mcp_bridge import PROTOCOL_VERSIONS, ToolBridge
from connector.store import Store


class Farm:
    """A stand-in extension: one tool that records what it was asked and one that fails."""

    def __init__(self):
        self.calls = []

    def tools(self, session):
        return [{"type": "function", "function": {"name": "shared_image", "description": "Farm image.",
                                                  "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}},
                                                                 "required": ["prompt"]}}},
                {"type": "function", "function": {"name": "shared_boom", "description": "Always fails.",
                                                  "parameters": {"type": "object", "properties": {}}}}]

    async def execute(self, session, name, args):
        self.calls.append((name, args))
        if name == "shared_boom":
            raise ValueError("the farm is asleep")
        return {"path": "shared/moon.png", "prompt": args["prompt"]}


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"))
    store = Store(config.root / "mcp.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    farm = Farm()
    agent.extensions.append(farm)
    yield config, store, agent, farm, ToolBridge(agent)
    store.db.close()


async def test_the_catalogue_is_the_sessions_extensions_plus_delivery_tools(bundle):
    _, store, agent, farm, bridge = bundle
    session = store.resolve(1, 0, 1)

    names = [t["name"] for t in bridge.tools(session)]

    assert names == ["ask_user_async", "send_file", "shared_image", "shared_boom"]
    assert "read_file" not in names and "write_file" not in names, "files and commands are the engine's own"
    schema = next(t for t in bridge.tools(session) if t["name"] == "shared_image")["inputSchema"]
    assert schema["required"] == ["prompt"] and schema["properties"]["prompt"] == {"type": "string"}


async def test_initialize_list_and_call_over_json_rpc(bundle):
    _, store, agent, farm, bridge = bundle
    session = store.resolve(2, 0, 1)

    status, init = await bridge.handle(session, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    assert status == 200 and init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    unknown, _ = await bridge.handle(session, {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                                               "params": {"protocolVersion": "1999-01-01"}})
    assert unknown == 200
    status, none = await bridge.handle(session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert (status, none) == (202, None), "a notification gets 202 and no body"

    _, listed = await bridge.handle(session, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]][-2:] == ["shared_image", "shared_boom"]

    _, called = await bridge.handle(session, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                              "params": {"name": "shared_image", "arguments": {"prompt": "moon"}}})
    assert called["result"]["isError"] is False
    assert '"path": "shared/moon.png"' in called["result"]["content"][0]["text"]
    assert farm.calls == [("shared_image", {"prompt": "moon"})]

    _, failed = await bridge.handle(session, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                              "params": {"name": "shared_boom", "arguments": {}}})
    assert failed["result"]["isError"] is True and "the farm is asleep" in failed["result"]["content"][0]["text"]

    _, missing = await bridge.handle(session, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                               "params": {"name": "read_file", "arguments": {"path": "x"}}})
    assert missing["result"]["isError"] is True and "Unknown tool" in missing["result"]["content"][0]["text"]
    _, nomethod = await bridge.handle(session, {"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
    assert nomethod["error"]["code"] == -32601
    _, batch = await bridge.handle(session, [{"jsonrpc": "2.0", "id": 8, "method": "ping"},
                                             {"jsonrpc": "2.0", "method": "notifications/progress"}])
    assert batch == [{"jsonrpc": "2.0", "id": 8, "result": {}}]
    assert PROTOCOL_VERSIONS[0] > PROTOCOL_VERSIONS[-1]


def test_the_endpoint_needs_the_connector_token_and_serves_a_session(tmp_path):
    app = create_app(tmp_path, polling=False)
    config = app.state.config
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        sid = client.post("/api/sessions", json={"title": "mcp"}).json()["id"]
        anonymous = TestClient(app)
        assert anonymous.post(f"/api/mcp/{sid}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
        headers = {"Authorization": "Bearer " + config["connector_token"]}
        init = anonymous.post(f"/api/mcp/{sid}", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "aigent"
        assert anonymous.post(f"/api/mcp/{sid}", headers=headers,
                              json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
        listed = anonymous.post(f"/api/mcp/{sid}", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert {"send_file", "shared_image", "shared_video", "server_status", "restart_server"} <= names
        assert "spawn_subagents" not in names, "the engine has its own subagents"
        assert anonymous.get(f"/api/mcp/{sid}", headers=headers).status_code == 405
        assert anonymous.post("/api/mcp/nope", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "ping"}).status_code == 404
