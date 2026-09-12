"""The server restarts itself on new code only after the turn that asked for it is over."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.lifecycle import RESTART_EXIT_CODE, VERIFY_TEXT, Lifecycle
from connector.selfheal import RESTART_FLAG
from connector.store import Store


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"))
    store = Store(config.root / "lifecycle.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    exits = []
    lifecycle = Lifecycle(agent, store, config, exit=exits.append)
    agent.extensions.append(lifecycle)
    agent.after_turn = lifecycle.after_turn
    agent.restart_pending = lifecycle.pending
    yield config, store, agent, lifecycle, exits
    store.db.close()


async def test_the_tool_refuses_without_a_supervisor(bundle, monkeypatch):
    _, store, agent, lifecycle, exits = bundle
    monkeypatch.delenv("AIGENT_SUPERVISED", raising=False)
    session = store.resolve(1, 0, 1)

    result = await agent.execute(session, "restart_server", {"reason": "new tool"})

    assert "supervisor" in result["error"] and not store.get_state(RESTART_FLAG, "")
    assert lifecycle.after_turn(session["id"]) is False and exits == []


async def test_the_restart_waits_for_the_turn_and_queues_the_verification(bundle, monkeypatch):
    _, store, agent, lifecycle, exits = bundle
    monkeypatch.setenv("AIGENT_SUPERVISED", "1")
    session = store.resolve(2, 0, 1)
    sid = session["id"]

    result = await agent.execute(session, "restart_server", {"reason": "checkpoint loop shipped"})

    assert result["scheduled"] is True and "after this turn ends" in result["note"]
    request = json.loads(store.get_state(RESTART_FLAG, ""))
    assert request["sid"] == sid and request["reason"] == "checkpoint loop shipped"
    assert exits == [], "the tool itself never exits: the turn that called it is still running"

    class Running:
        def done(self):
            return False

    agent.jobs[sid] = Running()
    assert lifecycle.after_turn(sid) is False and exits == [], "never while a turn runs"
    agent.jobs.pop(sid)

    assert lifecycle.after_turn(sid) is True
    assert exits == [RESTART_EXIT_CODE]
    assert store.get_state(RESTART_FLAG, "") == "", "the request is consumed exactly once"
    queued = store.queued(sid)
    assert len(queued) == 1 and queued[0]["payload"] == VERIFY_TEXT, "the next boot starts with a verification turn"
    notices = [e["payload"] for e in store.events(sid) if e["kind"] == "notice"]
    assert [n.get("restart") for n in notices if n.get("restart")] == ["scheduled", "deferred", "now"], \
        "scheduled by the tool, deferred once while a turn ran, then performed"


async def test_server_status_reports_the_supervisor_without_secrets(bundle, monkeypatch):
    config, store, agent, lifecycle, _ = bundle
    monkeypatch.setenv("AIGENT_SUPERVISED", "1")
    session = store.resolve(5, 0, 1)
    (config.root / "supervisor.json").write_text(json.dumps({
        "state": "starting", "revision": "abc", "restarts": 4, "failures": 0, "good_revision": "abb",
        "secret_looking_field": "must not leak"}), encoding="utf-8")

    status = await agent.execute(session, "server_status", {})

    assert status["supervised"] is True and status["restart_pending"] is False and status["uptime_seconds"] >= 0
    assert status["supervisor"] == {"state": "starting", "revision": "abc", "restarts": 4, "failures": 0,
                                    "good_revision": "abb"}
    assert "secret_looking_field" not in json.dumps(status) and status["version"]
    await agent.execute(session, "restart_server", {"reason": "x"})
    assert (await agent.execute(session, "server_status", {}))["restart_pending"] is True


async def test_the_self_heal_flag_is_honoured_for_the_finishing_session(bundle, monkeypatch):
    _, store, agent, lifecycle, exits = bundle
    monkeypatch.setenv("AIGENT_SUPERVISED", "1")
    session = store.resolve(3, 0, 1)
    store.set_state(RESTART_FLAG, "1789000000.0")  # what selfheal.restart_client writes

    assert lifecycle.after_turn(session["id"]) is True and exits == [RESTART_EXIT_CODE]
    assert store.queued(session["id"])[0]["payload"] == VERIFY_TEXT


async def test_a_finished_turn_triggers_the_hook_after_draining(bundle, monkeypatch):
    _, store, agent, lifecycle, exits = bundle
    monkeypatch.setenv("AIGENT_SUPERVISED", "1")
    session = store.resolve(4, 0, 1)
    sid = session["id"]
    store.set_state(RESTART_FLAG, json.dumps({"sid": sid, "reason": "x", "requested": 1}))
    agent.run = AsyncMock()

    agent.start(session, "поправь и перезапусти")
    await asyncio.sleep(0.05)  # the turn (a mock) ends and its done-callback runs the hook

    assert exits == [RESTART_EXIT_CODE] and not agent.busy(sid)


def test_the_app_offers_the_tool_and_installs_the_hook(tmp_path):
    app = create_app(tmp_path, polling=False)
    app.state.config.values.update(admin_password=password_hash("admin-password-1"),
                                   chat_password=password_hash("chat-pass"))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        sid = client.post("/api/sessions", json={"title": "self-update"}).json()["id"]
        agent = app.state.agent
        names = {t["function"]["name"] for ext in agent.extensions for t in ext.tools(agent.store.session(sid))}
        assert "restart_server" in names and "spawn_subagents" in names
        assert agent.after_turn == app.state.lifecycle.after_turn


async def test_a_pending_restart_owns_the_next_turn_and_waits_for_other_sessions(bundle, monkeypatch):
    """The dev chat's auto-continuation ran against the old code while the restart waited."""
    _, store, agent, lifecycle, exits = bundle
    monkeypatch.setenv("AIGENT_SUPERVISED", "1")
    session = store.resolve(6, 0, 1)
    sid = session["id"]
    agent.save_goal(sid, goal="cleanup shipped", status="active")
    agent._delivered[sid] = True
    await agent.execute(session, "restart_server", {"reason": "cleanup"})

    assert agent.plan_continuation(session) is False, "no continuation against the old code"
    notices = [e["payload"] for e in store.events(sid) if e["kind"] == "notice"]
    assert notices[-1].get("restart") == "pending" and store.queued(sid) == []

    class Running:
        def done(self):
            return False

    other = store.resolve(7, 0, 1)["id"]
    agent.jobs[other] = Running()
    assert lifecycle.after_turn(sid) is False and exits == []
    deferred = [e["payload"] for e in store.events(sid) if e["kind"] == "notice" and e["payload"].get("restart") == "deferred"]
    assert len(deferred) == 1, "the owner is told once why nothing restarted yet"
    assert lifecycle.after_turn(sid) is False and exits == []
    assert len([e for e in store.events(sid) if e["kind"] == "notice" and e["payload"].get("restart") == "deferred"]) == 1

    agent.jobs.pop(other)
    assert lifecycle.after_turn(other) is True and exits == [RESTART_EXIT_CODE]
    assert store.queued(sid)[0]["payload"] == VERIFY_TEXT, "the verification goes to the session that asked"

