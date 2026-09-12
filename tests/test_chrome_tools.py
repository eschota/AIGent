"""The owner's Chrome as a tool: the protocol plumbing and the tool contract, without a browser."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from connector.agent import Agent
from connector.chrome_tools import KEYS, CDPSocket, ChromeTools
from connector.config import Config, password_hash
from connector.mcp_bridge import ToolBridge
from connector.store import Store


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"))
    store = Store(config.root / "chrome.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    chrome = ChromeTools(agent, client=AsyncMock())
    agent.extensions.append(chrome)
    yield config, store, agent, chrome
    store.db.close()


def test_websocket_frames_round_trip_and_split():
    payload = json.dumps({"id": 1, "method": "Page.navigate"}).encode()
    framed = CDPSocket.frame(payload)
    assert framed[0] == 0x81 and framed[1] & 0x80, "a client frame is a masked final text frame"
    fin, opcode, decoded, rest = CDPSocket.parse(framed)
    assert (fin, opcode, decoded, rest) == (True, 1, payload, b"")
    big = CDPSocket.frame(b"x" * 70000)
    assert big[1] & 0x7F == 127 and CDPSocket.parse(big)[2] == b"x" * 70000
    medium = CDPSocket.frame(b"y" * 300)
    assert medium[1] & 0x7F == 126
    # A server frame is unmasked; two frames back to back split cleanly, an incomplete one waits.
    server = bytes([0x81, 3]) + b"abc" + bytes([0x81, 2]) + b"de"
    fin, opcode, first, rest = CDPSocket.parse(server)
    assert first == b"abc" and CDPSocket.parse(rest)[2] == b"de"
    assert CDPSocket.parse(bytes([0x81, 5]) + b"ab") is None


async def test_call_matches_replies_by_id_and_keeps_events():
    socket = CDPSocket("ws://127.0.0.1:1/devtools/page/x")
    sent = []

    class Writer:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            pass

    class Reader:
        def __init__(self):
            frames = [json.dumps({"method": "Page.loadEventFired", "params": {}}).encode(),
                      json.dumps({"id": 1, "result": {"frameId": "f1"}}).encode()]
            self.chunks = [bytes([0x81, len(f)]) + f for f in frames]

        async def read(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    socket.writer, socket.reader = Writer(), Reader()
    result = await socket.call("Page.navigate", {"url": "https://example.org"})
    assert result == {"frameId": "f1"} and socket.events[0]["method"] == "Page.loadEventFired"
    assert json.loads(CDPSocket.parse(sent[0])[2])["params"]["url"] == "https://example.org"


def test_the_tool_contract_and_launch_arguments(bundle, monkeypatch):
    config, store, agent, chrome = bundle
    session = store.resolve(1, 0, 1)
    names = [t["function"]["name"] for t in chrome.tools(session)]
    assert names == ["chrome_open", "chrome_snapshot", "chrome_click", "chrome_type", "chrome_press", "chrome_upload",
                     "chrome_wait", "chrome_screenshot", "chrome_eval"]
    config.values.update(browser_binary=str(config.root / "chrome.exe"), chrome_user_data_dir=r"C:\Profiles\Me",
                         chrome_profile="Profile 2", chrome_debug_port=9444)
    (config.root / "chrome.exe").write_bytes(b"")
    args = chrome.launch_args("https://studio.youtube.com/")
    assert args[0] == str(config.root / "chrome.exe") and "--remote-debugging-port=9444" in args
    assert r"--user-data-dir=C:\Profiles\Me" in args and "--profile-directory=Profile 2" in args
    assert args[-1] == "https://studio.youtube.com/" and "--headless" not in " ".join(args)
    assert "Enter" in KEYS and KEYS["Enter"][0] == 13


async def test_actions_need_an_open_tab_and_approvals_guard_the_risky_ones(bundle):
    config, store, agent, chrome = bundle
    session = store.resolve(2, 0, 1)
    with pytest.raises(ValueError, match="chrome_open first"):
        await chrome.execute(session, "chrome_snapshot", {})
    with pytest.raises(ValueError, match="http"):
        await chrome.execute(session, "chrome_open", {"url": "file:///etc/passwd"})

    class Socket:
        url = "ws://127.0.0.1:9333/devtools/page/t"
        calls = []

        async def call(self, method, params=None, timeout=30):
            self.calls.append((method, params or {}))
            if method == "Runtime.evaluate" and "=== '1'" in (params or {}).get("expression", ""):
                return {"result": {"objectId": "obj1", "className": "HTMLInputElement"}}  # the stamped target
            if method == "Runtime.evaluate":
                return {"result": {"value": {"x": 10, "y": 20, "tag": "input", "label": "Select files", "type": "file"}}}
            return {}

    chrome.tabs[session["id"]] = {"target": "t", "socket": Socket()}
    (agent.workspace(session["id"]) / "reel.mp4").write_bytes(b"video")
    pending = asyncio.create_task(chrome.execute(session, "chrome_upload", {"path": "reel.mp4"}))
    for _ in range(100):
        if agent.approvals:
            break
        await asyncio.sleep(0.01)
    aid = next(iter(agent.approvals))
    assert agent.approvals[aid]["name"] == "chrome_upload", "an upload waits for the owner's approval"
    agent.decide(aid, False, admin=True)
    assert await pending == {"denied": True}
    store.update_session(session["id"], auto_approve=1)
    done = await chrome.execute(session, "chrome_upload", {"path": "reel.mp4"})
    assert done["uploaded"].endswith("reel.mp4") and done["bytes"] == 5
    assert any(m == "DOM.setFileInputFiles" and p["objectId"] == "obj1" for m, p in Socket.calls)
    with pytest.raises(ValueError, match="not found"):
        await chrome.execute(session, "chrome_upload", {"path": "missing.mp4"})
    pressed = await chrome.execute(session, "chrome_press", {"key": "ctrl+a"})
    assert pressed == {"pressed": "ctrl+a"}
    key_events = [p for m, p in Socket.calls if m == "Input.dispatchKeyEvent"]
    assert key_events[-2]["modifiers"] == 2 and key_events[-2]["key"] == "a" and key_events[-2]["type"] == "keyDown"


async def test_the_mcp_bridge_returns_a_screenshot_as_an_image_block(bundle):
    config, store, agent, chrome = bundle
    session = store.resolve(3, 0, 1)
    store.update_session(session["id"], model="deepseek-flash")
    session = store.session(session["id"])

    class Socket:
        url = "ws://x"

        async def call(self, method, params=None, timeout=30):
            assert method == "Page.captureScreenshot"
            return {"data": "iVBORw0KGgo="}

    chrome.tabs[session["id"]] = {"target": "t", "socket": Socket()}
    bridge = ToolBridge(agent)
    _, answer = await bridge.handle(session, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "chrome_screenshot", "arguments": {}}})
    blocks = answer["result"]["content"]
    assert blocks[0]["type"] == "text" and '"shown": true' in blocks[0]["text"]
    assert blocks[1] == {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
    assert agent.pending_images.get(session["id"]) in (None, []), "the bridge consumed the queued image"
    assert any(e["kind"] == "media" for e in store.events(session["id"]))
    await asyncio.sleep(0)
