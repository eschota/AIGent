"""Tests for the code-subagent system: parallel cached generation and honest scoring.

A fake DeepSeek transport (an httpx-like client exposing .stream()) stands in for the
provider, so nothing here touches the network. It records the exact request payload of
every subagent, which lets us prove the shared prefix is byte-identical (so the prompt
cache applies) and that concurrency is bounded.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.store import Store
from connector.subagents import PALETTE, SubAgents


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"),
                         chat_password=password_hash("chat-pass"), deepseek_key="test-key")
    store = Store(config.root / "subagents.db")
    agent = Agent(config, store, None, None)
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


def sse(content, hit=80, miss=20):
    return [
        "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}),
        "data: " + json.dumps({"usage": {"prompt_tokens": hit + miss, "completion_tokens": 5,
                                         "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}}),
        "data: [DONE]",
    ]


class FakeStream:
    status_code = 200

    def __init__(self, client, payload):
        self.client, self.payload = client, payload

    async def __aenter__(self):
        c = self.client
        c.live += 1
        c.peak = max(c.peak, c.live)
        if c.delay:
            await asyncio.sleep(c.delay)
        return self

    async def __aexit__(self, *args):
        self.client.live -= 1
        return False

    async def aiter_lines(self):
        content = self.client.reply(self.payload)
        for line in sse(content, self.client.hit, self.client.miss):
            yield line


class FakeDeepSeek:
    """Records each subagent request and replies with a fixed structured proposal."""

    def __init__(self, reply=None, hit=80, miss=20, delay=0.0):
        self._reply = reply or (lambda payload: json.dumps(
            {"path": "gen/piece.py", "content": "value = 1\n", "summary": "ok"}))
        self.hit, self.miss, self.delay = hit, miss, delay
        self.payloads, self.live, self.peak = [], 0, 0

    def reply(self, payload):
        return self._reply(payload) if callable(self._reply) else self._reply

    def stream(self, method, url, **kwargs):
        self.payloads.append(kwargs.get("json"))
        return FakeStream(self, kwargs.get("json"))


# 1 ---------------------------------------------------------------------------------
async def test_spawn_runs_tasks_in_parallel_and_returns_scored_proposals(bundle):
    _, store, agent = bundle
    session = store.resolve(1, 0, 1)
    fake = FakeDeepSeek()
    subs = SubAgents(agent, fake)

    result = await subs.spawn(session, [
        {"goal": "write add()", "files": ["gen/a.py"]},
        {"goal": "write sub()", "files": ["gen/b.py"]},
        {"goal": "write mul()", "files": ["gen/c.py"]},
    ], shared_context="Project: pure-python math helpers.")

    assert result["spawned"] == 3 and len(fake.payloads) == 3
    for item in result["results"]:
        assert item["proposal"] is True and item["content"] == "value = 1\n"
        assert item["tokens"] == 105 and item["cost_usd"] is not None
        assert item["cache_hit"] == 80 and item["cache_miss"] == 20
        assert 0.0 <= item["score"] <= 1.0 and item["context_tokens"] > 0
    assert result["totals"]["total_tokens"] == 315
    assert result["totals"]["avg_score"] is not None


# 2 ---------------------------------------------------------------------------------
async def test_the_shared_prefix_is_byte_identical_so_the_cache_applies(bundle):
    _, store, agent = bundle
    session = store.resolve(2, 0, 1)
    fake = FakeDeepSeek()
    subs = SubAgents(agent, fake)

    await subs.spawn(session, [{"goal": "task one"}, {"goal": "task two"}, {"goal": "task three"}],
                     shared_context="Common guidance shared by every subagent.")

    prefixes = [payload["messages"][0] for payload in fake.payloads]
    assert all(p == prefixes[0] for p in prefixes), "every subagent must send the identical cached prefix"
    assert "Common guidance shared by every subagent." in prefixes[0]["content"]
    # Only the small task tail differs between subagents.
    tails = [payload["messages"][1]["content"] for payload in fake.payloads]
    assert len(set(tails)) == 3 and all("task" in tail for tail in tails)


# 3 ---------------------------------------------------------------------------------
async def test_concurrency_is_capped(bundle):
    config, store, agent = bundle
    session = store.resolve(3, 0, 1)
    config.values["subagent_concurrency"] = 2
    fake = FakeDeepSeek(delay=0.05)
    subs = SubAgents(agent, fake)

    await subs.spawn(session, [{"goal": f"piece {i}"} for i in range(6)])

    assert fake.peak <= 2, "no more than the configured number of subagents run at once"
    # The configured value is clamped to the hard cap of eight.
    config.values["subagent_concurrency"] = 20
    assert subs.concurrency() == 8


# 4 ---------------------------------------------------------------------------------
async def test_scoring_updates_after_success_and_failure_and_orders_dispatch(bundle):
    _, store, agent = bundle
    session = store.resolve(4, 0, 1)
    sid = session["id"]

    # A successful python piece raises the rolling score for its kind above the neutral prior.
    good = FakeDeepSeek()
    await SubAgents(agent, good).spawn(session, [{"goal": "ok piece", "files": ["gen/ok.py"]}])
    rolling = float(store.get_state(f"subagent:score:{sid}:py"))
    assert rolling > 0.5

    # An unparseable reply fails and scores zero, dragging the rolling average back down.
    bad = FakeDeepSeek(reply="this is not json")
    failed = await SubAgents(agent, bad).spawn(session, [{"goal": "broken", "files": ["gen/bad.py"]}])
    assert failed["results"][0]["status"] == "failed" and failed["results"][0]["score"] == 0.0
    assert failed["results"][0].get("error")
    assert float(store.get_state(f"subagent:score:{sid}:py")) < rolling

    # Explicit priority scores order dispatch: the higher one runs first.
    ordered = await SubAgents(agent, FakeDeepSeek()).spawn(session, [
        {"goal": "low priority", "score": 0.1},
        {"goal": "high priority", "score": 0.9},
    ])
    assert ordered["results"][0]["goal"] == "high priority"


# 5 ---------------------------------------------------------------------------------
async def test_snapshot_reflects_live_and_totals(bundle):
    _, store, agent = bundle
    session = store.resolve(5, 0, 1)
    sid = session["id"]
    subs = SubAgents(agent, FakeDeepSeek())
    await subs.spawn(session, [{"goal": "one"}, {"goal": "two"}])

    snap = subs.snapshot(sid)
    assert snap["totals"]["total"] == 2 and snap["totals"]["count"] == 0  # finished after spawn returns
    assert snap["totals"]["total_tokens"] == 210
    emojis = {rec["emoji"] for rec in snap["subagents"]}
    assert len(emojis) == 2 and emojis <= {e for e, _ in PALETTE}
    assert all(rec["seconds"] >= 0 for rec in snap["subagents"])

    events = [e for e in store.events(sid) if e["kind"] == "subagent"]
    assert {e["payload"]["phase"] for e in events} == {"spawn", "update", "done"}


# 6 ---------------------------------------------------------------------------------
async def test_cancel_stops_inflight_subagents_without_writing(bundle):
    _, store, agent = bundle
    session = store.resolve(6, 0, 1)
    sid = session["id"]
    subs = SubAgents(agent, FakeDeepSeek(delay=10))

    job = asyncio.create_task(subs.spawn(session, [{"goal": "slow one"}, {"goal": "slow two"}]))
    assert await settle(lambda: subs.running(sid))
    stopped = subs.cancel(sid)
    result = await job

    assert stopped["cancelled"] >= 1
    assert all(item["status"] == "cancelled" for item in result["results"])
    assert not subs.running(sid)
    assert not (agent.workspace(sid) / "gen").exists(), "a cancelled subagent writes nothing"


# 7 ---------------------------------------------------------------------------------
async def test_the_tool_is_hidden_and_refused_when_disabled(bundle):
    config, store, agent = bundle
    session = store.resolve(7, 0, 1)
    subs = SubAgents(agent, FakeDeepSeek())
    assert [t["function"]["name"] for t in subs.tools(session)] == ["spawn_subagents"]

    config.values["subagents_enabled"] = False
    assert subs.tools(session) == []
    assert (await subs.execute(session, "spawn_subagents", {"tasks": [{"goal": "x"}]}))["error"]


# 8 ---------------------------------------------------------------------------------
async def test_a_proposal_is_never_written_without_review(bundle):
    _, store, agent = bundle
    session = store.resolve(8, 0, 1)
    sid = session["id"]
    subs = SubAgents(agent, FakeDeepSeek())

    result = await subs.spawn(session, [{"goal": "propose a file", "files": ["gen/piece.py"]}])

    piece = result["results"][0]
    assert piece["proposal"] is True and piece["path"] == "gen/piece.py" and piece["content"]
    assert not agent.approvals, "generation must not open an approval by itself"
    assert not (agent.workspace(sid) / "gen" / "piece.py").exists(), "nothing reaches disk from a subagent"


# 9 ---------------------------------------------------------------------------------
def test_api_exposes_snapshot_and_cancel(web):
    app, client = web
    sid = client.post("/api/sessions", json={"title": "subs"}).json()["id"]
    # Populate live state directly so the snapshot endpoint has something to report without network.
    subs = app.state.subagents
    subs.state[sid] = {"sa_1": {"id": "sa_1", "emoji": "\U0001f7e6", "color": "#4f8cff",
                                "goal": "demo", "status": "running", "kind": "py", "context_tokens": 40,
                                "started": time.time(), "finished": 0.0, "tokens": 100, "cost_usd": 0.001,
                                "score": None, "path": None, "error": None}}

    snap = client.get(f"/api/sessions/{sid}/subagents").json()
    assert snap["totals"]["count"] == 1 and snap["totals"]["total_tokens"] == 100
    assert snap["subagents"][0]["emoji"] == "\U0001f7e6" and snap["subagents"][0]["seconds"] >= 0

    assert client.post(f"/api/sessions/{sid}/subagents/cancel").json()["cancelled"] == 0
    assert subs.state[sid]["sa_1"]["status"] == "cancelled"


async def settle(check, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.01)
    return False
