"""Acceptance tests for the self-heal flow: fix chats that repair the code and close themselves.

The fix chat inherits the source's provider and workspace and is seeded with a compact briefing
built from the error (never the whole history); its turn auto-starts; confirming a fix archives
the chat (recoverable, not deleted) and posts closing notes to both chats; a restart is gated and
never happens without an explicit request; auto_confirm is off by default.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.selfheal import SelfHeal
from connector.store import Store


class Busy:
    cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


class FakeBackend:
    """A stand-in DeepSeek: one toolless assistant turn, no provider calls."""

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools, delta):
        self.calls += 1
        return {"role": "assistant", "content": "Исправил и проверил: тесты passed."}, None


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"))
    config.values["max_auto_continues"] = 0  # a fix turn must not spin into auto-continues in tests
    store = Store(config.root / "selfheal.db")
    from unittest.mock import AsyncMock
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


def make_source(store, agent, tmp_path, chat=1):
    source = store.resolve(chat, 0, 1, "источник")
    store.update_session(source["id"], provider="deepseek", model="deepseek-flash", workspace=str(tmp_path))
    agent.save_goal(source["id"], goal="починить превью", status="active", source="user")
    store.message(source["id"], {"role": "user", "content": "СЕКРЕТНАЯ_ИСТОРИЯ которую нельзя копировать"})
    store.event(source["id"], "tool", {"name": "read_file", "arguments": {"path": "connector/app.py"}, "call_id": "c1"})
    store.event(source["id"], "error", {"text": "ZeroDivisionError: division by zero in preview()"})
    store.event(source["id"], "trace", {"text": "Traceback...\n  File preview.py line 10\nZeroDivisionError"})
    return store.session(source["id"])


# 1 ----------------------------------------------------------------------------------
async def test_heal_creates_a_fix_chat_that_inherits_and_is_seeded_without_full_history(bundle, tmp_path):
    config, store, agent = bundle
    agent.deepseek = FakeBackend()
    source = make_source(store, agent, tmp_path)

    selfheal = SelfHeal(agent, store, config)
    result = selfheal.create_fix_session(source["id"])
    fix_sid = result["fix_sid"]
    fix = store.session(fix_sid)

    assert fix["provider"] == "deepseek" and fix["model"] == "deepseek-flash"
    assert fix["workspace"] == str(agent.workspace(source["id"])), "the fix chat edits the same codebase"
    assert fix["title"].startswith("🔧 Фикс")
    link = store.rows("SELECT * FROM fix_sessions WHERE fix_sid=?", (fix_sid,))[0]
    assert link["source_sid"] == source["id"] and link["status"] == "open"

    # The turn starts on its own and the briefing is the first user message.
    assert await settle(lambda: any(m.get("role") == "user" for m in store.history(fix_sid)))
    history = store.history(fix_sid)
    first_user = next(m for m in history if m["role"] == "user")["content"]
    assert "ZeroDivisionError" in first_user, "the specific error is conveyed"
    assert "починить превью" in first_user, "the prior goal is conveyed"
    assert "read_file" in first_user, "the recent actions are conveyed"
    assert "СЕКРЕТНАЯ_ИСТОРИЯ" not in json.dumps(history, ensure_ascii=False), "the full history is NOT copied"

    assert await settle(lambda: not agent.busy(fix_sid))
    assert store.rows("SELECT status FROM fix_sessions WHERE fix_sid=?", (fix_sid,))[0]["status"] == "open"


# 2 ----------------------------------------------------------------------------------
async def test_error_ref_selects_a_specific_event_for_the_briefing(bundle, tmp_path):
    config, store, agent = bundle
    agent.submit = lambda session, content, queue=True: {"accepted": True}
    source = store.resolve(2, 0, 1, "src")
    store.update_session(source["id"], workspace=str(tmp_path))
    first = store.event(source["id"], "error", {"text": "OldError: stale failure"})
    store.event(source["id"], "error", {"text": "NewError: latest failure"})

    selfheal = SelfHeal(agent, store, config)
    # By default the newest error is used; error_ref pins a specific one.
    assert selfheal._latest_kind(source["id"], "error")["payload"]["text"] == "NewError: latest failure"
    result = selfheal.create_fix_session(source["id"], error_ref=first)
    link = store.rows("SELECT * FROM fix_sessions WHERE fix_sid=?", (result["fix_sid"],))[0]
    assert link["error_ref"] == first
    briefing = [e for e in store.events(result["fix_sid"]) if e["kind"] == "heal_context"]
    assert briefing and briefing[0]["payload"]["source_sid"] == source["id"]


# 3 ----------------------------------------------------------------------------------
async def test_confirm_archives_the_fix_chat_and_notes_both_sides(bundle, tmp_path):
    config, store, agent = bundle
    agent.submit = lambda session, content, queue=True: {"accepted": True}
    source = make_source(store, agent, tmp_path, chat=3)
    selfheal = SelfHeal(agent, store, config)
    fix_sid = selfheal.create_fix_session(source["id"])["fix_sid"]

    confirmed = selfheal.confirm_fix(fix_sid, True)

    assert confirmed == {"fix_sid": fix_sid, "status": "archived", "verified": True, "archived": True}
    fix = store.session(fix_sid)
    assert fix["archived"] == 1 and not fix["deleted"], "archived is recoverable, never deleted"
    assert store.rows("SELECT status FROM fix_sessions WHERE fix_sid=?", (fix_sid,))[0]["status"] == "archived"
    assert any("подтверждён" in e["payload"].get("text", "")
               for e in store.events(fix_sid) if e["kind"] == "notice"), "closing note in the fix chat"
    assert any("подтверждён" in e["payload"].get("text", "")
               for e in store.events(source["id"]) if e["kind"] == "notice"), "closing note in the source chat"
    # The archived fix chat is hidden from the active list but present in the archived one.
    assert fix_sid not in [s["id"] for s in store.sessions()]
    assert fix_sid in [s["id"] for s in store.sessions(archived=True)]


# 4 ----------------------------------------------------------------------------------
async def test_a_failed_verification_keeps_the_chat_open(bundle, tmp_path):
    config, store, agent = bundle
    agent.submit = lambda session, content, queue=True: {"accepted": True}
    source = make_source(store, agent, tmp_path, chat=4)
    selfheal = SelfHeal(agent, store, config)
    fix_sid = selfheal.create_fix_session(source["id"])["fix_sid"]

    result = selfheal.confirm_fix(fix_sid, False)

    assert result["archived"] is False and result["status"] == "open"
    assert not store.session(fix_sid)["archived"], "an unverified fix chat stays active"


# 5 ----------------------------------------------------------------------------------
async def test_restart_is_gated_and_never_fires_without_confirmation(bundle, tmp_path):
    config, store, agent = bundle
    agent.submit = lambda session, content, queue=True: {"accepted": True}
    source = make_source(store, agent, tmp_path, chat=5)
    selfheal = SelfHeal(agent, store, config)
    fix_sid = selfheal.create_fix_session(source["id"])["fix_sid"]

    # A confirm without restart must not request a restart.
    selfheal.confirm_fix(fix_sid, True)
    assert store.get_state("restart_requested", "") == "", "confirming a fix must not restart on its own"

    # A restart is refused while another session's turn is running.
    other = store.resolve(50, 0, 1, "busy")
    agent.jobs[other["id"]] = Busy()
    blocked = selfheal.restart_client()
    assert blocked["blocked"] is True and not blocked.get("queued"), "never restart mid-turn of another session"
    agent.jobs.pop(other["id"])

    # Idle: an explicit restart request is queued via the store flag (no real restart in tests).
    queued = selfheal.restart_client()
    assert queued["queued"] is True and queued["restarted"] is False
    assert store.get_state("restart_requested", "") != ""


# 6 ----------------------------------------------------------------------------------
async def test_auto_confirm_is_off_by_default_and_closes_the_loop_when_enabled(bundle, tmp_path):
    config, store, agent = bundle
    agent.submit = lambda session, content, queue=True: {"accepted": True}
    assert config.values.get("auto_confirm_fixes") in (None, False), "auto_confirm is OFF by default"

    # With it off, green checks do not archive the fix chat.
    source = make_source(store, agent, tmp_path, chat=6)
    selfheal = SelfHeal(agent, store, config)
    fix_off = selfheal.create_fix_session(source["id"])["fix_sid"]
    store.event(fix_off, "tool_result", {"name": "run_command", "result": {"exit_code": 0, "output": "208 passed"}})
    store.event(fix_off, "turn_completed", {"goal": {}})
    assert store.rows("SELECT status FROM fix_sessions WHERE fix_sid=?", (fix_off,))[0]["status"] == "open"

    # With it on, a green check followed by turn end auto-confirms and archives.
    config.values["auto_confirm_fixes"] = True
    fix_on = selfheal.create_fix_session(source["id"])["fix_sid"]
    store.event(fix_on, "tool_result",
                {"name": "run_command", "result": {"exit_code": 0, "output": "208 passed in 3s\nAll checks passed!"}})
    store.event(fix_on, "turn_completed", {"goal": {}})
    assert store.rows("SELECT status FROM fix_sessions WHERE fix_sid=?", (fix_on,))[0]["status"] == "archived"
    assert store.session(fix_on)["archived"] == 1
    # A failing check must not auto-confirm.
    config.values["auto_confirm_fixes"] = True
    fix_bad = selfheal.create_fix_session(source["id"])["fix_sid"]
    store.event(fix_bad, "tool_result", {"name": "run_command", "result": {"exit_code": 1, "output": "3 failed"}})
    store.event(fix_bad, "turn_completed", {"goal": {}})
    assert store.rows("SELECT status FROM fix_sessions WHERE fix_sid=?", (fix_bad,))[0]["status"] == "open"


# 7 ----------------------------------------------------------------------------------
def test_the_api_creates_lists_and_confirms_fix_sessions(web):
    app, client = web
    # Avoid a real background provider turn: the briefing submit is stubbed for the API tests.
    app.state.agent.submit = lambda session, content, queue=True: {"accepted": True, "queued": False, "position": 0}
    sid = client.post("/api/sessions", json={"title": "src"}).json()["id"]
    app.state.store.event(sid, "error", {"text": "BoomError: kaboom"})

    created = client.post(f"/api/sessions/{sid}/heal", json={})
    assert created.status_code == 200
    fix_sid = created.json()["fix_sid"]
    fix = app.state.store.session(fix_sid)
    assert fix["provider"] == app.state.store.session(sid)["provider"], "the fix chat inherits the provider"

    listed = client.get("/api/heal").json()
    assert any(f["fix_sid"] == fix_sid and f["source_sid"] == sid for f in listed)
    per_source = client.get(f"/api/sessions/{sid}/heal").json()
    assert [f["fix_sid"] for f in per_source] == [fix_sid]

    confirmed = client.post(f"/api/heal/{fix_sid}/confirm", json={"verified": True}).json()
    assert confirmed["archived"] is True
    assert app.state.store.session(fix_sid)["archived"] == 1

    # A restart endpoint exists and is gated behind the explicit call (no session busy here).
    restart = client.post(f"/api/heal/{fix_sid}/restart").json()
    assert restart["queued"] is True and restart["restarted"] is False
