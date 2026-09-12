"""A restart must not lie about a running turn, and the meter must not invent context numbers.

A killed process leaves a turn marker in the database and a session still saying "running". On the
next boot that session stops claiming to be running, the tool call the crash orphaned is closed,
and the lost turn either continues by itself (auto-continue plus an open goal) or waits as an
explicit offer that the interface can accept exactly once.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.store import Store

ADMIN = "admin-password-1"


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash(ADMIN), chat_password=password_hash("chat-pass"))
    store = Store(config.root / "restart.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    yield config, store, agent
    store.db.close()


def crash(agent, store, sid, prompt="собери отчёт", auto=False, status="running"):
    """Reproduce exactly what a process killed mid-turn leaves in the database."""
    agent.mark_turn(sid, {"started": 1000.0, "auto": auto, "prompt": prompt})
    store.execute("UPDATE sessions SET status=? WHERE id=?", (status, sid))
    store.message(sid, {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})


def notices(store, sid):
    return [event["payload"]["text"] for event in store.events(sid) if event["kind"] == "notice"]


async def wait_free(agent, sid):
    for _ in range(300):
        if not agent.busy(sid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the turn never finished")


# 1 -----------------------------------------------------------------------------------
async def test_a_live_turn_leaves_a_marker_and_a_stopped_one_clears_it(bundle):
    config, store, agent = bundle
    session = store.resolve(51, 0, 1)
    sid = session["id"]
    started = asyncio.Event()

    async def runner(session, content):
        started.set()
        await asyncio.Event().wait()  # like a real turn: it runs until it is stopped or killed

    agent.run = runner
    agent.start(session, "построй график")
    await asyncio.wait_for(started.wait(), 5)

    marker = agent.interrupted_turn(sid)
    assert marker["prompt"] == "построй график" and marker["started"] and marker["auto"] is False
    assert store.session(sid)["status"] == "running"

    agent.stop(sid)
    await wait_free(agent, sid)
    assert agent.interrupted_turn(sid) == {}, "a turn that ended in this process is not resumable"


# 2 -----------------------------------------------------------------------------------
def test_a_restart_stops_the_lie_and_offers_the_lost_turn(bundle):
    config, store, agent = bundle
    sid = store.resolve(52, 0, 1)["id"]
    crash(agent, store, sid)

    result = agent.reconcile_restart()

    assert result == {"resumed": [], "interrupted": [sid]}
    assert store.session(sid)["status"] == "interrupted", "a dead process cannot still be running"
    assert agent.interrupted_turn(sid) == {}
    assert agent.pending_resume(sid) == {"prompt": "собери отчёт", "started": 1000.0, "auto": False}
    assert any("Продолжить" in text for text in notices(store, sid))
    assert not store.queued(sid), "an offer waits for the owner instead of spending tokens"
    # The tool call the crash orphaned is closed, so the next request is a valid conversation again.
    assert [m["tool_call_id"] for m in store.history(sid) if m.get("role") == "tool"] == ["call-1"]


# 3 -----------------------------------------------------------------------------------
def test_an_open_goal_with_auto_continue_restarts_the_turn_itself(bundle):
    config, store, agent = bundle
    sid = store.resolve(53, 0, 1)["id"]
    store.update_session(sid, auto_continue=1)
    store.set_state("goal:" + sid, json.dumps({"goal": "собрать видео с Луны", "kind": "generate",
                                               "status": "active", "source": "agent"}))
    crash(agent, store, sid, prompt="сделай ролик", auto=True)

    result = agent.reconcile_restart()

    assert result == {"resumed": [sid], "interrupted": []}
    queued = store.queued(sid)
    assert len(queued) == 1 and "собрать видео с Луны" in queued[0]["payload"]
    assert sid in agent.auto_pending, "a continuation is automatic, so the continue guard applies"
    assert agent.pending_resume(sid) is None
    assert any("продолжаю цель автоматически" in text for text in notices(store, sid))


def test_auto_continue_off_never_spends_tokens_without_the_owner(bundle):
    config, store, agent = bundle
    sid = store.resolve(54, 0, 1)["id"]
    store.update_session(sid, auto_continue=0)
    store.set_state("goal:" + sid, json.dumps({"goal": "собрать видео с Луны", "kind": "generate",
                                               "status": "active"}))
    crash(agent, store, sid, auto=True)

    result = agent.reconcile_restart()

    assert result == {"resumed": [], "interrupted": [sid]}
    assert not store.queued(sid) and agent.pending_resume(sid)


# 4 -----------------------------------------------------------------------------------
def test_a_restart_leaves_untouched_sessions_alone(bundle):
    config, store, agent = bundle
    sid = store.resolve(55, 0, 1)["id"]
    store.message(sid, {"role": "user", "content": "привет"})

    assert agent.reconcile_restart() == {"resumed": [], "interrupted": []}
    assert store.session(sid)["status"] == "idle"
    assert agent.pending_resume(sid) is None and not notices(store, sid)


# 5 -----------------------------------------------------------------------------------
async def test_the_owner_can_accept_the_offer_exactly_once(bundle):
    config, store, agent = bundle
    sid = store.resolve(56, 0, 1)["id"]
    crash(agent, store, sid, prompt="залей отчёт")
    agent.reconcile_restart()
    seen = []

    async def runner(session, content):
        seen.append(content)

    agent.run = runner
    result = agent.accept_resume(store.session(sid))

    assert result["accepted"] is True and result["queued"] is False
    await wait_free(agent, sid)
    assert len(seen) == 1 and "Прерванный ход" in seen[0] and "залей отчёт" in seen[0]
    assert agent.pending_resume(sid) is None, "the offer is consumed by the acceptance"
    assert agent.interrupted_turn(sid) == {}


# 6 -----------------------------------------------------------------------------------
def test_the_offer_reaches_the_interface_and_is_accepted_once(tmp_path):
    """The whole path: a session left running in the database, a boot, the interface, one click."""
    app = create_app(tmp_path, polling=False)
    app.state.config.values.update(admin_password=password_hash(ADMIN), chat_password=password_hash("chat-pass"))
    store, agent = app.state.store, app.state.agent
    sid = store.resolve(57, 0, 1)["id"]
    crash(agent, store, sid, prompt="сверстай страницу")

    with TestClient(app) as client:  # entering the context manager is the restart
        client.post("/api/login", json={"password": ADMIN})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        assert client.get("/api/status").json()["restarted"] == {"resumed": [], "interrupted": [sid]}
        listed = [s for s in client.get("/api/sessions").json() if s["id"] == sid][0]
        assert listed["status"] == "interrupted"
        assert listed["resume"]["prompt"] == "сверстай страницу", "the interface sees what was lost"

        async def runner(session, content):
            agent.store.event(sid, "notice", {"text": "ход принят"})

        agent.run = runner
        first = client.post(f"/api/sessions/{sid}/resume")
        assert first.status_code == 200 and first.json()["resumed"] is True
        second = client.post(f"/api/sessions/{sid}/resume")
        assert second.status_code == 409, "one offer means one turn, never a doubled reply"
        assert agent.pending_resume(sid) is None
        assert any("возобновлён" in text for text in notices(store, sid))


def test_nothing_to_resume_is_refused_instead_of_faked(tmp_path):
    app = create_app(tmp_path, polling=False)
    app.state.config.values.update(admin_password=password_hash(ADMIN), chat_password=password_hash("chat-pass"))
    sid = app.state.store.resolve(58, 0, 1)["id"]

    with TestClient(app) as client:
        client.post("/api/login", json={"password": ADMIN})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        assert client.get("/api/status").json()["restarted"] == {"resumed": [], "interrupted": []}
        response = client.post(f"/api/sessions/{sid}/resume")
        assert response.status_code == 409 and "Nothing to resume" in response.json()["detail"]
        assert client.post("/api/sessions/does-not-exist/resume").status_code == 404
