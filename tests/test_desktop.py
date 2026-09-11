import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from connector.agent import Agent, safe_path
from connector.app import create_app
from connector.config import Config, password_hash
from connector.local_providers import LocalProviders, account_env
from connector.store import Store
from connector.workspace import WorkspaceService, parse_patch


@pytest.fixture
def service(tmp_path):
    config = Config(tmp_path)
    store = Store(tmp_path / "test.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    workspace = WorkspaceService(agent)
    agent.workspace_service = workspace
    session = store.resolve(0, 0, 1)
    yield agent, workspace, session
    store.db.close()


def test_patch_add_update_move_and_reject_ambiguity(tmp_path):
    plans = parse_patch(tmp_path, "*** Begin Patch\n*** Add File: app.py\n+one\n+two\n*** End Patch")
    assert plans["app.py"]["new"] == "one\ntwo\n"
    (tmp_path / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    plans = parse_patch(tmp_path, "*** Begin Patch\n*** Update File: app.py\n*** Move to: renamed.py\n@@\n one\n-two\n+three\n*** End Patch")
    assert plans["app.py"]["new"] is None
    assert plans["renamed.py"]["new"] == "one\nthree\n"
    (tmp_path / "ambiguous.py").write_text("same\nsame\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_patch(tmp_path, "*** Begin Patch\n*** Update File: ambiguous.py\n@@\n-same\n+changed\n*** End Patch")


@pytest.mark.parametrize("path", [".local/config.json", ".local/accounts/user/auth.json", ".codex/auth.json", ".claude/.credentials.json", "../escape"])
def test_runtime_and_authorizations_are_not_agent_files(tmp_path, path):
    with pytest.raises(ValueError):
        safe_path(tmp_path, path)


async def test_patch_approval_and_conflicting_revision(service):
    agent, workspace, session = service
    file = agent.workspace(session["id"]) / "code.py"
    file.write_text("old\n", encoding="utf-8")
    task = asyncio.create_task(workspace.execute(session, "apply_patch", {"patch": "*** Begin Patch\n*** Update File: code.py\n@@\n-old\n+new\n*** End Patch"}))
    await asyncio.sleep(.01)
    file.write_text("external edit\n", encoding="utf-8")
    agent.decide(next(iter(agent.approvals)), True, admin=True)
    with pytest.raises(ValueError, match="changed"):
        await task
    assert file.read_text() == "external edit\n"


def test_editor_conflicts_do_not_overwrite(service):
    agent, workspace, session = service
    first = workspace.save(session["id"], "new.py", "first", None)
    workspace.save(session["id"], "new.py", "external", first["revision"])
    with pytest.raises(ValueError, match="changed"):
        workspace.save(session["id"], "new.py", "stale", first["revision"])
    assert (agent.workspace(session["id"]) / "new.py").read_text() == "external"


async def test_terminal_stream_and_session_ownership(service):
    agent, workspace, session = service
    command = "Write-Output 'AIGENT_TERMINAL_TEST'" if os.name == "nt" else "printf AIGENT_TERMINAL_TEST"
    tid = await workspace.start_command(session, command)
    item = workspace.command(session["id"], tid)
    await asyncio.wait_for(item["reader"], 15)
    assert item["exit_code"] == 0
    assert "AIGENT_TERMINAL_TEST" in item["text"]
    with pytest.raises(ValueError, match="not found"):
        workspace.command("another-session", tid)
    events = agent.store.events(session["id"])
    assert any(e["kind"] == "terminal" and "AIGENT_TERMINAL_TEST" in e["payload"].get("delta", "") for e in events)


def test_account_environments_are_isolated_and_subscription_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    one = account_env({"provider": "codex", "auth_path": str(tmp_path / "one")}, tmp_path)
    two = account_env({"provider": "codex", "auth_path": str(tmp_path / "two")}, tmp_path)
    claude = account_env({"provider": "claude", "auth_path": str(tmp_path / "claude")}, tmp_path)
    assert one["CODEX_HOME"] != two["CODEX_HOME"]
    assert claude["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude")
    assert "OPENAI_API_KEY" not in one and "ANTHROPIC_API_KEY" not in claude


async def test_native_disconnect_finishes_waiting_turn(service):
    agent, _, session = service
    local = LocalProviders(agent)
    local.routes[("account", "thread")] = session
    future = asyncio.get_running_loop().create_future()
    local.done[session["id"]] = future
    await local.codex_event({"id": "account"}, {"method": "aigent/disconnected"})
    assert future.done()
    with pytest.raises(Exception, match="Codex"):
        await future


async def test_codex_usage_is_not_double_counted(service):
    agent, _, session = service
    session = agent.store.update_session(session["id"], provider="codex", account_id="codex-default")
    local = LocalProviders(agent)
    local.routes[("codex-default", "thread")] = session
    raw = {"inputTokens": 100, "outputTokens": 5, "cachedInputTokens": 64, "reasoningOutputTokens": 2, "totalTokens": 105}
    event = {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread", "turnId": "turn", "tokenUsage": {"total": raw, "last": raw}}}
    await local.codex_event({"id": "codex-default"}, event)
    await local.codex_event({"id": "codex-default"}, event)
    assert agent.store.usage(session["id"])["requests"] == 1
    assert agent.store.usage(session["id"])["cache_hit_tokens"] == 64


def test_desktop_api_projects_accounts_identity_and_editor(tmp_path):
    app = create_app(tmp_path / "state", polling=False)
    config = app.state.config
    config.values.update(admin_password=password_hash("test-admin-password"), chat_password=password_hash("test-chat-password"))
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + config["connector_token"]
        nonce = "test-challenge"
        assert len(client.get("/api/identity", params={"nonce": nonce}).json()["signature"]) == 64
        assert client.post("/api/desktop/session").json()["cookie"]
        project = client.post("/api/projects", json={"path": str(tmp_path), "name": "Fixture"}).json()
        account = client.post("/api/accounts", json={"provider": "codex", "name": "one@example.test"}).json()
        assert Path(account["auth_path"]).is_relative_to(config.root)
        assert client.patch("/api/accounts/" + account["id"], json={"browser_profile": "chrome:Profile 1"}).status_code == 200
        session = client.post("/api/sessions", json={"account_id": account["id"], "project_id": project["id"]}).json()
        assert session["provider"] == "codex" and session["workspace"] == str(tmp_path.resolve())
        sid = session["id"]
        file = client.put(f"/api/sessions/{sid}/editor", json={"path": "fixture.py", "text": "print(1)", "revision": None})
        assert file.status_code == 200
        stale = client.put(f"/api/sessions/{sid}/editor", json={"path": "fixture.py", "text": "wrong", "revision": "stale"})
        assert stale.status_code == 400
        assert client.patch(f"/api/sessions/{sid}", json={"archived": True}).status_code == 200
        assert any(s["id"] == sid for s in client.get("/api/sessions?archived=true").json())
        assert "/api/accounts/{aid}/login" in client.get("/openapi.json").json()["paths"]


def test_store_read_open_does_not_mark_running_turn_interrupted(tmp_path):
    path = tmp_path / "live.sqlite3"
    first = Store(path)
    s = first.resolve(0, 0, 0)
    first.execute("UPDATE sessions SET status='running' WHERE id=?", (s["id"],))
    reader = Store(path)
    assert reader.session(s["id"])["status"] == "running"
    reader.db.close()
    first.db.close()


def test_chat_delete_restore_and_full_history_fork(tmp_path):
    app = create_app(tmp_path / 'state', polling=False)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + app.state.config['connector_token']
        project = client.post('/api/projects', json={'path': str(tmp_path), 'name': 'Fixture'}).json()
        original = client.post('/api/sessions', json={'title': 'Disposable', 'project_id': project['id']}).json()
        sid = original['id']
        store = app.state.store
        store.message(sid, {'role': 'user', 'content': 'Keep this context'})
        for i in range(503):
            store.event(sid, 'notice', {'text': str(i)})
        file = tmp_path / 'keep.txt'
        file.write_text('Project file')
        fork = client.post(f'/api/sessions/{sid}/fork').json()
        assert store.history(fork['id']) == store.history(sid)
        assert store.rows('SELECT count(*) AS n FROM events WHERE session_id=?', (fork['id'],))[0]['n'] == 503
        assert client.delete(f'/api/sessions/{sid}').json()['recoverable']
        assert sid not in [s['id'] for s in client.get('/api/sessions').json()]
        assert sid in [s['id'] for s in client.get('/api/sessions?deleted=true').json()]
        assert file.read_text() == 'Project file'
        assert client.post(f'/api/sessions/{sid}/messages', json={'text':'Must not run'}).status_code == 404
        restored = client.post(f'/api/sessions/{sid}/restore').json()
        assert not restored['deleted'] and store.history(sid)


async def test_project_guidance_is_loaded_once_per_turn_and_private_dirs_hidden(service):
    agent, _, session = service
    sid = session['id']
    root = agent.workspace(sid)
    (root / 'AGENTS.md').write_text('Use this project only', encoding='utf-8')
    (root / 'README.md').write_text('Project overview', encoding='utf-8')
    (root / '.local').mkdir()
    (root / '.local' / 'private.txt').write_text('Must stay private', encoding='utf-8')
    agent.store.message(sid, {'role': 'user', 'content': 'Find project rules'})
    first = agent.context(sid)
    second = agent.context(sid)
    assert first == second
    assert first[1]['role'] == 'user'
    assert 'Use this project only' in first[1]['content']
    assert 'Must stay private' not in str(first)
    assert len([e for e in agent.store.events(sid) if e['kind'] == 'context']) == 1
    result = await agent.execute(session, 'list_files', {'path': '.'})
    assert '.local' not in str(result)


async def test_multistep_turn_sends_one_telegram_summary_and_preserves_tool_details(service):
    agent, _, session = service
    agent.store.execute('UPDATE sessions SET chat_id=123 WHERE id=?', (session['id'],))
    session = agent.store.session(session['id'])
    usage = {'prompt_tokens':100, 'completion_tokens':10, 'cache_hit_tokens':64, 'cache_miss_tokens':36,
             'cost_usd':0.001, 'saved_usd':0.002}
    agent.deepseek.complete.side_effect = [
        ({'role':'assistant', 'content':'', 'tool_calls':[{'id':'one', 'type':'function',
          'function':{'name':'list_files', 'arguments':'{"path":"."}'}}]}, dict(usage)),
        ({'role':'assistant', 'content':'Finished'}, dict(usage)),
    ]
    await agent.run(session, 'List the project')
    assert agent.telegram.text.await_count == 2  # Final answer and one total, no tool/step spam.
    assert '200' in agent.telegram.text.call_args_list[-1].args[1]
    events = agent.store.events(session['id'])
    assert [e['kind'] for e in events].count('usage') == 2
    assert [e['kind'] for e in events].count('turn_completed') == 1
    assert next(e for e in events if e['kind']=='tool_result')['payload']['call_id'] == 'one'


async def test_nested_workspace_cannot_modify_parent_git_repository(service):
    agent, workspace, session = service
    parent = agent.config.root / 'git-project'
    parent.mkdir()
    process = await asyncio.create_subprocess_exec('git', 'init', str(parent), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await process.communicate()
    child = parent / 'nested'
    child.mkdir()
    agent.store.update_session(session['id'], workspace=str(child))
    with pytest.raises(ValueError, match='Git'):
        await workspace.git(session['id'], ['status', '--porcelain'])


def test_admin_cookie_survives_restart_and_password_change_revokes_it(tmp_path):
    root = tmp_path / 'state'
    first = create_app(root, polling=False)
    first.state.config.values['admin_password'] = password_hash('restart-password')
    first.state.config.save()
    with TestClient(first) as client:
        assert client.post('/api/login', json={'password':'restart-password'}).status_code == 200
        cookie = client.cookies['ide_admin']
        stored = first.state.store.rows('SELECT * FROM admin_sessions')
        assert cookie not in str(stored)
    second = create_app(root, polling=False)
    with TestClient(second) as client:
        client.cookies.set('ide_admin', cookie)
        assert client.get('/api/sessions').status_code == 200
        second.state.config.values['admin_password'] = password_hash('changed-password')
        assert client.get('/api/sessions').status_code == 401


def test_retried_message_is_started_once(tmp_path):
    from unittest.mock import Mock
    app = create_app(tmp_path / 'state', polling=False)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + app.state.config['connector_token']
        sid = client.post('/api/sessions', json={'title':'Retry fixture'}).json()['id']
        app.state.agent.start = Mock()
        body = {'text':'Do this once', 'request_id':'retry-fixture-1'}
        assert client.post(f'/api/sessions/{sid}/messages', json=body).status_code == 202
        assert client.post(f'/api/sessions/{sid}/messages', json=body).json()['duplicate']
        assert app.state.agent.start.call_count == 1
        assert client.post(f'/api/sessions/{sid}/messages', json=body | {'text':'Different'}).status_code == 409
