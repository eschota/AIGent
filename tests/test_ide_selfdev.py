"""Ten acceptance tests for AIGent developing and extending its own IDE.

Each test covers one capability the workspace needs in order to keep building itself:
queued mid-turn messages, unattended auto-apply, clipboard attachments, project moves,
the Skill Manager index and provider reliability.
"""

import asyncio
import inspect
import io
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from connector import skill_manager
from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.providers import DeepSeek, ProviderError
from connector import shared_tools
from connector.shared_tools import SharedTools
from connector.skill_manager import SkillIndex
from connector.store import Store


class Busy:
    """A stand-in for a running turn: busy, cancellable, never finishing on its own."""

    cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"))
    store = Store(config.root / "selfdev.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    yield config, store, agent
    store.db.close()


@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path, polling=False)
    app.state.config.values.update(admin_password=password_hash("admin-password-1"),
                                   chat_password=password_hash("chat-pass"))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        yield app, client


async def settle(check, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.01)
    return False


def png_bytes(colour=(40, 90, 160)):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (24, 16), colour).save(buffer, format="PNG")
    return buffer.getvalue()


# 1 ----------------------------------------------------------------------------------
async def test_message_sent_mid_turn_is_queued_and_never_interrupts_the_agent(bundle):
    _, store, agent = bundle
    session = store.resolve(1, 0, 1)
    running = Busy()
    agent.jobs[session["id"]] = running

    result = agent.submit(session, "продолжай, но сначала посмотри тесты")

    assert result == {"accepted": True, "queued": True, "position": 1, "queue_id": result["queue_id"]}
    assert not running.cancelled, "a queued message must not cancel the active turn"
    assert [item["payload"] for item in store.queued(session["id"])] == ["продолжай, но сначала посмотри тесты"]
    assert store.events(session["id"])[-1]["kind"] == "queued"


# 2 ----------------------------------------------------------------------------------
async def test_queued_messages_run_in_order_once_the_turn_finishes(bundle):
    _, store, agent = bundle
    session = store.resolve(2, 0, 1)
    seen, gate = [], asyncio.Event()

    async def runner(session, content):
        seen.append(content)
        if len(seen) == 1:
            await gate.wait()

    agent.run = runner
    agent.start(session, "первый ход")
    await asyncio.sleep(0)
    assert agent.submit(session, "второй")["position"] == 1
    assert agent.submit(session, "третий")["position"] == 2
    assert seen == ["первый ход"], "queued work must wait for real output, not preempt it"

    gate.set()
    assert await settle(lambda: seen == ["первый ход", "второй", "третий"]), seen
    assert not store.queued(session["id"])
    assert not agent.busy(session["id"])


# 3 ----------------------------------------------------------------------------------
def test_queue_is_visible_and_cancellable_through_the_api(web):
    app, client = web
    sid = client.post("/api/sessions", json={"title": "queue"}).json()["id"]
    app.state.agent.jobs[sid] = Busy()

    first = client.post(f"/api/sessions/{sid}/messages", json={"text": "первое"}).json()
    second = client.post(f"/api/sessions/{sid}/messages", json={"text": "второе"}).json()
    assert (first["position"], second["position"]) == (1, 2)

    queued = client.get(f"/api/sessions/{sid}/queue").json()
    assert [item["text"] for item in queued] == ["первое", "второе"]
    assert client.delete(f"/api/sessions/{sid}/queue", params={"item": queued[0]["id"]}).json() == {"removed": 1}
    assert [item["text"] for item in client.get(f"/api/sessions/{sid}/queue").json()] == ["второе"]
    assert client.delete(f"/api/sessions/{sid}/queue").json() == {"removed": 1}
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "нет", "queue": False}).status_code == 400
    app.state.agent.jobs.pop(sid)


# 4 ----------------------------------------------------------------------------------
async def test_auto_apply_writes_reviewed_files_without_asking(bundle):
    _, store, agent = bundle
    session = store.resolve(4, 0, 1)
    store.update_session(session["id"], auto_approve=1)

    result = await agent.execute(session, "write_file", {"path": "ide/feature.py", "content": "value = 1\n"})

    assert result["written"] == "ide/feature.py"
    assert (agent.workspace(session["id"]) / "ide/feature.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not agent.approvals, "auto-apply must not leave a pending confirmation"
    decision = [e for e in store.events(session["id"]) if e["kind"] == "decision"][-1]
    assert decision["payload"] == {"id": decision["payload"]["id"], "accepted": True, "auto": True}


# 5 ----------------------------------------------------------------------------------
async def test_auto_apply_runs_administrator_terminal_commands(bundle):
    config, store, agent = bundle
    config.values["allow_commands"] = True
    session = store.resolve(5, 0, 1)
    store.update_session(session["id"], auto_approve=1)

    result = await agent.execute(session, "run_command",
                                 {"argv": [sys.executable, "-c", "print('auto-applied')"]})

    assert result["exit_code"] == 0
    assert "auto-applied" in result["output"]
    assert not agent.approvals


# 6 ----------------------------------------------------------------------------------
async def test_confirmation_is_still_required_when_auto_apply_is_off(bundle):
    _, store, agent = bundle
    session = store.resolve(6, 0, 1)
    assert not store.session(session["id"])["auto_approve"]

    task = asyncio.create_task(agent.execute(session, "write_file", {"path": "manual.txt", "content": "x"}))
    assert await settle(lambda: bool(agent.approvals)), "the agent must ask before writing"
    aid = next(iter(agent.approvals))
    agent.decide(aid, False, admin=True)

    assert await task == {"denied": True}
    assert not (agent.workspace(session["id"]) / "manual.txt").exists()


# 7 ----------------------------------------------------------------------------------
def test_pasted_clipboard_image_is_uploaded_and_sent_as_vision_input(web):
    app, client = web
    sid = client.post("/api/sessions", json={"title": "paste"}).json()["id"]
    upload = client.post(f"/api/sessions/{sid}/files",
                         files={"file": ("clipboard.png", png_bytes(), "image/png")},
                         data={"kind": "photo"})
    assert upload.status_code == 200
    path = upload.json()["path"]

    captured = []
    app.state.agent.submit = lambda session, content, queue=True: (captured.append(content),
                                                                  {"accepted": True, "queued": False, "position": 0})[1]
    reply = client.post(f"/api/sessions/{sid}/messages",
                        json={"text": "что на скриншоте?", "attachments": [path]})

    assert reply.status_code == 202 and reply.json()["queued"] is False
    parts = captured[0]
    assert parts[0] == {"type": "text", "text": "что на скриншоте?"}
    assert any(p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/png;base64,") for p in parts)


# 8 ----------------------------------------------------------------------------------
def test_session_moves_between_projects_and_is_protected_while_running(web, tmp_path):
    app, client = web
    folder = tmp_path / "real_agent_clone"
    folder.mkdir()
    project = client.post("/api/projects", json={"path": str(folder), "name": "real_agent"}).json()
    sid = client.post("/api/sessions", json={"title": "move me"}).json()["id"]
    assert client.get("/api/sessions").json()[0]["project_id"] is None

    moved = client.patch(f"/api/sessions/{sid}", json={"project_id": project["id"]}).json()
    assert moved["project_id"] == project["id"]
    assert Path(moved["workspace"]) == folder
    assert app.state.agent.workspace(sid) == folder.resolve()

    app.state.agent.jobs[sid] = Busy()
    assert client.patch(f"/api/sessions/{sid}", json={"project_id": ""}).status_code == 409
    assert client.patch(f"/api/sessions/{sid}", json={"title": "renamed"}).status_code == 200
    app.state.agent.jobs.pop(sid)

    detached = client.patch(f"/api/sessions/{sid}", json={"project_id": ""}).json()
    assert detached["project_id"] is None and detached["workspace"] is None


# 9 ----------------------------------------------------------------------------------
async def test_skill_manager_indexes_codex_and_claude_markdown_without_spending_tokens(bundle, tmp_path, monkeypatch):
    config, store, agent = bundle
    home = tmp_path / "home"
    skill = home / ".claude" / "skills" / "release-check"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: Release check\ndescription: Verify the desktop build before shipping.\n"
        "tags: [release, desktop]\n---\n\n# Release check\n\nRun the build script.\n", encoding="utf-8")
    prompts = home / ".codex" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "refactor.md").write_text("# Refactor prompt\n\nSplit long modules. #cleanup\n", encoding="utf-8")
    monkeypatch.setattr(skill_manager.Path, "home", lambda: home)

    index = SkillIndex(config, store, agent.workspace)
    state = index.scan()

    assert state["indexed"] == 2 and state["error"] is None
    titles = {item["title"] for item in index.search("")}
    assert titles == {"Release check", "Refactor prompt"}
    assert [item["title"] for item in index.search("release")] == ["Release check"]
    assert [item["title"] for item in index.search("", tags=["cleanup"])] == ["Refactor prompt"]
    assert {item["kind"] for item in index.search("")} == {"skill", "prompt"}
    assert "release" in {tag["tag"] for tag in index.tag_cloud()}

    status = index.status()
    assert status["analysis"] == "local" and status["free_model"] is None and status["tokens_spent"] == 0
    assert "httpx" not in inspect.getsource(skill_manager), "indexing must never call a paid provider"

    index.scan()
    assert index.status()["total"] == 2, "a rescan must not duplicate known skills"


# 10 ---------------------------------------------------------------------------------
async def test_skills_attach_into_any_chat_and_transfer_between_sessions(bundle, tmp_path, monkeypatch):
    config, store, agent = bundle
    home = tmp_path / "home"
    folder = home / ".claude" / "skills" / "deploy"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: Deploy\ndescription: Ship the build.\n---\n\nSteps.\n", encoding="utf-8")
    monkeypatch.setattr(skill_manager.Path, "home", lambda: home)
    index = SkillIndex(config, store, agent.workspace)
    index.scan()
    skill_id = index.search("deploy")[0]["id"]
    first, second = store.resolve(10, 0, 1), store.resolve(11, 0, 1)

    attached = index.attach(skill_id, first["id"])
    shared = index.attach(skill_id, second["id"])

    assert attached["path"] == shared["path"] == "skills/SKILL.md"
    for session in (first, second):
        copied = agent.workspace(session["id"]) / "skills" / "SKILL.md"
        assert "Ship the build." in copied.read_text(encoding="utf-8")
        assert [e["kind"] for e in store.events(session["id"])][-1] == "skill_attached"
    assert index.get(skill_id)["uses"] == 2
    assert "Deploy" in attached["reference"]


# Provider reliability underpins every test above: a crash here ends a self-development run.
async def test_deepseek_stream_survives_damaged_chunks_and_unnamed_tool_calls(bundle):
    config, _, _ = bundle
    lines = [
        "data: {broken json",
        'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"read_file","arguments":"{\\"path\\":\\"a\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{"content":"готово"}}]}',
        'data: {"usage":{"prompt_tokens":10,"completion_tokens":2,"prompt_cache_hit_tokens":8,"prompt_cache_miss_tokens":2}}',
        "data: [DONE]",
    ]

    class Response:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Client:
        def stream(self, *args, **kwargs):
            return Response()

    config.values["deepseek_key"] = "test-key"
    message, usage = await DeepSeek(config, Client()).complete([], [], AsyncMock())

    assert message["content"] == "готово"
    assert message["tool_calls"][0]["id"], "a call without an id must get one or the next request fails"
    assert message["tool_calls"][0]["function"]["name"] == "read_file"
    assert usage["cache_hit_tokens"] == 8


async def test_turn_retries_transient_provider_failures_and_repairs_history(bundle):
    _, store, agent = bundle
    session = store.resolve(12, 0, 1)
    sid = session["id"]
    store.message(sid, {"role": "tool", "tool_call_id": "orphan", "content": "{}"})
    store.message(sid, {"role": "user", "content": "hi"})
    assert [m["role"] for m in agent.sanitize(store.history(sid))] == ["user"]

    attempts = []

    class Flaky:
        async def complete(self, messages, tools, delta):
            attempts.append(messages)
            if len(attempts) < 3:
                raise ProviderError("timeout", retryable=True)
            return {"role": "assistant", "content": "ok"}, None

    agent.config.values["max_context_chars"] = 2000000
    message, _ = await agent.complete_with_retry(Flaky(), sid, [], AsyncMock(), attempts=3)
    assert message["content"] == "ok" and len(attempts) == 3
    assert any(e["kind"] == "notice" for e in store.events(sid))

    class Fatal:
        async def complete(self, messages, tools, delta):
            raise ProviderError("invalid key")

    with pytest.raises(ProviderError):
        await agent.complete_with_retry(Fatal(), sid, [], AsyncMock(), attempts=3)

# 11 ---------------------------------------------------------------------------------
class FarmResponse:
    """A minimal httpx-like response for the connected free farm."""

    is_redirect = False

    def __init__(self, payload=None, chunks=(), status_code=200):
        self.payload, self.chunks, self.status_code = payload, chunks, status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FarmClient:
    """The connected farm: text prompt renders a png, an uploaded frame renders an mp4."""

    def __init__(self, image, status="done", error=""):
        self.image, self.status, self.error, self.calls = image, status, error, []
        self.bodies = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        self.bodies.append(kwargs.get("json") or {})
        if url.endswith("/dev/api/scratch"):
            return FarmResponse({"url": "https://autorig.online/dev/api/scratch/frame.png"})
        if url.endswith("/renderfin/api-render"):
            video = "image_url" in (kwargs.get("json") or {})
            return FarmResponse({"task_id": "task-77",
                                 "output_url": "https://autorig.online/out/task-77." + ("mp4" if video else "png")})
        return FarmResponse([{"status": self.status, "error": self.error, "workflow": "gen_animation_by_url.json"}])

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url))
        return FarmResponse(chunks=[self.image])


async def test_shared_tools_run_in_any_chat_including_non_deepseek_providers(bundle, tmp_path, monkeypatch):
    config, store, agent = bundle
    home = tmp_path / "home"
    folder = home / ".claude" / "skills" / "review"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: Review\ndescription: Review a diff.\n---\n\nSteps.\n", encoding="utf-8")
    monkeypatch.setattr(skill_manager.Path, "home", lambda: home)
    index = SkillIndex(config, store, agent.workspace)
    index.scan()

    client = FarmClient(png_bytes())
    shared = SharedTools(agent, client, index)
    agent.extensions.append(shared)

    codex = store.resolve(20, 0, 1)
    store.update_session(codex["id"], provider="codex")
    codex = store.session(codex["id"])

    names = {item["name"] for item in shared.catalogue()}
    assert names == {"image", "video", "skill"}
    assert all(item["providers"] == "any" for item in shared.catalogue())

    attached = await shared.run(codex, "skill", {"query": "review"})
    assert attached["path"] == "skills/SKILL.md"
    assert (agent.workspace(codex["id"]) / "skills" / "SKILL.md").is_file()

    generated = await shared.run(codex, "image", {"prompt": "isolated red crate, flat background"}, wait=True)
    assert generated["path"] == "shared/task-77.png"
    assert generated["billing"] == "free-farm"
    assert (agent.workspace(codex["id"]) / "shared" / "task-77.png").read_bytes() == png_bytes()
    media = [e for e in store.events(codex["id"]) if e["kind"] == "media"][-1]
    assert media["payload"]["direction"] == "generated"
    assert "image_url" not in str(generated), "a Codex chat receives the file, not DeepSeek vision content"
    assert {call[1] for call in client.calls} >= {"https://autorig.online/renderfin/api-render"}


# 12 ---------------------------------------------------------------------------------
async def test_shared_image_runs_in_the_background_without_blocking_the_chat(bundle):
    _, store, agent = bundle
    session = store.resolve(21, 0, 1)
    shared = SharedTools(agent, FarmClient(png_bytes()), None)

    started = await shared.run(session, "image", {"prompt": "test"})
    assert started["started"] is True and started["billing"] == "free-farm"
    for extra in ("second", "third"):
        assert (await shared.run(session, "image", {"prompt": extra}))["started"] is True
    assert len(shared.active(session["id"])) == SharedTools.SLOTS == 3, "three farm slots run side by side"
    with pytest.raises(ValueError, match="слота"):
        await shared.run(session, "image", {"prompt": "fourth"})

    def results():
        return [e for e in store.events(session["id"]) if e["kind"] == "tool_result"]

    assert await settle(lambda: len(results()) >= 3, 10)
    assert results()[-1]["payload"]["result"]["path"] == "shared/task-77.png"
    assert shared.active(session["id"]) == [], "a finished task frees its slot"
    await shared.close()


# 13 ---------------------------------------------------------------------------------
async def test_farm_video_renders_from_a_frame_and_survives_a_restart(bundle):
    config, store, agent = bundle
    session = store.resolve(22, 0, 1)
    frame = agent.workspace(session["id"]) / "shot.png"
    frame.write_bytes(png_bytes())
    client = FarmClient(b"\x00\x00mp4-bytes")
    shared = SharedTools(agent, client)

    result = await shared.run(session, "video", {"path": "shot.png", "prompt": "camera push in"}, wait=True)

    assert result["path"] == "shared/task-77.mp4"
    assert result["source_frame"] == "shot.png"
    assert (agent.workspace(session["id"]) / "shared" / "task-77.mp4").read_bytes() == b"\x00\x00mp4-bytes"
    media = [e for e in store.events(session["id"]) if e["kind"] == "media"][-1]
    assert media["payload"]["kind"] == "video"
    assert any(url.endswith("/dev/api/scratch") for _, url in client.calls), "the frame is published before rendering"
    # the owner asked for a 16:8 frame, five seconds long, without passing either field
    rendered = [body for (_, url), body in zip(client.calls, client.bodies)
                if url.endswith("/renderfin/api-render")]
    assert rendered[-1]["frame_count"] == 121, "an omitted length is five seconds"
    assert (rendered[-1]["main_size_width"], rendered[-1]["main_size_height"]) == (512, 256)
    assert shared.video_size("512x256") == (512, 256)
    assert not shared.pending(), "a finished task is no longer pending"

    # A task still rendering when the server stops is picked up again after the restart.
    shared.remember(session["id"], {"kind": "video", "task_id": "task-77", "started": time.time(),
                                    "output_url": "https://autorig.online/out/task-77.mp4", "prompt": "x"})
    assert shared.resume() == ["task-77"]
    assert await settle(lambda: any(e["kind"] == "tool_result" for e in store.events(session["id"])), 10)
    await shared.close()


# 14 ---------------------------------------------------------------------------------
async def test_a_farm_rejection_is_retried_then_reported_verbatim(bundle, monkeypatch):
    _, store, agent = bundle
    monkeypatch.setattr(shared_tools, "RETRY_SECONDS", 0)
    session = store.resolve(23, 0, 1)
    frame = agent.workspace(session["id"]) / "shot.png"
    frame.write_bytes(png_bytes())
    client = FarmClient(b"", status="Error", error="real_output_artifact_missing")
    shared = SharedTools(agent, client)

    started = await shared.run(session, "video", {"path": "shot.png"})
    assert started["started"] is True

    assert await settle(lambda: any(e["kind"] == "error" for e in store.events(session["id"])), 10)
    submissions = [url for _, url in client.calls if url.endswith("/renderfin/api-render")]
    assert len(submissions) == shared_tools.ATTEMPTS, "a dropped artifact is resubmitted to another node"
    retries = [e for e in store.events(session["id"])
               if e["kind"] == "notice" and "отправляю задачу заново" in e["payload"].get("text", "")]
    assert len(retries) == shared_tools.ATTEMPTS - 1
    message = [e for e in store.events(session["id"]) if e["kind"] == "error"][-1]["payload"]["text"]
    assert "real_output_artifact_missing" in message and "gen_animation_by_url.json" in message
    assert not shared.pending(), "a rejected task must not be retried forever after a restart"
    await shared.close()


# 15 ---------------------------------------------------------------------------------
async def test_a_slow_command_returns_partial_output_instead_of_failing_the_turn(bundle):
    config, store, agent = bundle
    config.values["allow_commands"] = True
    session = store.resolve(24, 0, 1)
    store.update_session(session["id"], auto_approve=1)
    script = "import sys,time; print('stage one', flush=True); time.sleep(30)"

    result = await agent.execute(session, "run_command",
                                 {"argv": [sys.executable, "-u", "-c", script], "timeout_seconds": 5})

    assert result["timed_out"] is True and result["timeout_seconds"] == 5
    assert "stage one" in result["output"], "output produced before the timeout is preserved"
    assert "таймауту" in result["note"]


# 16 ---------------------------------------------------------------------------------
async def test_context_meter_reports_the_budget_that_triggers_trimming(bundle):
    config, store, agent = bundle
    session = store.resolve(25, 0, 1)
    sid = session["id"]
    config.values["max_context_chars"] = 10000
    empty = agent.context_size(sid)
    assert {k: empty[k] for k in ("chars", "limit", "percent", "messages", "images")} == {
        "chars": 2, "limit": 10000, "percent": 0.0, "messages": 0, "images": 0}
    assert empty["limit_tokens"] == round(10000 / 4), "the meter also reports an approximate token budget"

    store.message(sid, {"role": "user", "content": "x" * 2000})
    store.message(sid, {"role": "user", "content": [{"type": "text", "text": "y"},
                                                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 50000}}]})
    measured = agent.context_size(sid)

    assert measured["messages"] == 2 and measured["images"] == 1
    assert 20 < measured["percent"] < 30, "base64 transport must not be counted as context text"
    assert measured["chars"] == agent.text_size(store.history(sid))


# 17 ---------------------------------------------------------------------------------
async def test_an_open_goal_keeps_the_agent_working_without_a_new_user_message(bundle):
    config, store, agent = bundle
    config.values["max_auto_continues"] = 3
    session = store.resolve(30, 0, 1)
    sid = session["id"]
    seen = []

    async def runner(session, content):
        seen.append(content)
        if len(seen) == 1:
            await agent.execute(session, "set_goal",
                                {"goal": "собрать видео с Луны", "kind": "generate", "status": "active"})
        agent.plan_continuation(session)

    agent.run = runner
    agent.start(session, "сделай ролик")
    assert await settle(lambda: len(seen) >= 4, 5), seen

    assert seen[0] == "сделай ролик"
    assert all("Продолжай цель: «собрать видео с Луны»" in str(item) for item in seen[1:])
    assert "Автопродолжение 3/3" in str(seen[3])
    assert len(seen) == 4, "the cap stops an endless loop"
    assert any("Автопродолжение остановлено" in e["payload"].get("text", "")
               for e in store.events(sid) if e["kind"] == "notice")


# 18 ---------------------------------------------------------------------------------
async def test_a_finished_or_blocked_goal_stops_the_loop_immediately(bundle):
    _, store, agent = bundle
    session = store.resolve(31, 0, 1)

    await agent.execute(session, "set_goal", {"goal": "починить превью", "kind": "fix", "status": "active"})
    assert agent.plan_continuation(session) is True
    store.drop_queued(session["id"])
    agent.auto_pending.discard(session["id"])

    await agent.execute(session, "set_goal", {"goal": "починить превью", "kind": "fix", "status": "done"})
    assert agent.plan_continuation(session) is False, "a proven goal must not trigger another turn"

    await agent.execute(session, "set_goal", {"goal": "нужен эндпоинт фермы", "kind": "wait", "status": "blocked"})
    assert agent.plan_continuation(session) is False, "a blocked goal waits for the owner"
    goals = [e["payload"] for e in store.events(session["id"]) if e["kind"] == "goal"]
    assert [g["status"] for g in goals] == ["active", "done", "blocked"]
    assert goals[0]["kind"] == "fix"

    store.update_session(session["id"], auto_continue=0)
    await agent.execute(session, "set_goal", {"goal": "снова активна", "kind": "code", "status": "active"})
    assert agent.plan_continuation(session) is False, "the owner can switch the behaviour off per chat"


# 19 ---------------------------------------------------------------------------------
async def test_a_background_question_never_blocks_the_turn(bundle):
    _, store, agent = bundle
    session = store.resolve(32, 0, 1)

    result = await asyncio.wait_for(
        agent.execute(session, "ask_user_async",
                      {"question": "16:9 или 1:1?", "assumption": "делаю 16:9"}), 2)

    assert result["blocking"] is False and result["asked"] is True
    assert not agent.questions, "an async question must not create a blocking wait"
    asked = [e["payload"] for e in store.events(session["id"]) if e["kind"] == "background_question"][-1]
    assert asked["question"] == "16:9 или 1:1?" and asked["assumption"] == "делаю 16:9"


# 20 ---------------------------------------------------------------------------------
def test_media_is_served_for_viewing_and_a_damaged_file_fails_cleanly(web):
    app, client = web
    sid = client.post("/api/sessions", json={"title": "media"}).json()["id"]
    workspace = app.state.agent.workspace(sid)
    (workspace / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (workspace / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")
    good = client.post(f"/api/sessions/{sid}/files", files={"file": ("shot.png", png_bytes(), "image/png")}).json()["path"]

    image = client.get(f"/api/sessions/{sid}/media", params={"path": good})
    video = client.get(f"/api/sessions/{sid}/media", params={"path": "clip.mp4"})
    damaged = client.get(f"/api/sessions/{sid}/media", params={"path": "broken.png"})

    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert "attachment" not in image.headers.get("content-disposition", ""), "viewing must not force a download"
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    assert damaged.status_code == 415, "a corrupted file returns a clean refusal, not a server error"
    assert client.get(f"/api/sessions/{sid}/media", params={"path": "missing.png"}).status_code == 404


# 21 ---------------------------------------------------------------------------------
def test_several_pasted_files_travel_together_in_one_message(web):
    app, client = web
    sid = client.post("/api/sessions", json={"title": "multi"}).json()["id"]
    paths = [client.post(f"/api/sessions/{sid}/files",
                         files={"file": (f"shot-{i}.png", png_bytes((i * 40, 60, 120)), "image/png")},
                         data={"kind": "photo"}).json()["path"] for i in range(3)]
    assert len(set(paths)) == 3

    captured = []
    app.state.agent.submit = lambda session, content, queue=True: (captured.append(content),
                                                                  {"accepted": True, "queued": False, "position": 0})[1]
    reply = client.post(f"/api/sessions/{sid}/messages", json={"text": "сравни кадры", "attachments": paths})

    assert reply.status_code == 202
    parts = captured[0]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 3, "every pasted file reaches the model"
    assert parts[0]["text"] == "сравни кадры"


# 22 ---------------------------------------------------------------------------------
def test_a_broken_self_edit_is_rolled_back_to_the_last_working_revision(tmp_path):
    """The agent rewrites its own code; a revision that cannot start must not end the workspace."""
    from connector import supervisor

    root = tmp_path / "app"
    (root / "connector").mkdir(parents=True)
    (root / "connector" / "core.py").write_text("VALUE = 'good'\n", encoding="utf-8")
    (root / "run.py").write_text("print('serving')\n", encoding="utf-8")
    data = tmp_path / "state"
    data.mkdir()

    good_revision = supervisor.revision(root)
    keeper = supervisor.Supervisor(root, data, ["noop"])
    keeper.good = good_revision
    supervisor.snapshot(root, keeper.snapshots, good_revision)

    # A self-edit that will not import: the file now contains a syntax error.
    (root / "connector" / "core.py").write_text("VALUE = 'broken\n", encoding="utf-8")
    broken_revision = supervisor.revision(root)
    assert broken_revision != good_revision

    lives = iter([1, 1])  # two immediate crashes
    keeper.spawn = lambda: next(lives)
    restored = {}

    def stop_after_rollback(**fields):
        restored.update(fields)
        if fields.get("state") == "rolled_back":
            raise KeyboardInterrupt

    keeper.publish = stop_after_rollback
    keeper.log = lambda message: None
    with pytest.raises(KeyboardInterrupt):
        keeper.run()

    assert restored["restored"] == good_revision
    assert (root / "connector" / "core.py").read_text(encoding="utf-8") == "VALUE = 'good'\n"
    assert supervisor.revision(root) == good_revision, "the workspace is back on code that starts"


# 23 ---------------------------------------------------------------------------------
def test_a_revision_that_serves_becomes_the_rollback_target(tmp_path, monkeypatch):
    from connector import supervisor

    root = tmp_path / "app"
    (root / "connector").mkdir(parents=True)
    (root / "connector" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "run.py").write_text("print('serving')\n", encoding="utf-8")
    keeper = supervisor.Supervisor(root, tmp_path / "state", ["noop"])
    keeper.log = lambda message: None
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: None)

    clock = iter([0, supervisor.HEALTHY_SECONDS + 1, 0, 0])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))
    steps = iter([3, 0])  # first run serves a long time and dies, second exits cleanly
    keeper.spawn = lambda: next(steps)

    assert keeper.run() == 0
    assert keeper.good == supervisor.revision(root)
    assert (keeper.snapshots / keeper.good).is_dir(), "the working revision is kept for rollback"
    assert keeper.restarts == 1


# 24 ---------------------------------------------------------------------------------
async def test_background_questions_close_and_never_pile_up(bundle):
    config, store, agent = bundle
    session = store.resolve(40, 0, 1)
    sid = session["id"]

    for index in range(5):
        await agent.execute(session, "ask_user_async",
                            {"question": f"вопрос {index}?", "assumption": f"допущение {index}"})

    open_now = store.open_questions(sid)
    assert len(open_now) == store.OPEN_QUESTIONS, "older questions retire instead of accumulating"
    assert [row["question"] for row in open_now] == ["вопрос 2?", "вопрос 3?", "вопрос 4?"]

    # Answering closes exactly one and it stays closed when the page is reloaded.
    assert store.close_question(open_now[0]["id"], "answered", "да") == 1
    assert store.close_question(open_now[0]["id"], "answered", "да") == 0, "closing twice is a no-op"
    assert [row["id"] for row in store.open_questions(sid)] == [open_now[1]["id"], open_now[2]["id"]]

    # A message from the owner supersedes whatever is still hanging.
    agent.submit(session, "продолжай, это уже неважно")
    assert store.open_questions(sid) == []
    closed = [e["payload"]["id"] for e in store.events(sid) if e["kind"] == "background_answered"]
    assert set(closed) == {open_now[1]["id"], open_now[2]["id"]}
    agent.stop(sid)


# 25 ---------------------------------------------------------------------------------
def test_the_api_lists_only_open_questions_and_answers_them_once(web):
    app, client = web
    sid = client.post("/api/sessions", json={"title": "questions"}).json()["id"]
    store = app.state.store
    store.ask_async(sid, "q1", "16:9 или 1:1?", "делаю 16:9")
    store.ask_async(sid, "q2", "звук нужен?", "без звука")

    listed = client.get(f"/api/sessions/{sid}/async-questions").json()
    assert [item["id"] for item in listed] == ["q1", "q2"]
    assert "status" not in listed[0]

    app.state.agent.submit = lambda session, content, queue=True: {"accepted": True, "queued": True, "position": 1}
    answered = client.post(f"/api/sessions/{sid}/async-questions/q1", json={"answer": "16:9"}).json()
    assert answered["closed"] is True and answered["queued"] is True
    assert client.post(f"/api/sessions/{sid}/async-questions/q1", json={"answer": "16:9"}).json()["closed"] is False

    dismissed = client.post(f"/api/sessions/{sid}/async-questions/q2", json={"answer": ""}).json()
    assert dismissed == {"closed": True, "queued": False}
    assert client.get(f"/api/sessions/{sid}/async-questions").json() == []
