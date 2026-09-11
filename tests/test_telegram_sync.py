"""Two-way Telegram mirror: topics, outbound mirroring, media storage and inbound continuation."""

import asyncio
import json

import httpx
import pytest

from connector.agent import Agent
from connector.config import Config, password_hash
from connector.providers import TelegramAPI
from connector.store import Store
from connector.sync import SessionSync, split_text
from connector.telegram import Bot

CHAT = -1001234567890


class FakeTelegram:
    """Records every Bot API call and answers like Telegram would."""

    def __init__(self):
        self.calls = []
        self.topics = 0
        self.messages = 0
        self.files = {}
        self.flood = {}
        self.fail = set()

    def payload(self, request):
        if request.headers.get("content-type", "").startswith("multipart/"):
            body = request.content.decode("latin-1")
            return {"multipart": True, "body": body}
        return json.loads(request.content or b"{}")

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = self.payload(request)
        self.calls.append((method, payload))
        left = self.flood.get(method, 0)
        if left:
            self.flood[method] = left - 1
            return httpx.Response(200, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 0}})
        if method in self.fail:
            return httpx.Response(200, json={"ok": False, "error_code": 400, "description": "forum disabled"})
        if method == "createForumTopic":
            self.topics += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_thread_id": 100 + self.topics,
                                                                    "name": payload.get("name", "")}})
        if method in ("editForumTopic", "closeForumTopic", "deleteMessage"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/file.bin", "file_size": 9}})
        if method.startswith("send"):
            self.messages += 1
            result = {"message_id": self.messages}
            if method == "sendPhoto":
                result["photo"] = [{"file_id": "photo-id", "file_unique_id": "pu", "file_size": 10}]
            elif method == "sendDocument":
                result["document"] = {"file_id": "doc-id", "file_unique_id": "du", "file_size": 10}
            elif method == "sendVideo":
                result["video"] = {"file_id": "video-id", "file_unique_id": "vu", "file_size": 10}
            return httpx.Response(200, json={"ok": True, "result": result})
        return httpx.Response(200, json={"ok": True, "result": {}})

    def transport(self):
        def route(request):
            if "/file/bot" in request.url.path:
                return httpx.Response(200, content=b"restored!")
            return self.handler(request)
        return httpx.MockTransport(route)

    def sent(self, method=None):
        return [c for c in self.calls if method is None or c[0] == method]

    def texts(self):
        return [c[1]["text"] for c in self.calls if c[0] == "sendMessage"]


@pytest.fixture
async def mirror(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password"),
                         chat_password=password_hash("chat-password"),
                         telegram_token="test-token", telegram_sync_chat_id=str(CHAT))
    fake = FakeTelegram()
    client = httpx.AsyncClient(transport=fake.transport())
    store = Store(tmp_path / "sync.sqlite3")
    telegram = TelegramAPI(config, client)
    agent = Agent(config, store, None, telegram)
    sync = SessionSync(config, store, telegram, agent)
    sync.interval = 0
    agent.sync = sync
    sync.start()
    yield config, store, agent, sync, fake
    await sync.close()
    await client.aclose()
    store.db.close()


async def close(sync):
    await sync.drain()
    await sync.close()


def ide_session(store, title="IDE session"):
    session = store.resolve(0, 0, 0, title, new=True)
    return store.update_session(session["id"], provider="codex", model="gpt-5")


async def test_ide_session_gets_topic_and_header(mirror):
    _, store, _, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    await sync.drain()
    assert session["chat_id"] == CHAT and session["topic_id"] == 101
    assert fake.sent("createForumTopic")[0][1]["name"] == "IDE session"
    header = fake.texts()[0]
    assert session["id"] in header and "codex" in header
    # Idempotent: a second call creates nothing.
    await sync.ensure_topic(store.session(session["id"]))
    assert len(fake.sent("createForumTopic")) == 1
    await sync.close()


async def test_topic_failure_falls_back_and_retries_later(mirror):
    _, store, _, sync, fake = mirror
    fake.fail.add("createForumTopic")
    session = await sync.ensure_topic(ide_session(store))
    assert session["chat_id"] == CHAT and session["topic_id"] == 0
    assert store.sync_state(session["id"])["status"] == "fallback"
    assert any("топик" in e["payload"].get("text", "") for e in store.events(session["id"]))
    fake.fail.clear()
    session = await sync.ensure_topic(store.session(session["id"]))
    assert session["topic_id"] == 101
    await close(sync)


async def test_user_message_and_answer_are_mirrored_once(mirror):
    _, store, agent, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    await sync.mirror_user(session, "make it green")
    await agent.tell(session, "done, it is green now")
    await sync.drain()
    assert "👤 make it green" in fake.texts()
    assert fake.texts().count("done, it is green now") == 1
    # Restarting the mirror must not repost what the pointer already covers.
    restarted = SessionSync(sync.config, store, sync.telegram, agent)
    restarted.interval = 0
    restarted.start()
    assert await restarted.catch_up(session["id"]) == 0
    await restarted.drain()
    assert fake.texts().count("done, it is green now") == 1
    await restarted.close()
    await close(sync)


async def test_catch_up_mirrors_events_recorded_while_offline(mirror):
    _, store, agent, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    await sync.drain()
    await sync.close()  # mirror down: the observer no longer sees the journal
    store.event(session["id"], "assistant", {"text": "offline answer"})
    restarted = SessionSync(sync.config, store, sync.telegram, agent)
    restarted.interval = 0
    restarted.start()
    assert await restarted.catch_up(session["id"]) == 1
    await restarted.drain()
    assert "offline answer" in fake.texts()
    await restarted.close()


async def test_tool_calls_are_grouped_into_one_message(mirror):
    _, store, _, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    sid = session["id"]
    for name in ("read_file", "read_file", "write_file"):
        store.event(sid, "tool", {"name": name})
    store.event(sid, "turn_completed", {})
    await sync.drain()
    summaries = [t for t in fake.texts() if t.startswith("🛠")]
    assert summaries == ["🛠 Инструменты за ход: read_file ×2, write_file"]
    await close(sync)


def test_long_messages_split_with_markers():
    parts = split_text("line\n" * 3000)
    assert len(parts) > 1
    assert all(len(part) <= 4096 for part in parts)
    assert parts[0].endswith(f"… (1/{len(parts)})")


async def test_media_is_uploaded_and_file_id_stored(mirror):
    _, store, agent, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    sid = session["id"]
    (agent.workspace(sid) / "shot.png").write_bytes(b"image bytes")
    store.event(sid, "media", {"path": "shot.png", "kind": "image", "direction": "out"})
    await sync.drain()
    assert fake.sent("sendPhoto")
    record = store.media_file(sid, "shot.png")
    assert record["file_id"] == "photo-id" and record["sha256"] and record["kind"] == "photo"
    # A repeated media event for the same file does not upload it again.
    store.event(sid, "media", {"path": "shot.png", "kind": "image", "direction": "out"})
    await sync.drain()
    assert len(fake.sent("sendPhoto")) == 1
    await close(sync)


async def test_evict_needs_file_id_and_restore_downloads_again(mirror):
    _, store, agent, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    sid = session["id"]
    path = agent.workspace(sid) / "report.bin"
    path.write_bytes(b"payload")
    with pytest.raises(ValueError):
        sync.evict(sid, "report.bin")  # never evict without a verified file_id
    assert path.exists()
    store.event(sid, "media", {"path": "report.bin", "kind": "document", "direction": "web"})
    await sync.drain()
    assert sync.evict(sid, "report.bin")["evicted"] and not path.exists()
    restored = await sync.restore(sid, "report.bin")
    assert restored.read_bytes() == b"restored!"
    await close(sync)


async def test_offload_evicts_only_large_files(mirror):
    config, store, agent, sync, fake = mirror
    config.values.update(telegram_media_offload=True, telegram_media_offload_mb=1)
    session = await sync.ensure_topic(ide_session(store))
    sid = session["id"]
    small, big = agent.workspace(sid) / "small.bin", agent.workspace(sid) / "big.bin"
    small.write_bytes(b"x" * 1000)
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    for name in ("small.bin", "big.bin"):
        store.event(sid, "media", {"path": name, "kind": "document", "direction": "web"})
    await sync.drain()
    assert small.exists() and not big.exists()
    assert store.media_file(sid, "big.bin")["file_id"] == "doc-id"
    await close(sync)


async def test_flood_control_is_retried(mirror):
    _, store, _, sync, fake = mirror
    fake.flood["createForumTopic"] = 1
    session = await sync.ensure_topic(ide_session(store))
    assert session["topic_id"] == 101
    assert len(fake.sent("createForumTopic")) == 2
    await close(sync)


async def test_rename_and_delete_touch_the_topic(mirror):
    _, store, _, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    assert await sync.rename(session, "renamed session")
    assert fake.sent("editForumTopic")[0][1]["name"] == "renamed session"
    store.execute("UPDATE sessions SET deleted=1,active=0 WHERE id=?", (session["id"],))
    assert await sync.closed(store.session(session["id"]))
    assert fake.sent("closeForumTopic")
    assert not fake.sent("deleteForumTopic")
    await close(sync)


async def test_backfill_is_idempotent(mirror):
    _, store, _, sync, fake = mirror
    for index in range(3):
        ide_session(store, f"session {index}")
    first = await sync.backfill()
    await sync.drain()
    assert first["created"] == 3
    second = await sync.backfill()
    assert second["created"] == 0 and second["skipped"] == 3
    assert len(fake.sent("createForumTopic")) == 3
    await close(sync)


async def test_inbound_topic_message_continues_the_ide_session(mirror):
    config, store, agent, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    await sync.drain()
    store.execute("INSERT INTO users VALUES (7,?,0)", (config["auth_epoch"],))
    started = []
    agent.start = lambda target, content: started.append((target["id"], content))
    bot = Bot(config, store, sync.telegram, agent)
    await bot.handle({"message": {"message_id": 5, "text": "continue from my phone", "from": {"id": 7},
                                  "chat": {"id": CHAT, "type": "supergroup"},
                                  "message_thread_id": session["topic_id"]}})
    assert started == [(session["id"], "continue from my phone")]
    assert store.session(session["id"])["user_id"] == 7  # the topic was adopted, not duplicated
    assert len(store.sessions()) == 1
    await close(sync)


async def test_inbound_message_is_queued_while_a_turn_runs(mirror):
    config, store, agent, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    store.execute("INSERT INTO users VALUES (7,?,0)", (config["auth_epoch"],))
    sid = session["id"]

    async def busy():
        await asyncio.sleep(0.2)
    agent.jobs[sid] = asyncio.create_task(busy())
    bot = Bot(config, store, sync.telegram, agent)
    await bot.handle({"message": {"message_id": 6, "text": "queued task", "from": {"id": 7},
                                  "chat": {"id": CHAT, "type": "supergroup"},
                                  "message_thread_id": session["topic_id"]}})
    assert [q["payload"] for q in store.queued(sid)] == ["queued task"]
    assert any(e["kind"] == "user" and e["payload"].get("queued") for e in store.events(sid))
    agent.jobs[sid].cancel()
    await close(sync)


def test_api_creates_topics_mirrors_messages_and_offloads_media(tmp_path):
    """End to end through the HTTP API: session create, message, upload, sync report, evict."""
    from fastapi.testclient import TestClient

    from connector.app import create_app

    fake = FakeTelegram()
    app = create_app(tmp_path, polling=False)
    app.state.config.values.update(telegram_token="test-token", telegram_sync_chat_id=str(CHAT),
                                   admin_password=password_hash("admin-password"),
                                   chat_password=password_hash("chat-password"), setup_token="")
    app.state.sync.telegram.client = httpx.AsyncClient(transport=fake.transport())
    app.state.sync.interval = 0
    with TestClient(app) as client:
        client.headers.update({"Authorization": "Bearer " + app.state.config["connector_token"],
                               "X-Requested-With": "DeepSeekIDE"})
        sid = client.post("/api/sessions", json={"title": "mirrored task"}).json()["id"]
        assert fake.sent("createForumTopic")
        app.state.agent.start = lambda session, content: None
        assert client.post(f"/api/sessions/{sid}/messages", json={"text": "hello from the IDE"}).status_code == 202
        name = client.post(f"/api/sessions/{sid}/files", files={"file": ("note.txt", b"stored in telegram")}).json()["path"]
        assert client.post("/api/sync/backfill").json()["enabled"]  # also drains the mirror queue
        assert "👤 hello from the IDE" in fake.texts()
        state = client.get(f"/api/sessions/{sid}/sync").json()
        assert state["topic_id"] and state["media_count"] == 1 and state["topic_url"].startswith("https://t.me/c/")
        assert client.get("/api/sessions").json()[0]["telegram"]["topic_id"] == state["topic_id"]
        assert client.post(f"/api/sessions/{sid}/media/evict", json={"path": name}).json()["evicted"]
        assert client.get(f"/api/sessions/{sid}/media/telegram", params={"path": name}).content == b"restored!"
        assert client.patch(f"/api/sessions/{sid}", json={"title": "renamed"}).status_code == 200
        assert fake.sent("editForumTopic")
        assert client.delete(f"/api/sessions/{sid}").json()["deleted"]
        assert fake.sent("closeForumTopic") and not fake.sent("deleteForumTopic")


async def test_sync_info_reports_topic_url_and_counters(mirror):
    _, store, agent, sync, fake = mirror
    session = await sync.ensure_topic(ide_session(store))
    (agent.workspace(session["id"]) / "a.png").write_bytes(b"x")
    store.event(session["id"], "media", {"path": "a.png", "kind": "image", "direction": "out"})
    await sync.drain()
    info = sync.info(store.session(session["id"]))
    assert info["topic_url"] == f"https://t.me/c/1234567890/{session['topic_id']}"
    assert info["media_count"] == 1 and info["media_in_telegram"] == 1 and info["pending_uploads"] == 0
    assert info["last_mirrored_event"] > 0
    await close(sync)
