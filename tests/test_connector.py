import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent, safe_path
from connector.app import create_app
from connector.config import Config, password_hash, verify_password
from connector.providers import DeepSeek, account_usage
from connector.store import Store
from connector.telegram import Bot


@pytest.fixture
def config(tmp_path):
    c = Config(tmp_path)
    c.values.update(admin_password=password_hash("admin-password"), chat_password=password_hash("chat-password"))
    return c


@pytest.fixture
def bundle(config):
    store = Store(config.root / "test.db")
    telegram = AsyncMock()
    agent = Agent(config, store, AsyncMock(), telegram)
    yield config, store, telegram, agent
    store.db.close()


def test_passwords_are_salted():
    first = password_hash("example password")
    assert verify_password("example password", first)
    assert not verify_password("wrong", first)
    assert first != password_hash("example password")
    assert not verify_password("password", "")


@pytest.mark.parametrize("value", ["../secret", "/etc/passwd", "C:/Windows/test", "a/../../b", ".env", ".git/config", "a\\b"])
def test_path_escape_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        safe_path(tmp_path, value)


def test_topics_and_owners_are_isolated(bundle):
    _, store, _, _ = bundle
    a = store.resolve(100, 1, 10)
    assert store.resolve(100, 1, 10)["id"] == a["id"]
    assert store.resolve(100, 2, 10)["id"] != a["id"]
    assert store.resolve(100, 1, 11)["id"] != a["id"]
    b = store.resolve(100, 1, 10, new=True)
    assert b["id"] != a["id"]
    assert not store.session(a["id"])["active"]


def test_real_cache_accounting_and_peak(config):
    raw = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_cache_hit_tokens": 800,
           "prompt_cache_miss_tokens": 200}
    peak = account_usage(raw, "deepseek-flash", config, datetime(2026, 9, 11, 8, tzinfo=timezone.utc))
    off = account_usage(raw, "deepseek-flash", config, datetime(2026, 9, 12, 8, tzinfo=timezone.utc))
    assert peak["cost_usd"] == pytest.approx((800*.006 + 200*.3 + 200*1.2)/1e6)
    assert peak["saved_usd"] == pytest.approx(800*(.3-.006)/1e6)
    assert off["cost_usd"] == peak["cost_usd"] / 2
    missing = account_usage({"prompt_tokens": 1000}, "deepseek-flash", config)
    assert missing["cost_usd"] is None and missing["cache_hit_tokens"] is None
    assert missing["unknown_cache_requests"] == 1
    standard = account_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 64}}, "deepseek-flash", config)
    assert standard["cache_miss_tokens"] == 36


async def test_read_dedup_and_revision_approval(bundle):
    _, store, _, agent = bundle
    session = store.resolve(0, 0, 1)
    path = agent.workspace(session["id"]) / "hello.py"
    path.write_text("first", encoding="utf-8")
    assert (await agent.execute(session, "read_file", {"path": "hello.py"}))["text"] == "first"
    assert (await agent.execute(session, "read_file", {"path": "hello.py"}))["unchanged"]
    task = asyncio.create_task(agent.execute(session, "write_file", {"path": "hello.py", "content": "second"}))
    await asyncio.sleep(.01)
    aid = next(iter(agent.approvals))
    with pytest.raises(ValueError):
        agent.decide(aid, True, user_id=99)
    path.write_text("external edit", encoding="utf-8")
    agent.decide(aid, True, user_id=1)
    with pytest.raises(ValueError, match="changed since review"):
        await task
    assert path.read_text() == "external edit"


async def test_write_denial_and_approval(bundle):
    _, store, _, agent = bundle
    s = store.resolve(0, 0, 1)
    for accepted in (False, True):
        task = asyncio.create_task(agent.execute(s, "write_file", {"path": "new.txt", "content": "ok"}))
        await asyncio.sleep(.01)
        agent.decide(next(iter(agent.approvals)), accepted, admin=True)
        result = await task
        assert ("written" in result) == accepted
        assert (agent.workspace(s["id"]) / "new.txt").exists() == accepted


async def test_command_disabled(bundle):
    _, store, _, agent = bundle
    s = store.resolve(0, 0, 1)
    result = await agent.execute(s, "run_command", {"argv": ["whoami"]})
    assert "disabled" in result["error"]


async def test_streaming_reasoning_tool_fragments_and_usage(config):
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "plan"}}]},
        {"choices": [{"delta": {"content": "hello"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x.txt"}'}}]}}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 64}},
    ]
    async def handler(request):
        assert json.loads(request.content)["stream_options"]["include_usage"]
        return httpx.Response(200, text="".join("data: "+json.dumps(c)+"\n\n" for c in chunks)+"data: [DONE]\n\n")
    config.values["deepseek_key"] = "test-only-key"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        delta = AsyncMock()
        message, usage = await DeepSeek(config, client).complete([], [], delta)
    assert message["reasoning_content"] == "plan"
    assert message["content"] == "hello"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"path": "x.txt"}
    assert usage["cache_hit_tokens"] == 64
    assert delta.await_count == 2


def test_interrupted_history_has_tool_results(bundle):
    _, store, _, agent = bundle
    s = store.resolve(0, 0, 1)
    store.message(s["id"], {"role": "assistant", "tool_calls": [{"id": "unfinished"}]})
    agent.repair_history(s["id"])
    assert store.history(s["id"])[-1]["tool_call_id"] == "unfinished"
    agent.repair_history(s["id"])
    assert len(store.history(s["id"])) == 2


async def test_bot_authentication_any_message_and_rotation(bundle):
    c, store, tg, agent = bundle
    bot = Bot(c, store, tg, agent)
    def update(text, private=True):
        return {"message": {"message_id": 1, "text": text, "from": {"id": 42},
                            "chat": {"id": 42 if private else -100, "type": "private" if private else "supergroup"}}}
    await bot.handle(update("hello"))
    assert "пароль" in tg.text.call_args.args[1].lower()
    await bot.handle(update("/unknown"))
    assert "пароль" in tg.text.call_args.args[1].lower()
    await bot.handle(update("chat-password", private=False))
    assert not bot.authorized(42)
    await bot.handle(update("/auth chat-password"))
    assert bot.authorized(42)
    assert "chat-password" not in str(store.sessions())
    c.values["auth_epoch"] += 1
    assert not bot.authorized(42)


@pytest.mark.parametrize("kind", ["photo", "document", "audio", "voice", "video", "animation", "video_note", "sticker"])
async def test_inbound_media_reaches_session(bundle, kind):
    c, store, tg, agent = bundle
    store.execute("INSERT INTO users VALUES (1,?,0)", (c["auth_epoch"],))
    async def download(file_id, path):
        path.write_bytes(b"test file")
    tg.download.side_effect = download
    agent.start = lambda session, content: store.event(session["id"], "test_received", {"content": content})
    media = {"file_id": "fixture", "file_name": "fixture.txt"}
    update = {"message": {"message_id": 1, "from": {"id": 1}, "chat": {"id": 1, "type": "private"},
                          kind: [media] if kind == "photo" else media}}
    await Bot(c, store, tg, agent).handle(update)
    events = store.events(store.sessions()[0]["id"])
    assert events[0]["kind"] == "media"
    assert events[0]["payload"]["kind"] == kind
    assert events[-1]["kind"] == "test_received"


def test_admin_security_setup_upload_and_openapi(tmp_path):
    app = create_app(tmp_path, polling=False)
    with TestClient(app) as client:
        assert client.get("/api/sessions").status_code == 401
        assert client.post("/api/setup", json={}).status_code == 403
        headers = {"Authorization": "Bearer " + app.state.config["setup_token"]}
        body = {"admin_password": "test-admin-password", "chat_password": "test-chat-password"}
        assert client.post("/api/setup", json=body, headers=headers).status_code == 200
        assert client.post("/api/setup", json=body, headers=headers).status_code == 403
        assert client.post("/api/login", json={"password": "test-admin-password"}).status_code == 200
        assert client.post("/api/sessions", json={}).status_code == 403
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        sid = client.post("/api/sessions", json={"title": "safe fixture"}).json()["id"]
        result = client.post(f"/api/sessions/{sid}/files", files={"file": ("hello.txt", b"safe content")})
        assert result.status_code == 200
        name = result.json()["path"]
        assert client.get(f"/api/sessions/{sid}/file", params={"path": name}).content == b"safe content"
        assert client.get(f"/api/sessions/{sid}/file", params={"path": "../../config.json"}).status_code == 400
        public = client.get("/api/settings").json()
        assert "connector_token" not in public and "admin_password" not in public
        paths = client.get("/openapi.json").json()["paths"]
        assert "/v1/chat/completions" in paths and "/api/sessions/{sid}/stream" in paths
        assert client.post("/api/sessions", json={}, headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/").status_code == 200


def test_usage_and_history_persist_after_reopen(tmp_path):
    store = Store(tmp_path / "persistence.db")
    s = store.resolve(123, 7, 99)
    store.message(s["id"], {"role": "user", "content": "persistent"})
    store.add_usage(s["id"], {"prompt_tokens": 123, "completion_tokens": 7})
    store.db.close()
    reopened = Store(tmp_path / "persistence.db")
    assert reopened.history(s["id"])[0]["content"] == "persistent"
    assert reopened.usage(s["id"])["prompt_tokens"] == 123
    reopened.db.close()


async def test_group_upgrade_rebinds_without_model_call(bundle):
    c, store, tg, agent = bundle
    session = store.resolve(-123, 0, 42)
    bot = Bot(c, store, tg, agent)
    await bot.handle({"message": {"message_id": 12, "chat": {"id": -123}, "migrate_to_chat_id": -100456}})
    assert store.session(session["id"])["chat_id"] == -100456
    assert not store.history(session["id"])
    tg.text.assert_not_called()
