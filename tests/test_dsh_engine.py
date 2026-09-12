"""AIGent sessions on the DeepSeek Harness engine: the runtime's events become the journal the
interface renders, the answer reaches Telegram, a mid-turn message joins the running turn, stop
kills the runtime, and AIGent's own tools are offered through the per-session MCP patch."""

import asyncio
import base64
import json
import queue
import time
from unittest.mock import AsyncMock

import pytest

from connector.agent import Agent
from connector.config import Config, password_hash
from connector.dsh_engine import MCP_PREFIX, DshEngine
from connector.store import Store


class Note:
    def __init__(self, method, payload):
        self.method, self.payload = method, payload


def event(session, kind, data, seq=0):
    return Note("session.event", {"sessionId": session, "event": {"type": kind, "seq": seq, "time": 0, "data": data}})


class FakeSubscription:
    def __init__(self):
        self.queue = queue.Queue()
        self.closed = False

    def drain(self, on_notification):
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, BaseException):
                raise item
            on_notification(item)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, script):
        self.script, self.prompts, self.subscriptions = script, [], []

    def subscribe_session_notifications(self, session_id):
        sub = FakeSubscription()
        self.subscriptions.append(sub)
        return sub

    def session_prompt(self, session_id, blocks, notification_subscription=None):
        self.prompts.append((session_id, blocks))
        message_id = f"m{len(self.prompts)}"
        for note in self.script(session_id, blocks, message_id, len(self.prompts)):
            notification_subscription.queue.put(note)
        return message_id


class FakeHarness:
    """Stands in for deepseek_harness.DeepSeekHarness: records options, never spawns anything."""

    instances = []
    script = None

    def __init__(self, **options):
        self.options = options
        self.client = FakeClient(self.script)
        self.started = self.closed = False
        FakeHarness.instances.append(self)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


def full_turn(session, blocks, mid, n):
    """A turn with reasoning, one AIGent tool call, a worker, a goal and a final answer."""
    child = session + "-child"
    return [
        event(session, "agent/inbox/spliced", {"target": "next-turn", "inserted": [{"id": mid, "content": blocks}]}),
        Note("session.status", {"sessionId": session, "status": "running"}),
        event(session, "turn/start", {"turn": 1}),
        event(session, "step/start", {"turn": 1, "step": 1}),
        event(session, "assistant/message", {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [
            {"type": "reasoning", "text": "Нужна картинка с фермы."}, {"type": "text", "text": "Рендерю на ферме."}]},
            "usage": {"inputTokens": 300, "outputTokens": 50, "cacheReadTokens": 7000, "reasoningTokens": 20}}),
        event(session, "tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": MCP_PREFIX + "shared_image",
                                     "arguments": json.dumps({"prompt": "луна"})}),
        event(session, "tool/result", {"turn": 1, "step": 1, "message": {"source": {"kind": "tool", "callId": "c1"},
              "content": [{"type": "tool-result", "toolCallId": "c1", "content": [{"type": "text", "text": json.dumps({"path": "shared/moon.png"})}]}]}}),
        event(session, "goal/change", {"id": "g1", "revision": 1, "objective": "Собрать ролик с Луны", "phase": "active",
                                       "maxGoalRounds": 8}),
        event(session, "step/end", {"turn": 1, "step": 1}),
        Note("subagent.started", {"provider": "spawn", "agentId": child, "parentSessionId": session,
                                  "childSessionId": child, "label": "Проверить кадры"}),
        event(child, "step/start", {"turn": 1, "step": 1}),
        event(child, "tool/call", {"turn": 1, "step": 1, "callId": "k1", "name": "read", "arguments": json.dumps({"file_path": "a.py"})}),
        event(child, "assistant/message", {"turn": 1, "step": 2, "message": {"role": "assistant", "content": [{"type": "text", "text": "Кадры на месте."}]},
                                           "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 100}}),
        Note("subagent.finished", {"provider": "spawn", "agentId": child, "parentSessionId": session,
                                   "childSessionId": child, "status": "ok", "stopReason": "completed"}),
        event(session, "step/start", {"turn": 1, "step": 2}),
        event(session, "assistant/message", {"turn": 1, "step": 2, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Готово: кадр отрендерен, ролик собран."}]},
            "usage": {"inputTokens": 100, "outputTokens": 30, "cacheReadTokens": 7300}}),
        event(session, "step/end", {"turn": 1, "step": 2}),
        event(session, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        Note("session.status", {"sessionId": session, "status": "idle"}),
    ]


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("AIGENT_PORT", "8787")
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"),
                         deepseek_key="test-key", model="deepseek-v4-pro")
    store = Store(config.root / "dsh.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    FakeHarness.instances = []
    FakeHarness.script = staticmethod(full_turn)
    engine = DshEngine(agent, config, store, factory=FakeHarness)
    agent.engine = engine
    yield config, store, agent, engine
    store.db.close()


# 1 ---------------------------------------------------------------------------------
async def test_a_turn_becomes_the_journal_the_interface_renders(bundle):
    config, store, agent, engine = bundle
    session = store.resolve(1, 0, 1)
    sid = session["id"]

    await agent.run(session, "сделай ролик с Луны")

    harness = FakeHarness.instances[0]
    assert harness.started and harness.options["profile"] == "sdk" and harness.options["model"] == "deepseek-v4-pro"
    assert harness.options["env"]["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert harness.options["env"]["DEEPSEEK_API_KEY"] == "test-key" and harness.options["cwd"] == str(agent.workspace(sid))
    kinds = [e["kind"] for e in store.events(sid)]
    for kind in ("user", "stream", "assistant", "tool", "tool_result", "subagent", "goal", "usage", "turn_completed"):
        assert kind in kinds, kind
    tool = next(e["payload"] for e in store.events(sid) if e["kind"] == "tool")
    assert tool["name"] == "shared_image" and tool["arguments"] == {"prompt": "луна"} and tool["engine"] == "dsh"
    result = next(e["payload"] for e in store.events(sid) if e["kind"] == "tool_result")
    assert result["name"] == "shared_image" and result["result"] == {"path": "shared/moon.png"}
    commentary = [e["payload"] for e in store.events(sid) if e["kind"] == "assistant"]
    assert commentary[0]["text"] == "Рендерю на ферме." and commentary[0]["phase"] == "commentary"
    assert commentary[-1]["text"] == "Готово: кадр отрендерен, ролик собран." and "phase" not in commentary[-1]
    agent.telegram.text.assert_awaited()
    assert "Готово: кадр отрендерен" in agent.telegram.text.await_args_list[0].args[1]
    reasoning = next(e["payload"] for e in store.events(sid) if e["kind"] == "stream")
    assert reasoning["reasoning"] == "Нужна картинка с фермы." and reasoning["done"] is True
    assert agent.goal(sid)["goal"] == "Собрать ролик с Луны" and agent.goal(sid)["status"] == "active"
    workers = [e["payload"] for e in store.events(sid) if e["kind"] == "subagent"]
    assert [w["phase"] for w in workers] == ["spawn", "update", "done"]
    assert workers[-1]["status"] == "done" and workers[-1]["goal"] == "Проверить кадры" and workers[1]["activity"] == "read"
    usage = store.usage(sid)
    assert usage["requests"] == 3 and usage["cache_hit_tokens"] == 7000 + 100 + 7300
    assert usage["prompt_tokens"] == 7300 + 110 + 7400 and usage["completion_tokens"] == 85
    assert store.session(sid)["status"] == "idle" and agent._delivered[sid] is True
    assert not any("Продолжу сам" in (e["payload"].get("text") or "") for e in store.events(sid) if e["kind"] == "notice"), \
        "goal rounds are the engine's job: AIGent schedules no continuation"


# 2 ---------------------------------------------------------------------------------
async def test_the_session_patch_offers_aigent_tools_over_mcp_and_disables_telemetry(bundle):
    config, store, agent, engine = bundle
    session = store.resolve(2, 0, 1)
    sid = session["id"]

    await agent.run(session, "привет")

    patch = engine.home() / "patches" / f"{sid}.patch.yml"
    text = patch.read_text(encoding="utf-8")
    assert f"http://127.0.0.1:8787/api/mcp/{sid}" in text and config["connector_token"] in text
    assert "serverName: aigent" in text and "transport: streamable-http" in text
    assert "mode: DISABLED" in text and ".claude/skills" in text
    assert FakeHarness.instances[0].options["patches"] == (str(patch),)
    instructions = (engine.home() / "AGENTS.md").read_text(encoding="utf-8")
    assert "mcp__aigent__restart_server" in instructions and "Infer the goal and do the work" in instructions
    assert config["connector_token"] not in instructions


# 3 ---------------------------------------------------------------------------------
async def test_a_message_queued_mid_turn_is_forwarded_into_the_running_turn(bundle):
    config, store, agent, engine = bundle
    session = store.resolve(3, 0, 1)
    sid = session["id"]

    def script(dsh_session, blocks, mid, n):
        if n == 1:  # the turn keeps running: no idle until the owner's second message arrives
            return [event(dsh_session, "agent/inbox/spliced", {"inserted": [{"id": mid}]}),
                    Note("session.status", {"sessionId": dsh_session, "status": "running"}),
                    event(dsh_session, "step/start", {"turn": 1, "step": 1})]
        return [event(dsh_session, "assistant/message", {"turn": 1, "step": 2, "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Учёл: " + blocks[0]["text"]}]}}),
                event(dsh_session, "step/end", {"turn": 1, "step": 2}),
                event(dsh_session, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
                Note("session.status", {"sessionId": dsh_session, "status": "idle"})]

    FakeHarness.script = staticmethod(script)
    store.queue_message(sid, "и ещё добавь тест")

    await asyncio.wait_for(agent.run(session, "поработай"), 10)

    client = FakeHarness.instances[0].client
    assert len(client.prompts) == 2 and client.prompts[1][1] == [{"type": "text", "text": "и ещё добавь тест"}]
    assert store.queued(sid) == []
    events = [(e["kind"], e["payload"]) for e in store.events(sid)]
    assert any(k == "user" and p.get("inline") and p["text"] == "и ещё добавь тест" for k, p in events)
    assert any(k == "queue_started" and p.get("inline") for k, p in events)
    assert "Учёл: и ещё добавь тест" in agent.telegram.text.await_args_list[0].args[1]


# 4 ---------------------------------------------------------------------------------
async def test_stop_kills_the_runtime_and_the_next_turn_starts_a_fresh_engine_session_with_a_brief(bundle):
    config, store, agent, engine = bundle
    session = store.resolve(4, 0, 1)
    sid = session["id"]
    store.event(sid, "assistant", {"text": "Ранее: кадры готовы, ролик не собран."})
    agent.save_goal(sid, goal="Собрать ролик", status="active")

    def endless(dsh_session, blocks, mid, n):
        return [event(dsh_session, "agent/inbox/spliced", {"inserted": [{"id": mid}]}),
                Note("session.status", {"sessionId": dsh_session, "status": "running"})]

    FakeHarness.script = staticmethod(endless)
    agent.start(session, "продолжай")
    await asyncio.sleep(0.3)
    assert agent.busy(sid) and len(engine.runtimes) == 1
    first = FakeHarness.instances[0]
    first_blocks = first.client.prompts[0][1]
    assert first_blocks[0]["text"].startswith("Контекст AIGent") and "Собрать ролик" in first_blocks[0]["text"]
    assert "кадры готовы" in first_blocks[0]["text"] and first_blocks[1] == {"type": "text", "text": "продолжай"}

    assert agent.stop(sid) is True
    await asyncio.sleep(0.2)
    assert first.closed and not engine.runtimes and not agent.busy(sid)
    assert any("остановлен" in (e["payload"].get("text") or "") for e in store.events(sid) if e["kind"] == "notice")

    FakeHarness.script = staticmethod(full_turn)
    await agent.run(session, "дальше")
    second = FakeHarness.instances[1]
    assert second.started and second.client.prompts[0][0] != first.client.prompts[0][0], "a new engine session id"
    assert second.client.prompts[0][1][0]["text"].startswith("Контекст AIGent"), "the brief again: fresh runtime"


# 5 ---------------------------------------------------------------------------------
async def test_images_go_inline_to_a_vision_model_and_as_descriptions_to_a_blind_one(bundle):
    config, store, agent, engine = bundle
    session = store.resolve(5, 0, 1)
    sid = session["id"]
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"png").decode()}}
    content = [{"type": "text", "text": "что на картинке?"}, picture]

    class Eyes:
        async def complete(self, messages, tools, delta):
            return {"role": "assistant", "content": "Скриншот сайдбара с одной строкой списка."}, {"prompt_tokens": 1, "completion_tokens": 1}

    agent.vision_backend = lambda session: Eyes()
    await agent.run(session, content)
    blind = FakeHarness.instances[0].client.prompts[0][1]
    assert blind[-1]["type"] == "text" and "Скриншот сайдбара" in blind[-1]["text"]

    store.update_session(sid, model="deepseek-flash")
    session = store.session(sid)
    await agent.run(session, content)
    seeing = FakeHarness.instances[-1].client.prompts[0][1]
    assert seeing[-1] == {"type": "image", "data": base64.b64encode(b"png").decode(), "mimeType": "image/png"}
    assert FakeHarness.instances[0].closed, "the model changed: the old runtime was replaced"


# 6 ---------------------------------------------------------------------------------
async def test_legacy_engine_setting_keeps_the_connectors_own_loop(bundle):
    config, store, agent, engine = bundle
    config.values["engine"] = "legacy"
    session = store.resolve(6, 0, 1)
    assert engine.handles(session) is False
    agent.deepseek = AsyncMock(complete=AsyncMock(return_value=({"role": "assistant", "content": "по-старому"},
                                                                  {"prompt_tokens": 1, "completion_tokens": 1})))
    await agent.run(session, "привет")
    assert not FakeHarness.instances, "no runtime was started"
    assert any(e["payload"].get("text") == "по-старому" for e in store.events(session["id"]) if e["kind"] == "assistant")
    assert engine.status()["runtimes"] == 0


# 7 ---------------------------------------------------------------------------------
async def test_idle_runtimes_are_reaped_and_status_reports_them(bundle):
    config, store, agent, engine = bundle
    session = store.resolve(7, 0, 1)
    await agent.run(session, "раз")
    assert engine.status()["runtimes"] == 1 and engine.status()["sessions"][session["id"]]["prompts"] == 1
    engine.runtimes[session["id"]].last_used = time.time() - 3600
    other = store.resolve(8, 0, 1)
    await agent.run(other, "два")
    assert session["id"] not in engine.runtimes and other["id"] in engine.runtimes
    assert FakeHarness.instances[0].closed
    await engine.close()
    assert engine.runtimes == {} and all(h.closed for h in FakeHarness.instances)
