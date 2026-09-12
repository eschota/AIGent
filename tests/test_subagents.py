"""Tests for the coding workers: real subagents with tools, run in parallel, reviewed like the main agent.

A fake backend at the `complete()` level replays one script per task, so nothing here touches
the network. It records every request, which lets us prove the shared prefix is byte-identical
(so the prompt cache applies), that concurrency is bounded, and that a worker's write goes
through the same approval as the main agent's.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.store import Store
from connector.subagents import PALETTE, WORKER_TOOLS, SubAgents


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"),
                         chat_password=password_hash("chat-pass"), deepseek_key="test-key")
    store = Store(config.root / "subagents.db")
    agent = Agent(config, store, None, AsyncMock())  # approvals talk to Telegram; nothing real here
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


def call(name, arguments, index=0):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"w{index}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class FakeWorkers:
    """Replays one script per task goal, the way the streaming provider would answer."""

    def __init__(self, scripts=None, delay=0.0):
        self.scripts = {goal: list(script) for goal, script in (scripts or {}).items()}
        self.calls, self.live, self.peak, self.delay = [], 0, 0, delay

    @staticmethod
    def goal_of(messages):
        task = next(m["content"] for m in messages if m.get("role") == "user"
                    and str(m.get("content", "")).startswith("# Your task"))
        return task.splitlines()[1]

    async def complete(self, messages, tools, delta):
        self.calls.append((messages, tools))
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        finally:
            self.live -= 1
        script = self.scripts.get(self.goal_of(messages)) or []
        item = script.pop(0) if script else {"role": "assistant", "content": "Готово: " + self.goal_of(messages)}
        return item, {"prompt_tokens": 100, "completion_tokens": 5, "cache_hit_tokens": 80,
                      "cache_miss_tokens": 20, "cost_usd": 0.001}


def workers(agent, fake):
    subs = SubAgents(agent, None)
    subs._backend = lambda session: fake
    return subs


async def settle(check, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.01)
    return False


# 1 ---------------------------------------------------------------------------------
async def test_workers_run_the_tools_themselves_and_report(bundle):
    _, store, agent = bundle
    session = store.resolve(1, 0, 1)
    sid = session["id"]
    store.update_session(sid, auto_approve=1)
    (agent.workspace(sid) / "gen").mkdir()
    (agent.workspace(sid) / "gen" / "b.py").write_text("value = 2\n", encoding="utf-8")
    fake = FakeWorkers({
        "write add()": [call("write_file", {"path": "gen/a.py", "content": "def add(a, b):\n    return a + b\n"}),
                        {"role": "assistant", "content": "Записал gen/a.py: add(). Проверено чтением."}],
        "read b": [call("read_file", {"path": "gen/b.py"}),
                   {"role": "assistant", "content": "gen/b.py содержит value = 2; менять нечего."}],
    })
    subs = workers(agent, fake)

    result = await subs.spawn(session, [{"goal": "write add()", "files": ["gen/a.py"]},
                                        {"goal": "read b", "files": ["gen/b.py"]}],
                              shared_context="Project: pure-python helpers.")

    assert result["spawned"] == 2 and result["totals"]["files"] == ["gen/a.py"]
    by_goal = {item["goal"]: item for item in result["results"]}
    assert by_goal["write add()"]["status"] == "done" and by_goal["write add()"]["files"] == ["gen/a.py"]
    assert "add()" in by_goal["write add()"]["report"] and by_goal["write add()"]["steps"] == 2
    assert (agent.workspace(sid) / "gen" / "a.py").read_text(encoding="utf-8").startswith("def add")
    assert by_goal["read b"]["status"] == "done" and by_goal["read b"]["files"] == []
    assert all(item["tokens"] == 210 and item["cost_usd"] == 0.002 for item in result["results"])
    assert all(item["cache_hit"] == 160 and item["cache_miss"] == 40 for item in result["results"])
    assert result["totals"]["total_steps"] == 4
    # The tool ran in the session: its events and usage are the session's own.
    tools = [e["payload"]["name"] for e in store.events(sid) if e["kind"] == "approval"]
    assert "write_file gen/a.py" in tools, "the worker's write went through the session's approval path"


# 2 ---------------------------------------------------------------------------------
async def test_the_shared_prefix_is_byte_identical_so_the_cache_applies(bundle):
    _, store, agent = bundle
    session = store.resolve(2, 0, 1)
    fake = FakeWorkers()
    subs = workers(agent, fake)

    await subs.spawn(session, [{"goal": "task one"}, {"goal": "task two"}, {"goal": "task three"}],
                     shared_context="Common guidance shared by every worker.")

    prefixes = [messages[:3] for messages, _ in fake.calls]
    assert all(p == prefixes[0] for p in prefixes), "every worker must send the identical cached prefix"
    system, guidance, shared = prefixes[0]
    assert system["role"] == "system" and "coding worker" in system["content"]
    assert "Only these tools exist" in system["content"] and "spawn" in system["content"]
    assert guidance["role"] == "user" and "Selected workspace" in guidance["content"]
    assert "Common guidance shared by every worker." in shared["content"]
    # Only the task tail differs between workers.
    tails = [messages[3]["content"] for messages, _ in fake.calls]
    assert len(set(tails)) == 3 and all(tail.startswith("# Your task") for tail in tails)


# 3 ---------------------------------------------------------------------------------
async def test_concurrency_is_capped(bundle):
    config, store, agent = bundle
    session = store.resolve(3, 0, 1)
    config.values["subagent_concurrency"] = 2
    fake = FakeWorkers(delay=0.05)
    subs = workers(agent, fake)

    await subs.spawn(session, [{"goal": f"piece {i}"} for i in range(6)])

    assert fake.peak <= 2, "no more than the configured number of workers run at once"
    config.values["subagent_concurrency"] = 20
    assert subs.concurrency() == 8, "the configured value is clamped to the hard cap"


# 4 ---------------------------------------------------------------------------------
async def test_priority_orders_dispatch(bundle):
    config, store, agent = bundle
    session = store.resolve(4, 0, 1)
    config.values["subagent_concurrency"] = 1
    subs = workers(agent, FakeWorkers())

    result = await subs.spawn(session, [{"goal": "low priority", "score": 0.1},
                                        {"goal": "high priority", "score": 0.9}])

    first = subs.state[session["id"]][subs.dispatch_order[session["id"]][0]]
    assert first["goal"] == "high priority"
    assert {item["status"] for item in result["results"]} == {"done"}


# 5 ---------------------------------------------------------------------------------
async def test_snapshot_and_events_show_what_each_worker_is_doing(bundle):
    _, store, agent = bundle
    session = store.resolve(5, 0, 1)
    sid = session["id"]
    (agent.workspace(sid) / "x.txt").write_text("x", encoding="utf-8")
    subs = workers(agent, FakeWorkers({"one": [call("read_file", {"path": "x.txt"}),
                                               {"role": "assistant", "content": "x прочитан"}]}))
    await subs.spawn(session, [{"goal": "one"}, {"goal": "two"}])

    snap = subs.snapshot(sid)
    assert snap["totals"]["total"] == 2 and snap["totals"]["count"] == 0
    assert snap["totals"]["total_tokens"] == 315 and snap["totals"]["total_steps"] == 3
    emojis = {rec["emoji"] for rec in snap["subagents"]}
    assert len(emojis) == 2 and emojis <= {e for e, _ in PALETTE}
    one = next(rec for rec in snap["subagents"] if rec["goal"] == "one")
    assert one["log"] == ["read_file x.txt"] and one["report"] == "x прочитан" and one["activity"] is None
    events = [e["payload"] for e in store.events(sid) if e["kind"] == "subagent"]
    assert {e["phase"] for e in events} == {"spawn", "update", "done"}
    assert any(e["activity"] == "read_file" and e["detail"] == "x.txt" for e in events), "the UI sees the tool"


# 6 ---------------------------------------------------------------------------------
async def test_cancel_stops_inflight_workers_without_writing(bundle):
    _, store, agent = bundle
    session = store.resolve(6, 0, 1)
    sid = session["id"]
    subs = workers(agent, FakeWorkers(delay=10))

    job = asyncio.create_task(subs.spawn(session, [{"goal": "slow one"}, {"goal": "slow two"}]))
    assert await settle(lambda: subs.running(sid))
    stopped = subs.cancel(sid)
    result = await job

    assert stopped["cancelled"] >= 1
    assert all(item["status"] == "cancelled" for item in result["results"])
    assert not subs.running(sid)
    assert not (agent.workspace(sid) / "gen").exists(), "a cancelled worker writes nothing"


# 7 ---------------------------------------------------------------------------------
async def test_the_tool_is_hidden_and_refused_when_disabled(bundle):
    config, store, agent = bundle
    session = store.resolve(7, 0, 1)
    subs = workers(agent, FakeWorkers())
    assert [t["function"]["name"] for t in subs.tools(session)] == ["spawn_subagents"]
    assert "by default" in subs.tools(session)[0]["function"]["description"]

    config.values["subagents_enabled"] = False
    assert subs.tools(session) == []
    assert (await subs.execute(session, "spawn_subagents", {"tasks": [{"goal": "x"}]}))["error"]


# 8 ---------------------------------------------------------------------------------
async def test_a_workers_write_goes_through_the_owners_review(bundle):
    _, store, agent = bundle
    session = store.resolve(8, 0, 1)
    sid = session["id"]
    subs = workers(agent, FakeWorkers({"propose": [
        call("write_file", {"path": "gen/piece.py", "content": "value = 1\n"}),
        {"role": "assistant", "content": "Запись отклонена владельцем; файл не изменён."}]}))

    job = asyncio.create_task(subs.spawn(session, [{"goal": "propose", "files": ["gen/piece.py"]}]))
    assert await settle(lambda: bool(agent.approvals)), "the write waits for the owner like any other"
    aid, item = next(iter(agent.approvals.items()))
    assert item["name"] == "write_file gen/piece.py" and item["session"]["id"] == sid
    agent.decide(aid, False, admin=True)
    result = await job

    piece = result["results"][0]
    assert piece["status"] == "done" and piece["files"] == []
    assert not (agent.workspace(sid) / "gen" / "piece.py").exists(), "a denied write never reaches disk"
    assert any("denied by owner" in line for line in subs.state[sid][piece["id"]]["log"])


# 9 ---------------------------------------------------------------------------------
async def test_workers_cannot_reach_the_owner_or_spawn_workers(bundle):
    _, store, agent = bundle
    session = store.resolve(9, 0, 1)
    fake = FakeWorkers({"escalate": [
        call("set_goal", {"goal": "x", "kind": "code", "status": "active"}),
        call("spawn_subagents", {"tasks": [{"goal": "nested"}]}, 1),
        {"role": "assistant", "content": "Сделал что мог."}]})
    subs = workers(agent, fake)

    names = {t["function"]["name"] for t in subs.worker_tools(session)}
    assert names <= set(WORKER_TOOLS) and "set_goal" not in names and "spawn_subagents" not in names
    result = await subs.spawn(session, [{"goal": "escalate"}])

    assert result["results"][0]["status"] == "done"
    replies = [json.loads(m["content"]) for m in fake.calls[-1][0] if m.get("role") == "tool"]
    assert len(replies) == 2 and all("not available to a worker" in r["error"] for r in replies)
    assert agent.goal(session["id"]).get("goal", "") != "x", "a worker cannot rewrite the session's goal"


# 10 --------------------------------------------------------------------------------
async def test_a_spent_step_budget_ends_with_an_honest_summary(bundle):
    config, store, agent = bundle
    session = store.resolve(10, 0, 1)
    config.values["subagent_max_steps"] = 1
    fake = FakeWorkers({"endless": [call("list_files", {"path": "."}),
                                    {"role": "assistant", "content": "Успел только осмотреться."}]})
    subs = workers(agent, fake)

    result = await subs.spawn(session, [{"goal": "endless"}])

    item = result["results"][0]
    assert item["status"] == "exhausted" and item["report"] == "Успел только осмотреться."
    assert "Step budget of 1" in item["error"] and item["steps"] == 1
    assert fake.calls[-1][1] == [] and fake.calls[-1][0][-1]["content"].startswith("Step budget for this turn")


# 11 --------------------------------------------------------------------------------
async def test_a_worker_read_does_not_alias_the_main_agents_read_cache(bundle):
    _, store, agent = bundle
    session = store.resolve(11, 0, 1)
    sid = session["id"]
    (agent.workspace(sid) / "shared.txt").write_text("same bytes", encoding="utf-8")

    first = await agent.execute(session, "read_file", {"path": "shared.txt"})
    again = await agent.execute(session, "read_file", {"path": "shared.txt"})
    worker = await agent.execute(dict(session, conversation=f"{sid}/sa_1"), "read_file", {"path": "shared.txt"})

    assert first["text"] == "same bytes" and again.get("unchanged") is True
    assert worker["text"] == "same bytes", "a worker never sees an 'unchanged' that points at another thread"
    assert Agent.conversation(session) == sid and Agent.conversation(dict(session, conversation="c")) == "c"


# 12 --------------------------------------------------------------------------------
def test_api_exposes_snapshot_and_cancel(web):
    app, client = web
    sid = client.post("/api/sessions", json={"title": "subs"}).json()["id"]
    subs = app.state.subagents
    subs.state[sid] = {"sa_1": {"id": "sa_1", "emoji": "\U0001f7e6", "color": "#4f8cff", "goal": "demo",
                                "status": "running", "steps": 3, "activity": "read_file", "detail": "x.py",
                                "files": [], "report": "", "error": None, "started": time.time(),
                                "finished": 0.0, "tokens": 100, "cost_usd": 0.001, "cache_hit": None,
                                "cache_miss": None, "context_chars": 40, "log": ["read_file x.py"]}}

    snap = client.get(f"/api/sessions/{sid}/subagents").json()
    assert snap["totals"]["count"] == 1 and snap["totals"]["total_tokens"] == 100
    assert snap["subagents"][0]["activity"] == "read_file" and snap["subagents"][0]["seconds"] >= 0

    assert client.post(f"/api/sessions/{sid}/subagents/cancel").json()["cancelled"] == 0
    assert subs.state[sid]["sa_1"]["status"] == "cancelled"
