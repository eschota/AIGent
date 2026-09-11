"""Project map: free inventory and estimate, and the paid, pausable DeepSeek scan.

Nothing here talks to a real provider: every paid path runs through an httpx.MockTransport that
returns the JSON the scan asks for, so usage accounting, pausing, budgets and calibration are
verified exactly as the widget shows them.
"""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from connector import project_map as pm
from connector.agent import Agent
from connector.app import create_app
from connector.config import Config, password_hash
from connector.project_map import ProjectMap, token_estimate
from connector.providers import DeepSeek
from connector.store import Store

CYRILLIC = "Заметка о проекте. " * 40


def build_project(root: Path):
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "node_modules").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text("VALUE = 1\n\n\ndef run():\n    return VALUE\n", encoding="utf-8")
    (root / "pkg" / "api.py").write_text("from pkg.core import run\nimport json\n\n\ndef go():\n    return json.dumps(run())\n", encoding="utf-8")
    (root / "web" / "app.js").write_text("import {draw} from './view.js';\nconst x = require('left-pad');\ndraw(x);\n", encoding="utf-8")
    (root / "web" / "view.js").write_text("export function draw(v) { return v; }\n", encoding="utf-8")
    (root / "notes.md").write_text(CYRILLIC, encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (root / "node_modules" / "junk.js").write_text("module.exports = 1;\n", encoding="utf-8")
    return root


@pytest.fixture
def service(tmp_path):
    config = Config(tmp_path / "runtime")
    config.values.update(deepseek_key="test-only-key", model="deepseek-flash")
    store = Store(config.root / "map.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    session = store.resolve(0, 0, 1, "map session")
    root = build_project(tmp_path / "project")
    store.update_session(session["id"], workspace=str(root))
    mapper = ProjectMap(config, store, agent, skills=None)
    yield mapper, store, session["id"], root, config, agent
    store.db.close()


def transport(responses, seen=None, hook=None, usage=None):
    """A fake DeepSeek stream: one scripted answer per request, with usage attached."""
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if seen is not None:
            seen.append(body)
        if hook:
            hook(len(calls))
        answer = responses.pop(0) if responses else '{"files": []}'
        chunks = [{"choices": [{"delta": {"content": answer}}]},
                  {"choices": [], "usage": usage or {"prompt_tokens": 1200, "completion_tokens": 300,
                                                     "prompt_cache_hit_tokens": 200}}]
        return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                                          + "data: [DONE]\n\n")
    return httpx.MockTransport(handler)


def scan_answer(paths):
    return json.dumps({"files": [{"path": p, "purpose": "Файл " + p, "key_symbols": ["run"],
                                  "depends_on": [], "tags": ["core"]} for p in paths]})


# 1 ----------------------------------------------------------------------------------
def test_inventory_lists_text_and_binary_files_with_language_and_hashes(service):
    mapper, _, sid, root, _, _ = service
    inventory = mapper.inventory(sid)
    paths = {r["path"]: r for r in inventory["records"]}

    assert "node_modules/junk.js" not in paths, "private and vendored directories are skipped"
    assert paths["pkg/core.py"]["language"] == "Python"
    assert paths["pkg/core.py"]["lines"] == 5
    assert len(paths["pkg/core.py"]["sha256"]) == 64
    assert paths["logo.png"]["binary"] and not paths["notes.md"]["binary"]
    assert inventory["binary_files"] == 1
    assert inventory["total_chars"] == sum(r.get("chars", 0) for r in inventory["records"] if not r["binary"])
    assert inventory["by_language"]["Python"]["files"] == 3


# 2 ----------------------------------------------------------------------------------
def test_dependency_edges_resolve_python_and_js_imports_inside_the_project(service):
    mapper, _, sid, _, _, _ = service
    inventory = mapper.inventory(sid)
    edges = {(e["source"], e["target"]) for e in inventory["edges"]}

    assert ("pkg/api.py", "pkg/core.py") in edges
    assert ("web/app.js", "web/view.js") in edges
    assert not any(e[1] == "json" for e in edges), "stdlib and npm packages are external, not edges"
    record = next(r for r in inventory["records"] if r["path"] == "web/app.js")
    assert "left-pad" in record["external_imports"]


# 3 ----------------------------------------------------------------------------------
def test_estimate_math_is_deterministic_and_priced(service):
    mapper, _, sid, _, config, _ = service
    assert token_estimate(320, 0.0) == 80, "code: four characters per token"
    assert token_estimate(320, 0.5) == 100, "Cyrillic prose: 3.2 characters per token"

    inventory = mapper.inventory(sid)
    estimate = mapper.estimate(sid, inventory)
    body = sum(r.get("est_tokens", 0) for r in inventory["records"] if not r["binary"])

    assert estimate["est_requests"] == 1, "a tiny project fits into one 24k request"
    assert estimate["est_input_tokens"] == body + pm.SYSTEM_OVERHEAD_TOKENS
    assert estimate["est_output_tokens"] == pm.OUTPUT_TOKENS_PER_FILE * estimate["scanned_files"]
    hit, miss, out = config["pricing"]["deepseek-flash"]
    assert estimate["est_cost_usd"] == pytest.approx(
        (estimate["est_input_tokens"] * miss + estimate["est_output_tokens"] * out) / 1e6, rel=1e-6)
    assert estimate["est_cost_usd_cached"] < estimate["est_cost_usd"], "80% cache hit must be cheaper"
    assert estimate["est_cost_map_generation"] > 0 and estimate["calibration"] == 1.0
    assert mapper.estimate(sid, inventory) == estimate


# 4 ----------------------------------------------------------------------------------
def test_git_information_comes_from_the_workspace_repository_only(service, tmp_path):
    mapper, _, sid, root, _, _ = service
    assert mapper.inventory(sid)["git"]["repository"] is False

    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin"}
    for args in (["init", "-q", "-b", "work"], ["add", "-A"], ["commit", "-qm", "first map commit"],
                 ["remote", "add", "origin", "https://user:secret@example.com/demo.git"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)
    (root / "dirty.txt").write_text("changed", encoding="utf-8")

    info = mapper.inventory(sid)["git"]
    assert info["repository"] and info["branch"] == "work"
    assert info["commits"][0]["subject"] == "first map commit" and len(info["head"]) == 12
    assert info["dirty"] == 1
    assert info["remotes"]["origin"] == "https://[REDACTED]@example.com/demo.git"
    assert "secret" not in json.dumps(info)


# 5 ----------------------------------------------------------------------------------
async def test_scan_job_records_usage_progress_and_produces_a_map(service):
    mapper, store, sid, root, config, agent = service
    inventory = mapper.inventory(sid)
    paths = [r["path"] for r in inventory["records"] if not r["binary"]]
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(transport=transport(
        [scan_answer(paths), "Проект из пакета pkg и веб-части web."])))

    job = mapper.start(sid, "summaries", 5.0, inventory)
    result = await mapper.wait(job["id"])

    assert result["state"] == "completed" and result["done"] == result["total"]
    assert result["stats"]["requests"] == 2, "one batch plus one map-generation request"
    assert result["stats"]["prompt_tokens"] == 2400 and result["stats"]["cost_usd"] > 0
    totals = store.usage(sid)
    assert totals["requests"] == 2 and totals["cache_hit_tokens"] == 400
    tagged = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM usage WHERE session_id=?", (sid,))]
    assert all(item["source"] == "project_map" for item in tagged)
    kinds = [e["kind"] for e in store.events(sid)]
    assert "map_progress" in kinds and "map_completed" in kinds

    document = mapper.latest(sid)["map"]
    names = {m["name"] for m in document["modules"]}
    assert {"pkg", "web", "(root)"} <= names
    assert document["overview"].startswith("Проект")
    assert {"source": "pkg", "target": "pkg", "origin": "static", "weight": 1} not in document["edges"]
    purposes = {f["path"]: f["purpose"] for m in document["modules"] for f in m["files"]}
    assert purposes["pkg/core.py"] == "Файл pkg/core.py"
    assert mapper.memory_context(sid).startswith("Карта проекта AIGent")
    assert len(mapper.memory_context(sid)) <= 3000


# 6 ----------------------------------------------------------------------------------
async def test_pause_finishes_the_current_request_and_resume_continues_from_the_next_batch(service, monkeypatch):
    mapper, store, sid, _, config, agent = service
    monkeypatch.setattr(pm, "BATCH_MAX_FILES", 1)
    inventory = mapper.inventory(sid)
    paths = [r["path"] for r in inventory["records"] if not r["binary"]]
    seen, holder = [], {}
    # Pause is requested while the very first request is still in flight.
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(
        transport=transport([scan_answer([p]) for p in paths] + ["Обзор."], seen,
                            hook=lambda n: n == 1 and mapper.pause(holder["id"]))))

    job = mapper.start(sid, "summaries", None, inventory)
    holder["id"] = job["id"]
    assert job["total"] == len(paths) > 2
    state = await mapper.wait(job["id"])

    assert state["state"] == "paused", state
    assert state["done"] == 1, "the in-flight request is finished and paid for, then the job stops"
    assert store.usage(sid)["requests"] == 1
    assert "map_paused" in [e["kind"] for e in store.events(sid)]

    mapper.resume(job["id"])
    final = await mapper.wait(job["id"])
    assert final["state"] == "completed" and final["done"] == len(paths)
    assert len(seen) == len(paths) + 1, "resumed work never repeats a finished batch"
    assert mapper.latest(sid) is not None


# 7 ----------------------------------------------------------------------------------
async def test_budget_guard_auto_pauses_before_the_request_that_would_exceed_it(service, monkeypatch):
    mapper, store, sid, _, config, agent = service
    monkeypatch.setattr(pm, "BATCH_MAX_FILES", 1)
    inventory = mapper.inventory(sid)
    paths = [r["path"] for r in inventory["records"] if not r["binary"]]
    # Each scripted request costs at least $0.30, so the $0.50 budget cannot survive the batch list.
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(
        transport=transport([scan_answer([p]) for p in paths],
                            usage={"prompt_tokens": 2_000_000, "completion_tokens": 10,
                                   "prompt_cache_hit_tokens": 0})))

    job = mapper.start(sid, "summaries", 0.5, inventory)
    state = await mapper.wait(job["id"])

    assert state["state"] == "paused"
    assert "лимит" in state["reason"].lower() and "$" in state["reason"]
    assert 0 < state["done"] < state["total"], "work stops before the request that would break the budget"
    assert store.usage(sid)["requests"] == state["done"], "no request is made after the guard trips"


# 8 ----------------------------------------------------------------------------------
async def test_cancel_stops_the_job_and_keeps_the_spend_recorded(service, monkeypatch):
    mapper, store, sid, _, config, agent = service
    monkeypatch.setattr(pm, "BATCH_MAX_FILES", 1)
    inventory = mapper.inventory(sid)
    paths = [r["path"] for r in inventory["records"] if not r["binary"]]
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(
        transport=transport([scan_answer([p]) for p in paths])))

    job = mapper.start(sid, "summaries", None, inventory)
    mapper.cancel(job["id"])
    state = await mapper.wait(job["id"])

    assert state["state"] == "cancelled"
    assert state["done"] < state["total"]
    assert store.usage(sid)["requests"] == state["stats"]["requests"]
    with pytest.raises(ValueError):
        mapper.resume(job["id"])


# 9 ----------------------------------------------------------------------------------
async def test_unparsable_answer_is_stored_raw_and_marked_unparsed(service):
    mapper, _, sid, _, config, agent = service
    inventory = mapper.inventory(sid)
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(
        transport=transport(["не JSON, а свободный текст", "Обзор."])))

    job = mapper.start(sid, "summaries", None, inventory)
    state = await mapper.wait(job["id"])

    assert state["state"] == "completed" and state["unparsed"] == state["files"]
    document = mapper.latest(sid)["map"]
    assert all(f["unparsed"] for m in document["modules"] for f in m["files"] if f["path"].endswith(".py"))


# 10 ---------------------------------------------------------------------------------
async def test_probe_spends_one_small_request_and_stores_the_calibration(service):
    mapper, store, sid, _, config, agent = service
    fenced = "```json\n" + scan_answer(["pkg/__init__.py"]) + "\n```"
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(transport=transport([fenced])))

    before = mapper.estimate(sid)["est_input_tokens"]
    result = await mapper.probe(sid)

    assert len(result["files"]) == 3 and result["parsed"], "a fenced JSON answer still parses"
    assert result["actual_input_tokens"] == 1200
    assert result["calibration"] == pytest.approx(1200 / result["expected_input_tokens"], rel=1e-3)
    assert store.usage(sid)["requests"] == 1
    after = mapper.estimate(sid)
    assert after["calibration"] == result["calibration"]
    assert after["est_input_tokens"] != before, "future estimates follow the measured calibration"


# 11 ---------------------------------------------------------------------------------
async def test_agent_context_carries_the_map_memory_when_a_map_exists(service):
    mapper, store, sid, _, config, agent = service
    inventory = mapper.inventory(sid)
    paths = [r["path"] for r in inventory["records"] if not r["binary"]]
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(
        transport=transport([scan_answer(paths), "Обзор проекта для памяти."])))
    store.message(sid, {"role": "user", "content": "привет"})

    agent.project_map = mapper
    assert "Карта проекта" not in agent.context(sid)[1]["content"], "no map yet, no extra context"

    job = mapper.start(sid, "summaries", None, inventory)
    await mapper.wait(job["id"])
    agent._project_guidance.pop(sid, None)
    guidance = agent.context(sid)[1]["content"]

    assert "Карта проекта AIGent" in guidance and "Обзор проекта для памяти." in guidance
    assert "Selected workspace" in guidance, "the project guidance still comes first"


# 12 ---------------------------------------------------------------------------------
def test_map_endpoints_require_admin_and_report_estimate_and_jobs(tmp_path):
    app = create_app(tmp_path / "runtime", polling=False)
    app.state.config.values.update(admin_password=password_hash("admin-password-1"),
                                   chat_password=password_hash("chat-pass"),
                                   deepseek_key="test-only-key")
    root = build_project(tmp_path / "project")
    with TestClient(app) as client:
        assert client.get("/api/sessions/x/map").status_code == 401
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        sid = client.post("/api/sessions", json={"title": "map"}).json()["id"]
        app.state.store.update_session(sid, workspace=str(root))

        estimate = client.get(f"/api/sessions/{sid}/map/estimate").json()
        assert estimate["estimate"]["est_requests"] >= 1
        assert estimate["inventory"]["by_language"]["Python"]["files"] == 3
        assert "калибровк" in estimate["token_test"].lower()

        state = client.get(f"/api/sessions/{sid}/map").json()
        assert state["map"] is None and state["jobs"] == [] and state["budget_usd"] == 0.5
        assert client.get(f"/api/sessions/{sid}/map/jobs").json() == []
        assert client.post(f"/api/sessions/{sid}/map/jobs/missing/pause").status_code == 404

        paths = [f["path"] for f in estimate["files"] if not f["binary"]]
        app.state.agent.deepseek = DeepSeek(app.state.config, httpx.AsyncClient(
            transport=transport([scan_answer(paths), "Обзор."])))
        job = client.post(f"/api/sessions/{sid}/map/scan", json={"mode": "summaries", "max_cost_usd": 1}).json()
        assert job["state"] == "running"

        deadline = time.time() + 5
        while time.time() < deadline:
            jobs = client.get(f"/api/sessions/{sid}/map/jobs").json()
            if jobs[0]["state"] == "completed":
                break
            time.sleep(0.05)
        assert jobs[0]["state"] == "completed", jobs
        document = client.get(f"/api/sessions/{sid}/map").json()
        assert {m["name"] for m in document["map"]["map"]["modules"]} >= {"pkg", "web"}
        assert document["memory_context"].startswith("Карта проекта")
        assert client.post(f"/api/sessions/{sid}/map/jobs/{job['id']}/resume").status_code == 400


def test_json_parsing_is_tolerant():
    assert ProjectMap.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ProjectMap.parse_json('Вот ответ: {"a": [1]} — всё') == {"a": [1]}
    assert ProjectMap.parse_json("совсем не json") is None


def test_module_grouping_and_remote_redaction():
    assert pm.module_of("pkg/core.py") == "pkg" and pm.module_of("run.py") == "(root)"
    assert pm.redact_remote("ssh://git@example.com/x.git") == "ssh://[REDACTED]@example.com/x.git"
    assert pm.redact_remote("https://example.com/x.git") == "https://example.com/x.git"


async def test_close_cancels_running_jobs(service):
    mapper, _, sid, _, config, agent = service
    agent.deepseek = DeepSeek(config, httpx.AsyncClient(transport=transport([])))
    inventory = mapper.inventory(sid)
    job = mapper.start(sid, "summaries", None, inventory)
    await asyncio.sleep(0)
    await mapper.close()
    assert mapper.job(job["id"])["state"] in ("paused", "completed", "running")
