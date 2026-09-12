import asyncio
import hashlib
import hmac
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .agent import GOAL_STATUSES, Agent, safe_path
from .api_entities import entities
from .config import Config, password_hash, verify_password
from .providers import DeepSeek, ProviderError, TelegramAPI, account_usage
from .store import Store
from .sync import SessionSync
from .telegram import Bot
from .local_providers import LocalProviders
from .updates import UpdateChannel
from .selfheal import SelfHeal
from .version import __version__
from .workspace import WorkspaceService
from .autorig import AutoRigTools
from .computer import ComputerTools
from .skill_manager import SkillIndex
from .project_map import ProjectMap
from .shared_tools import SharedTools
from .lifecycle import Lifecycle
from .subagents import SubAgents
from .video_tools import VideoTools
from .ssh_tools import SSHTools
from .browser_tools import BrowserTools
from .media import MediaService, MediaUnavailable, UnsupportedMedia
from .models import ModelError, ModelRegistry, asset_mime


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class Settings(BaseModel):
    deepseek_key: str = Field(default="", max_length=200)
    telegram_token: str = Field(default="", max_length=200)
    admin_password: str = Field(default="", max_length=200)
    chat_password: str = Field(default="", max_length=200)
    model: str = Field(default="deepseek-flash", min_length=1, max_length=100)
    thinking: bool = True
    allow_commands: bool = False
    max_output_tokens: int = Field(default=8192, ge=256, le=65536)
    max_context_chars: int = Field(default=1000000, ge=8000, le=8000000)
    max_steps: int = Field(default=12, ge=1, le=40)
    # The ceiling a turn may reach while its goal is active; omitted keeps the stored value.
    max_turn_steps: int | None = Field(default=None, ge=1, le=1000)
    # Coding workers: omitted keys keep their stored value.
    subagents_enabled: bool | None = None
    subagent_concurrency: int | None = Field(default=None, ge=1, le=8)
    subagent_max_tokens: int | None = Field(default=None, ge=256, le=65536)
    subagent_max_steps: int | None = Field(default=None, ge=1, le=200)
    update_repo: str = Field(default="eschota/AIGent", max_length=200)
    auto_update_check: bool = True
    # Omitted SSH keys keep their stored value: a settings form without them must not drop hosts.
    ssh_hosts: list[dict] | None = Field(default=None, max_length=50)
    ssh_binary: str | None = Field(default=None, max_length=400)
    ssh_timeout_seconds: int | None = Field(default=None, ge=5, le=3600)
    # Telegram session mirror; omitted keys keep their stored value.
    telegram_sync_chat_id: str | None = Field(default=None, max_length=40)
    telegram_sync: bool | None = None
    telegram_media_offload: bool | None = None
    telegram_media_offload_mb: int | None = Field(default=None, ge=1, le=2000)
    # Omitted media keys keep their stored value as well.
    ffmpeg_path: str | None = Field(default=None, max_length=400)
    media_transcode_timeout_seconds: int | None = Field(default=None, ge=10, le=3600)
    media_cache_mb: int | None = Field(default=None, ge=16, le=100000)
    # Omitted web/browser keys keep their stored value too.
    allow_web: bool | None = None
    web_search_url: str | None = Field(default=None, max_length=500)
    web_allow_private: bool | None = None
    browser_binary: str | None = Field(default=None, max_length=400)
    browser_timeout_seconds: int | None = Field(default=None, ge=5, le=600)


class ChatBody(BaseModel):
    text: str = Field(min_length=1, max_length=40000)
    request_id: str | None = Field(default=None, max_length=100)
    attachments: list[str] = Field(default_factory=list, max_length=8)
    queue: bool = True


class SessionBody(BaseModel):
    title: str = Field(default="Web session", min_length=1, max_length=100)
    account_id: str = "deepseek-default"
    model: str = ""
    effort: str = "medium"
    project_id: str | None = None


class AccountBody(BaseModel):
    provider: str
    name: str = Field(min_length=1, max_length=120)
    browser_profile: str = ""
    api_key: str = ""


class ProjectBody(BaseModel):
    path: str
    name: str = ""


class SessionUpdate(BaseModel):
    title: str | None = None
    model: str | None = None
    effort: str | None = None
    archived: bool | None = None
    pinned: bool | None = None
    auto_approve: bool | None = None
    auto_continue: bool | None = None
    project_id: str | None = None


class MediaPathBody(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class SkillAttachBody(BaseModel):
    session_id: str


class MapScanBody(BaseModel):
    mode: str = "summaries"
    max_cost_usd: float | None = Field(default=None, ge=0, le=100)


class FileLocation(BaseModel):
    path: str = Field(min_length=1, max_length=400)


class AsyncAnswer(BaseModel):
    answer: str = Field(default="", max_length=4000)


class ToolBody(BaseModel):
    args: dict[str, str | int] = Field(default_factory=dict)


class SubAgentTask(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    files: list[str] | None = Field(default=None, max_length=20)
    context: str | None = Field(default=None, max_length=8000)
    score: float | None = Field(default=None, ge=0, le=1)


class SubAgentsBody(BaseModel):
    tasks: list[SubAgentTask] = Field(min_length=1, max_length=8)
    shared_context: str = Field(default="", max_length=40000)


class AnswerBody(BaseModel):
    answers: dict[str, str | list[str]]


class FileBody(BaseModel):
    path: str
    text: str = Field(max_length=1000000)
    revision: str | None = None


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=20000)
    workdir: str = "."


class StdinBody(BaseModel):
    text: str = Field(max_length=20000)


class AccountUpdate(BaseModel):
    browser_profile: str


class GitBody(BaseModel):
    action: str
    paths: list[str] = []
    message: str = ""


class DecisionBody(BaseModel):
    accepted: bool


class HealBody(BaseModel):
    error_ref: int | None = None


class HealConfirmBody(BaseModel):
    verified: bool
    restart: bool = False


class GoalStep(BaseModel):
    text: str = Field(default="", max_length=300)
    step: str = Field(default="", max_length=300)
    status: str = "pending"


class GoalBody(BaseModel):
    """The user editing the objective from the IDE; it reaches the model in the next turn note."""
    goal: str | None = Field(default=None, max_length=2000)
    status: str | None = None
    note: str | None = Field(default=None, max_length=500)
    steps: list[GoalStep] | None = Field(default=None, max_length=40)
    auto_continue: bool | None = None


class ComputerBody(BaseModel):
    window_id: int | None = None


class TelegramBody(BaseModel):
    kind: str
    payload: dict


class CompletionBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[dict]
    stream: bool = False


class ModelSidecarBody(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    patch: dict = Field(default_factory=dict)


class ModelReferenceBody(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    image: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=500)


def create_app(root: Path | None = None, polling=True):
    config = Config(root or Path(__file__).resolve().parents[1] / ".local")
    store = Store(config.root / "sessions.sqlite3", recover=True)
    client = httpx.AsyncClient()
    telegram = TelegramAPI(config, client)
    agent = Agent(config, store, DeepSeek(config, client), telegram)
    local = LocalProviders(agent)
    agent.local = local
    workspace_service = WorkspaceService(agent)
    agent.workspace_service = workspace_service
    media = MediaService(config, agent.workspace)
    agent.media = media
    models3d = ModelRegistry(config, agent.workspace, client)
    agent.models3d = models3d
    autorig = AutoRigTools(agent, client)
    agent.extensions.append(autorig)
    computer = ComputerTools(agent)
    agent.extensions.append(computer)
    ssh = SSHTools(agent)
    agent.extensions.append(ssh)
    browser = BrowserTools(agent, client)
    agent.extensions.append(browser)
    skills = SkillIndex(config, store, agent.workspace)
    project_map = ProjectMap(config, store, agent, skills)
    agent.project_map = project_map
    shared = SharedTools(agent, client, skills)
    agent.extensions.append(shared)
    subagents = SubAgents(agent, client)
    agent.extensions.append(subagents)
    video = VideoTools(agent, media)
    agent.extensions.append(video)
    lifecycle = Lifecycle(agent, store, config)
    agent.extensions.append(lifecycle)
    agent.after_turn = lifecycle.after_turn
    bot = Bot(config, store, telegram, agent)
    sync = SessionSync(config, store, telegram, agent)
    agent.sync = sync
    updates = UpdateChannel(client, config["update_repo"])
    selfheal = SelfHeal(agent, store, config)
    failures = {}

    @asynccontextmanager
    async def lifespan(app):
        if polling:
            bot.task = asyncio.create_task(bot.poll())
        skills.start()
        shared.resume()
        sync.start()
        if sync.enabled:
            # Give sessions created while the mirror was off a topic, then mirror the missed events.
            async def resume_sync():
                await sync.backfill()
                for item in store.sessions():
                    await sync.catch_up(item["id"])
            app.state.sync_task = asyncio.create_task(resume_sync())
        # Nothing of the previous process survives a restart: close the turns the crash cut in half
        # before starting anything, so no session keeps claiming to be running.
        app.state.restarted = agent.reconcile_restart()
        for session in store.sessions():
            if store.queued(session["id"]):
                agent.drain(session["id"])
        yield
        task = getattr(app.state, "sync_task", None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await sync.close()
        await shared.close()
        await subagents.close()
        await project_map.close()
        await skills.stop()
        if bot.task:
            bot.task.cancel()
            await asyncio.gather(bot.task, return_exceptions=True)
        await agent.shutdown()
        await client.aclose()
        store.db.close()

    app = FastAPI(title="AIGent", version=__version__, lifespan=lifespan,
                  description="Local agent connector. Authenticate with the admin cookie or connector Bearer token.")
    app.state.config, app.state.store, app.state.agent, app.state.bot = config, store, agent, bot
    app.state.client, app.state.skills, app.state.shared = client, skills, shared
    app.state.subagents = subagents
    app.state.lifecycle = lifecycle
    app.state.project_map = project_map
    app.state.sync = sync
    app.state.models3d = models3d
    app.state.started = time.time()
    app.state.updates = updates
    app.state.selfheal = selfheal

    @app.middleware("http")
    async def protections(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.cookies.get("ide_admin"):
            origin = request.headers.get("origin")
            if request.headers.get("x-requested-with") != "DeepSeekIDE" or (origin and origin != str(request.base_url).rstrip("/")):
                return Response("Cross-origin request denied", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Immutable media variants carry their own caching header; everything else stays uncached.
        if "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            # The 3D viewer needs two narrow additions: worker-src for the DRACO and KTX2 decoder workers,
            # which three.js builds from a same-origin blob, and 'wasm-unsafe-eval' for their WebAssembly
            # decoders. Neither permits inline script, eval or a remote origin; scripts stay at 'self'.
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        return response

    def bearer(request):
        return request.headers.get("authorization", "").removeprefix("Bearer ")

    def require_admin(request: Request, authorization: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False))):
        token = bearer(request)
        if token and hmac.compare_digest(token, config["connector_token"]):
            return True
        digest = hashlib.sha256(request.cookies.get("ide_admin", "").encode()).hexdigest()
        entries = store.rows("SELECT * FROM admin_sessions WHERE token_hash=?", (digest,))
        entry = entries[0] if entries else None
        revision = hashlib.sha256(config["admin_password"].encode()).hexdigest()
        if entry and entry["expires"] > time.time() and hmac.compare_digest(entry["credential_revision"], revision):
            return True
        raise HTTPException(401, "Administrator authentication required")

    def issue_admin_session():
        token, expires = secrets.token_urlsafe(32), time.time() + 43200
        store.execute("DELETE FROM admin_sessions WHERE expires<?", (time.time(),))
        store.execute("INSERT INTO admin_sessions(token_hash,expires,credential_revision) VALUES (?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), expires, hashlib.sha256(config["admin_password"].encode()).hexdigest()))
        return token, expires

    def require_session(sid):
        session = store.session(sid)
        if not session or session.get("deleted"):
            raise HTTPException(404, "Session not found")
        return session

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return Response(json.dumps({"detail": config.redact(exc)}), 502, media_type="application/json")

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return Response(json.dumps({"detail": str(exc)}), 400, media_type="application/json")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "aigent", "version": __version__, "configured": config.ready}

    @app.get("/api/identity")
    async def identity(nonce: str):
        if len(nonce) > 128:
            raise HTTPException(400, "Invalid challenge")
        return {"signature": hmac.new(config["connector_token"].encode(), ("aigent:" + nonce).encode(), hashlib.sha256).hexdigest()}

    @app.post("/api/desktop/session", dependencies=[Depends(require_admin)])
    async def desktop_session():
        token, expires = issue_admin_session()
        return {"cookie": token, "expires": expires}

    @app.get("/api/bootstrap")
    async def bootstrap():
        return {"setup_required": not config.ready}

    def apply_settings(body):
        values = body.model_dump()
        for key in ("admin_password", "chat_password"):
            if values[key]:
                minimum = 10 if key == "admin_password" else 4
                if len(values[key]) < minimum:
                    raise HTTPException(422, f"{key}: minimum {minimum} characters")
                values[key] = password_hash(values[key])
            else:
                values.pop(key)
        for key in ("deepseek_key", "telegram_token"):
            if not values[key].strip():
                values.pop(key)
        for key in ("telegram_sync_chat_id", "telegram_sync", "telegram_media_offload",
                    "telegram_media_offload_mb", "ssh_hosts", "ssh_binary", "ssh_timeout_seconds",
                    "ffmpeg_path", "media_transcode_timeout_seconds", "media_cache_mb",
                    "allow_web", "web_search_url", "web_allow_private", "browser_binary",
                    "browser_timeout_seconds", "subagents_enabled", "subagent_concurrency",
                    "subagent_max_tokens", "subagent_max_steps", "max_turn_steps"):
            if values.get(key) is None:
                values.pop(key, None)
        if "chat_password" in values:
            config.values["auth_epoch"] += 1
        config.values.update(values)
        config.save()

    @app.post("/api/setup")
    async def setup(body: Settings, request: Request):
        if config.ready or not hmac.compare_digest(bearer(request), config["setup_token"]):
            raise HTTPException(403, "Open the setup URL printed by run.py on this server")
        if len(body.admin_password) < 10 or len(body.chat_password) < 4:
            raise HTTPException(422, "Set admin password (10+ characters) and chat password (4+ characters)")
        apply_settings(body)
        config.values["setup_token"] = ""
        config.save()
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: PasswordBody, request: Request, response: Response):
        address = request.client.host
        count, until = failures.get(address, (0, 0))
        if time.time() < until:
            raise HTTPException(429, "Too many attempts; retry in five minutes")
        if not verify_password(body.password, config["admin_password"]):
            failures[address] = (count + 1, time.time() + 300 if count >= 4 else 0)
            raise HTTPException(401, "Invalid password")
        failures.pop(address, None)
        token, _ = issue_admin_session()
        response.set_cookie("ide_admin", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=43200)
        return {"ok": True}

    @app.post("/api/logout", dependencies=[Depends(require_admin)])
    async def logout(request: Request, response: Response):
        store.execute("DELETE FROM admin_sessions WHERE token_hash=?", (hashlib.sha256(request.cookies.get("ide_admin", "").encode()).hexdigest(),))
        response.delete_cookie("ide_admin")
        return {"ok": True}

    @app.get("/api/settings", dependencies=[Depends(require_admin)])
    async def settings():
        return config.public()

    @app.post("/api/settings", dependencies=[Depends(require_admin)])
    async def save_settings(body: Settings):
        apply_settings(body)
        return {"ok": True}

    @app.post("/api/connector-token", dependencies=[Depends(require_admin)])
    async def rotate_connector():
        config.values["connector_token"] = secrets.token_urlsafe(32)
        config.save()
        return {"token": config["connector_token"]}

    def ui_revision():
        """Newest timestamp of the served interface files; a changed value means an open page is stale."""
        folder = Path(__file__).parent / "static"
        return str(int(max((p.stat().st_mtime for p in folder.iterdir() if p.is_file()), default=0)))

    def supervisor_state():
        """What the supervisor recorded about restarts and rollbacks, if it is running."""
        path = config.root / "supervisor.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    @app.get("/api/status", dependencies=[Depends(require_admin)])
    async def status():
        return {"bot": bot.status, "username": bot.username, "error": bot.last_error,
                "ui_revision": ui_revision(), "supervisor": supervisor_state(),
                "started": app.state.started, "restarted": getattr(app.state, "restarted", None),
                "usage": store.usage(), "sessions": len(store.sessions()),
                "running": len(agent.jobs), "model": config["model"]}

    @app.get("/api/update", dependencies=[Depends(require_admin)])
    async def update_status(force: bool = False):
        """What the running build is and what the published release offers."""
        return await updates.check(__version__, force=force)

    @app.get("/api/balance", dependencies=[Depends(require_admin)])
    async def balance():
        try:
            return await bot.balance()
        except httpx.HTTPError:
            raise HTTPException(502, "DeepSeek balance network error") from None

    @app.get("/api/sessions", dependencies=[Depends(require_admin)])
    async def list_sessions(archived: bool = False, deleted: bool = False):
        return [s | {"usage": store.usage(s["id"]), "telegram": sync.info(s),
                     "resume": agent.pending_resume(s["id"]), "spark": store.token_series(s["id"])}
                for s in store.sessions(archived, deleted)]

    @app.post("/api/sessions/{sid}/resume", dependencies=[Depends(require_admin)])
    async def resume_session(sid: str):
        """Continue the turn a restart interrupted; the offer is consumed on acceptance."""
        session = require_session(sid)
        pending = agent.pending_resume(sid)
        if not pending and session.get("status") != "interrupted":
            raise HTTPException(409, "Nothing to resume: this session was not interrupted by a restart")
        try:
            return agent.accept_resume(session) | {"resumed": True}  # type: ignore[operator]
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/api/sessions/{sid}", dependencies=[Depends(require_admin)])
    async def delete_session(sid: str):
        require_session(sid)
        agent.stop(sid)
        store.execute("UPDATE sessions SET deleted=1,active=0 WHERE id=?", (sid,))
        # The forum topic is closed, never deleted: a restored session keeps its history and media.
        await sync.closed(store.session(sid))
        return {"deleted": True, "recoverable": True, "files_preserved": True}

    @app.post("/api/sessions/{sid}/restore", dependencies=[Depends(require_admin)])
    async def restore_session(sid: str):
        if not store.session(sid):
            raise HTTPException(404, "Session not found")
        return store.update_session(sid, deleted=False, archived=False)

    @app.post("/api/sessions", dependencies=[Depends(require_admin)])
    async def new_session(body: SessionBody):
        account = store.account(body.account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        session = store.resolve(0, 0, 0, body.title, new=True)
        workspace = None
        if body.project_id:
            projects = store.rows("SELECT * FROM projects WHERE id=?", (body.project_id,))
            if not projects:
                raise HTTPException(404, "Project not found")
            workspace = projects[0]["path"]
        session = store.update_session(session["id"], provider=account["provider"], account_id=account["id"],
            model=body.model, effort=body.effort, workspace=workspace, project_id=body.project_id)
        return await sync.ensure_topic(session)

    @app.patch("/api/sessions/{sid}", dependencies=[Depends(require_admin)])
    async def update_session(sid: str, body: SessionUpdate):
        session = require_session(sid)
        fields = body.model_dump(exclude_unset=True, exclude_none=True)
        if "project_id" in fields:
            target = fields["project_id"]
            if agent.busy(sid) and (target or None) != session.get("project_id"):
                raise HTTPException(409, "Дождитесь окончания хода или остановите его перед сменой проекта")
            if target:
                found = store.rows("SELECT * FROM projects WHERE id=?", (target,))
                if not found:
                    raise HTTPException(404, "Project not found")
                fields["workspace"] = found[0]["path"]
            else:
                fields["project_id"], fields["workspace"] = None, None
        for flag in ("auto_approve", "auto_continue"):
            if flag in fields:
                fields[flag] = int(fields[flag])
        if not fields:
            return session
        updated = store.update_session(sid, **fields)
        if "workspace" in fields:
            agent._project_guidance.pop(sid, None)
            store.event(sid, "notice", {"text": "Рабочая папка сессии: " + (updated["workspace"] or "отдельная папка сессии")})
        if "auto_approve" in fields:
            store.event(sid, "notice", {"text": "Автоприменение команд и правок: "
                                                + ("включено" if updated["auto_approve"] else "выключено")})
        if "title" in fields:
            try:
                await sync.rename(updated, updated["title"])
            except ProviderError as exc:
                store.event(sid, "notice", {"text": "Не удалось переименовать топик Telegram: " + config.redact(exc)})
        return updated

    @app.post("/api/sessions/{sid}/fork", dependencies=[Depends(require_admin)])
    async def fork_session(sid: str):
        original = require_session(sid)
        if sid in agent.jobs:
            raise HTTPException(409, "Wait for the current turn or stop it before forking")
        fields = {k: original[k] for k in ("provider", "account_id", "model", "effort", "workspace", "project_id")}
        fields["workspace"] = str(agent.workspace(sid))
        if original["provider"] == "codex" and original.get("external_id"):
            result = await local.rpc(store.account(original["account_id"])).call("thread/fork", {"threadId": original["external_id"]})
            fields["external_id"] = result["thread"]["id"]
        elif original["provider"] == "claude" and original.get("external_id"):
            fields.update(external_id=original["external_id"], forked=True)
        target = store.resolve(original["chat_id"], original["topic_id"], original["user_id"], original["title"] + " · fork", new=True)
        for message in store.history(sid):
            store.message(target["id"], message)
        cursor = 0
        while True:
            events = store.events(sid, cursor)
            for event in events:
                store.event(target["id"], event["kind"], event["payload"] | {"inherited": True})
            if len(events) < 500:
                break
            cursor = events[-1]["id"]
        return store.update_session(target["id"], **fields)

    @app.post("/api/sessions/{sid}/heal", dependencies=[Depends(require_admin)])
    async def heal(sid: str, body: HealBody):
        """From an error in this session, spawn a fix chat that knows the context and starts fixing."""
        require_session(sid)
        return selfheal.create_fix_session(sid, body.error_ref)

    @app.get("/api/sessions/{sid}/heal", dependencies=[Depends(require_admin)])
    async def heal_for_source(sid: str):
        require_session(sid)
        return selfheal.list_fixes(sid)

    @app.get("/api/heal", dependencies=[Depends(require_admin)])
    async def heal_list():
        return selfheal.list_fixes()

    @app.post("/api/heal/{fix_sid}/confirm", dependencies=[Depends(require_admin)])
    async def heal_confirm(fix_sid: str, body: HealConfirmBody):
        return selfheal.confirm_fix(fix_sid, body.verified, body.restart)

    @app.post("/api/heal/{fix_sid}/restart", dependencies=[Depends(require_admin)])
    async def heal_restart(fix_sid: str):
        """Explicit, user-confirmed restart request. Refused while another turn is running."""
        return selfheal.restart_client(exclude_sid=fix_sid)

    @app.get("/api/projects", dependencies=[Depends(require_admin)])
    async def projects():
        return store.rows("SELECT * FROM projects ORDER BY created DESC")

    @app.get("/api/sessions/{sid}/autorig", dependencies=[Depends(require_admin)])
    async def autorig_status(sid: str):
        require_session(sid)
        return autorig.state(sid)

    @app.post("/api/sessions/{sid}/autorig", dependencies=[Depends(require_admin)])
    async def autorig_enable(sid: str, body: DecisionBody):
        require_session(sid)
        return autorig.enable(sid, body.accepted)

    @app.get("/api/computer/windows", dependencies=[Depends(require_admin)])
    async def computer_windows():
        return await asyncio.to_thread(computer.desktop.windows) if computer.desktop else []

    @app.get("/api/sessions/{sid}/computer", dependencies=[Depends(require_admin)])
    async def computer_status(sid: str):
        require_session(sid)
        return {"window": computer.bindings.get(sid), "supported": computer.desktop is not None}

    @app.post("/api/sessions/{sid}/computer", dependencies=[Depends(require_admin)])
    async def computer_select(sid: str, body: ComputerBody):
        selected = require_session(sid)
        if selected["provider"] != "deepseek":
            raise HTTPException(400, "Computer tools currently connect to DeepSeek sessions")
        return {"window": computer.bind(sid, body.window_id)}

    def media_file(sid: str, path: str):
        """Serve a workspace image or video for viewing inside the interface, never as a download."""
        from PIL import Image
        require_session(sid)
        file = safe_path(agent.workspace(sid), path)
        if not file.is_file():
            raise HTTPException(404, "File not found")
        video = {".mp4": "video/mp4", ".webm": "video/webm"}.get(file.suffix.lower())
        if video:
            return FileResponse(file, media_type=video)
        try:
            with Image.open(file) as decoded:
                if decoded.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise HTTPException(415, "Not a supported image")
                mime = Image.MIME[decoded.format]
                decoded.verify()
        except HTTPException:
            raise
        except Exception:
            # A truncated or corrupted file is a normal outcome; the interface shows a placeholder.
            raise HTTPException(415, "Файл повреждён или не является поддерживаемым изображением") from None
        return FileResponse(file, media_type=mime)

    @app.get("/api/sessions/{sid}/image", dependencies=[Depends(require_admin)])
    async def image_preview(sid: str, path: str):
        return media_file(sid, path)

    @app.get("/api/media/capabilities", dependencies=[Depends(require_admin)])
    async def media_capabilities():
        return media.capabilities()

    @app.get("/api/sessions/{sid}/media", dependencies=[Depends(require_admin)])
    async def media_variant(sid: str, path: str, variant: str = "original"):
        """Serve the original file, a thumbnail, a browser-ready preview or a poster frame.

        Cache keys include the source mtime and size, so a served variant is immutable.
        A preview still transcoding answers 202 and the client polls the same URL.
        """
        require_session(sid)
        try:
            result = await media.render(sid, path, variant)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except UnsupportedMedia as exc:
            raise HTTPException(415, str(exc)) from None
        except MediaUnavailable as exc:
            raise HTTPException(503, config.redact(exc)) from None
        if result["status"] == "processing":
            return Response(json.dumps({"status": "processing", "variant": result["variant"]}),
                            202, media_type="application/json")
        return FileResponse(result["path"], media_type=result["media_type"],
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.head("/api/sessions/{sid}/media", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def media_variant_head(sid: str, path: str, variant: str = "thumb"):
        """The interface polls a running transcode with HEAD, so waiting costs no download."""
        return await media_variant(sid, path, variant)

    @app.get("/api/sessions/{sid}/media/telegram", dependencies=[Depends(require_admin)])
    async def media_from_telegram(sid: str, path: str):
        """Serve a media file, re-downloading it from Telegram when the local cache is empty."""
        require_session(sid)
        file = await sync.restore(sid, path)
        return FileResponse(file, filename=file.name, media_type=asset_mime(file))

    @app.post("/api/sessions/{sid}/media/evict", dependencies=[Depends(require_admin)])
    async def media_evict(sid: str, body: MediaPathBody):
        """Delete the local copy of a file that is verifiably stored in Telegram."""
        require_session(sid)
        return sync.evict(sid, body.path)

    @app.get("/api/sessions/{sid}/sync", dependencies=[Depends(require_admin)])
    async def session_sync(sid: str):
        return sync.info(require_session(sid))

    @app.post("/api/sync/backfill", dependencies=[Depends(require_admin)])
    async def sync_backfill():
        """Give every session without a topic one. Idempotent, rate limited, resumable."""
        result = await sync.backfill()
        await sync.drain()
        return result

    @app.get("/api/sessions/{sid}/media/info", dependencies=[Depends(require_admin)])
    async def media_info(sid: str, path: str):
        require_session(sid)
        try:
            return await media.info(sid, path)
        except UnsupportedMedia as exc:
            raise HTTPException(415, str(exc)) from None
        except MediaUnavailable as exc:
            raise HTTPException(503, config.redact(exc)) from None

    # ---------------------------------------------------------------- 3D models and the viewer preset
    @app.get("/api/graphics/preset", dependencies=[Depends(require_admin)])
    async def graphics_preset(refresh: bool = False):
        """Gravity House server graphics record plus the derived quality levels 1/2/3.

        Offline, or with web access switched off, the embedded revision-24 record is used and the
        answer says so in "source".
        """
        return await models3d.preset(refresh)

    @app.get("/api/sessions/{sid}/models", dependencies=[Depends(require_admin)])
    async def list_models(sid: str):
        """Every model file in the workspace. A missing sidecar is created on first listing."""
        require_session(sid)
        return await asyncio.to_thread(models3d.list, sid)

    @app.get("/api/sessions/{sid}/models/sidecar", dependencies=[Depends(require_admin)])
    async def model_sidecar(sid: str, path: str):
        require_session(sid)
        try:
            return await asyncio.to_thread(models3d.sidecar, sid, path)
        except ModelError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/sessions/{sid}/models/sidecar", dependencies=[Depends(require_admin)])
    async def model_sidecar_update(sid: str, body: ModelSidecarBody):
        require_session(sid)
        try:
            return await asyncio.to_thread(models3d.update_sidecar, sid, body.path, body.patch)
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/sessions/{sid}/models/reference", dependencies=[Depends(require_admin)])
    async def model_reference(sid: str, body: ModelReferenceBody):
        require_session(sid)
        try:
            return await asyncio.to_thread(models3d.add_reference, sid, body.path, body.image, body.note)
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/sessions/{sid}/asset/{path:path}", dependencies=[Depends(require_admin)])
    async def workspace_asset(sid: str, path: str):
        """Serve a workspace file inline with its real media type.

        A .gltf model references its .bin buffers and textures by relative URL, so the loader needs
        every sibling file at a predictable address; safe_path keeps that inside the workspace.
        """
        require_session(sid)
        file = safe_path(agent.workspace(sid), path)
        if not file.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(file, media_type=asset_mime(file))
    @app.post("/api/sessions/{sid}/file-path", dependencies=[Depends(require_admin)])
    async def file_location(sid: str, body: FileLocation):
        """The desktop shell needs a real path to put a file on the OS clipboard."""
        require_session(sid)
        file = safe_path(agent.workspace(sid), body.path)
        if not file.is_file():
            raise HTTPException(404, "File not found")
        return {"absolute": str(file), "name": file.name, "bytes": file.stat().st_size}


    @app.post("/api/projects", dependencies=[Depends(require_admin)])
    async def add_project(body: ProjectBody):
        path = Path(body.path).resolve()
        if not path.is_dir():
            raise HTTPException(400, "Select an existing project directory")
        found = store.rows("SELECT * FROM projects WHERE path=?", (str(path),))
        if found:
            return found[0]
        pid = uuid.uuid4().hex[:16]
        store.execute("INSERT INTO projects VALUES (?,?,?,?)", (pid, body.name or path.name, str(path), time.time()))
        return store.rows("SELECT * FROM projects WHERE id=?", (pid,))[0]

    @app.get("/api/accounts", dependencies=[Depends(require_admin)])
    async def accounts(refresh: bool = False):
        async def describe(account):
            if account["provider"] == "deepseek":
                status = {"connected": bool(config["deepseek_key"] if account["id"] == "deepseek-default" else config["account_keys"].get(account["id"])),
                          "source": "DeepSeek API", "checked_at": time.time(),
                          "models": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}
            else:
                status = await local.status(account, refresh)
            return {k: v for k, v in account.items() if k != "metadata"} | {"status": status}
        return await asyncio.gather(*(describe(a) for a in store.accounts()))

    @app.post("/api/accounts", dependencies=[Depends(require_admin)])
    async def add_account(body: AccountBody):
        if body.provider not in ("codex", "claude", "deepseek"):
            raise HTTPException(400, "Unknown provider")
        aid = uuid.uuid4().hex[:16]
        path = config.root / "accounts" / aid
        path.mkdir(parents=True, exist_ok=True)
        store.execute("INSERT INTO accounts(id,provider,name,auth_path,browser_profile,created) VALUES (?,?,?,?,?,?)",
                      (aid, body.provider, body.name, str(path), body.browser_profile, time.time()))
        if body.api_key:
            config.values["account_keys"][aid] = body.api_key
            config.save()
        return store.account(aid)

    @app.post("/api/accounts/{aid}/login", dependencies=[Depends(require_admin)])
    async def account_login(aid: str):
        account = store.account(aid)
        if not account:
            raise HTTPException(404, "Account not found")
        if account["provider"] == "codex":
            if not account.get("auth_path") and (await local.status(account, True)).get("connected"):
                return {"state": "connected", "url": None, "browser_profile": account["browser_profile"]}
            result = await local.rpc(account).call("account/login/start", {"type": "chatgpt"})
            return {"url": result.get("authUrl"), "state": "waiting", "browser_profile": account["browser_profile"]}
        if account["provider"] == "claude":
            raise HTTPException(409, "Войдите в официальный Claude Code CLI для этого профиля. AIGent использует готовую CLI-авторизацию и не предоставляет вход Claude.ai через SDK.")
        raise HTTPException(400, "DeepSeek uses an API key")

    @app.patch("/api/accounts/{aid}", dependencies=[Depends(require_admin)])
    async def update_account(aid: str, body: AccountUpdate):
        if not store.account(aid):
            raise HTTPException(404, "Account not found")
        store.execute("UPDATE accounts SET browser_profile=? WHERE id=?", (body.browser_profile, aid))
        return {"ok": True}

    @app.get("/api/accounts/{aid}/login", dependencies=[Depends(require_admin)])
    async def account_login_status(aid: str):
        account = store.account(aid)
        if not account:
            raise HTTPException(404, "Account not found")
        status = await local.status(account, True)
        result = {"connected": status.get("connected", False), "status": status, "url": None}
        return result

    @app.get("/api/accounts/{aid}/sessions", dependencies=[Depends(require_admin)])
    async def native_sessions(aid: str):
        account = store.account(aid)
        if not account:
            raise HTTPException(404, "Account not found")
        if account["provider"] == "codex":
            result = await local.rpc(account).call("thread/list", {"limit": 50, "useStateDbOnly": True})
            return [{"id": t["id"], "title": t.get("name") or t.get("preview") or t["id"],
                     "cwd": t.get("cwd"), "status": t.get("status")} for t in result.get("data", [])]
        if account["provider"] == "claude":
            return await local.claude_history(account)
        return []

    @app.post("/api/accounts/{aid}/sessions/{external_id}/import", dependencies=[Depends(require_admin)])
    async def import_native(aid: str, external_id: str):
        account = store.account(aid)
        if not account or account["provider"] not in ("codex", "claude"):
            raise HTTPException(400, "Choose a local Codex or Claude account")
        if account["provider"] == "claude":
            history = await local.claude_history(account, external_id)
            info = history.get("info")
            if not info:
                raise HTTPException(404, "Claude session not found in this account")
            session = store.resolve(0, 0, 0, info.get("summary") or "Imported Claude fork", new=True)
            session = store.update_session(session["id"], provider="claude", account_id=aid, external_id=external_id,
                                           workspace=info.get("cwd"), forked=True)
            for item in history.get("messages", []):
                message = item.get("message", {})
                role = message.get("role", item.get("type"))
                content = message.get("content", "")
                text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
                if role in ("user", "assistant") and text:
                    store.event(session["id"], role, {"text": text, "provider": "claude"})
            return session
        result = await local.rpc(account).call("thread/fork", {"threadId": external_id})
        thread = result["thread"]
        session = store.resolve(0, 0, 0, thread.get("name") or "Imported Codex fork", new=True)
        session = store.update_session(session["id"], provider="codex", account_id=aid, external_id=thread["id"], workspace=thread.get("cwd"))
        for turn in thread.get("turns", []):
            for item in turn.get("items", []):
                if item["type"] == "agentMessage":
                    store.event(session["id"], "assistant", {"text": item["text"], "provider": "codex"})
        return session

    @app.get("/api/tools", dependencies=[Depends(require_admin)])
    async def tool_catalogue():
        return shared.catalogue()

    @app.post("/api/sessions/{sid}/tools/{name}", dependencies=[Depends(require_admin)])
    async def run_tool(sid: str, name: str, body: ToolBody):
        session = require_session(sid)
        return await shared.run(session, name, body.args)

    @app.get("/api/sessions/{sid}/subagents", dependencies=[Depends(require_admin)])
    async def subagents_snapshot(sid: str):
        require_session(sid)
        return subagents.snapshot(sid)

    @app.post("/api/sessions/{sid}/subagents", dependencies=[Depends(require_admin)])
    async def subagents_spawn(sid: str, body: SubAgentsBody):
        session = require_session(sid)
        if not subagents.enabled():
            raise HTTPException(409, "Subagents are disabled in settings")
        return await subagents.spawn(session, [t.model_dump(exclude_none=True) for t in body.tasks],
                                     body.shared_context)

    @app.post("/api/sessions/{sid}/subagents/cancel", dependencies=[Depends(require_admin)])
    async def subagents_cancel(sid: str):
        require_session(sid)
        return subagents.cancel(sid)

    @app.get("/api/entities", dependencies=[Depends(require_admin)])
    async def list_entities():
        """The API entities the chat can attach: each one names the endpoint that does its work."""
        return {"entities": entities()}

    @app.get("/api/skills/status", dependencies=[Depends(require_admin)])
    async def skills_status():
        return skills.status()

    @app.get("/api/skills/tags", dependencies=[Depends(require_admin)])
    async def skills_tags(limit: int = 30):
        return skills.tag_cloud(min(max(limit, 1), 100))

    @app.post("/api/skills/rescan", dependencies=[Depends(require_admin)])
    async def skills_rescan():
        return await asyncio.to_thread(skills.scan)

    @app.get("/api/skills", dependencies=[Depends(require_admin)])
    async def skills_search(query: str = "", tags: str = "", source: str = "",
                            sort: str = "relevant", limit: int = 40):
        selected = [t for t in tags.split(",") if t.strip()]
        return skills.search(query[:200], selected, source, sort, min(max(limit, 1), 200))

    @app.get("/api/skills/{skill_id}", dependencies=[Depends(require_admin)])
    async def skill_detail(skill_id: str):
        return skills.get(skill_id)

    @app.post("/api/skills/{skill_id}/attach", dependencies=[Depends(require_admin)])
    async def skill_attach(skill_id: str, body: SkillAttachBody):
        require_session(body.session_id)
        return skills.attach(skill_id, body.session_id)

    @app.get("/api/questions", dependencies=[Depends(require_admin)])
    async def questions():
        return [{"id": qid, "sid": q["session"]["id"], "questions": q["questions"]} for qid, q in agent.questions.items()]

    @app.post("/api/questions/{qid}", dependencies=[Depends(require_admin)])
    async def answer(qid: str, body: AnswerBody):
        item = agent.questions.get(qid)
        if item:
            agent.note_activity(item["session"]["id"])
        agent.answer(qid, body.answers, admin=True)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/steer", dependencies=[Depends(require_admin)])
    async def steer(sid: str, body: ChatBody):
        return await local.steer(require_session(sid), body.text)

    @app.post("/api/sessions/{sid}/topics", dependencies=[Depends(require_admin)])
    async def new_topic(sid: str, body: SessionBody):
        original = require_session(sid)
        if not original["chat_id"]:
            raise HTTPException(409, "Select a Telegram session first")
        created = await telegram.call("createForumTopic", {"chat_id": original["chat_id"], "name": body.title})
        session = store.resolve(original["chat_id"], created["message_thread_id"], original["user_id"], body.title, new=True)
        account = store.account(body.account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        project = store.rows("SELECT * FROM projects WHERE id=?", (body.project_id,)) if body.project_id else []
        session = store.update_session(session["id"], provider=account["provider"], account_id=account["id"], model=body.model,
            effort=body.effort, project_id=body.project_id, workspace=project[0]["path"] if project else None)
        await telegram.text(session, f"AIGent · {body.title}\nСессия {session['id']} готова. Отправьте задачу.")
        return session

    @app.get("/api/sessions/{sid}/events", dependencies=[Depends(require_admin)])
    async def events(sid: str, after: int = 0):
        require_session(sid)
        return store.events(sid, after)

    @app.get("/api/sessions/{sid}/stream", dependencies=[Depends(require_admin)])
    async def event_stream(sid: str, request: Request, after: int = 0):
        require_session(sid)
        async def generate():
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            while not await request.is_disconnected():
                found = store.events(sid, cursor)
                for event in found:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: message\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not found:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/sessions/{sid}/messages", status_code=202, dependencies=[Depends(require_admin)])
    async def send(sid: str, body: ChatBody):
        selected = require_session(sid)
        digest = hashlib.sha256(body.text.encode()).hexdigest()
        if body.request_id:
            previous = store.rows("SELECT text_hash FROM message_requests WHERE session_id=? AND request_id=?", (sid, body.request_id))
            if previous:
                if previous[0]["text_hash"] != digest:
                    raise HTTPException(409, "Request id belongs to a different message")
                return {"accepted": True, "duplicate": True}
        content = body.text
        if body.attachments:
            parts = [{"type": "text", "text": body.text}]
            for relative in body.attachments:
                piece = agent.attachment_content(safe_path(agent.workspace(sid), relative))
                parts.extend(piece if isinstance(piece, list) else [{"type": "text", "text": piece}])
            content = parts
        selected = await sync.ensure_topic(selected)
        await sync.mirror_user(selected, body.text, body.attachments)
        result = agent.submit(selected, content, queue=body.queue)
        if body.request_id:
            store.execute("INSERT INTO message_requests(session_id,request_id,text_hash,created) VALUES (?,?,?,?)", (sid, body.request_id, digest, time.time()))
        return result

    @app.get("/api/sessions/{sid}/context", dependencies=[Depends(require_admin)])
    async def context_usage(sid: str):
        require_session(sid)
        return agent.context_size(sid)

    @app.get("/api/sessions/{sid}/goal", dependencies=[Depends(require_admin)])
    async def goal_state(sid: str):
        require_session(sid)
        return agent.goal(sid) or agent.blank_goal(sid)

    @app.post("/api/sessions/{sid}/goal", dependencies=[Depends(require_admin)])
    async def goal_update(sid: str, body: GoalBody):
        require_session(sid)
        fields = body.model_dump(exclude_none=True)
        if body.status is not None and body.status not in GOAL_STATUSES:
            raise HTTPException(422, "Unknown goal status")
        if body.steps is not None:
            fields["steps"] = [step.model_dump() for step in body.steps]
        return agent.save_goal(sid, source="user", **fields)

    @app.get("/api/sessions/{sid}/async-questions", dependencies=[Depends(require_admin)])
    async def async_questions(sid: str):
        require_session(sid)
        return [{k: row[k] for k in ("id", "question", "assumption", "created")}
                for row in store.open_questions(sid)]

    @app.post("/api/sessions/{sid}/async-questions/{qid}", dependencies=[Depends(require_admin)])
    async def answer_async_question(sid: str, qid: str, body: AsyncAnswer):
        session = require_session(sid)
        agent.note_activity(sid)
        answer = body.answer.strip()
        if not store.close_question(qid, "answered" if answer else "dismissed", answer or None):
            return {"closed": False, "note": "Вопрос уже закрыт"}
        store.event(sid, "background_answered", {"id": qid, "reason": "answered" if answer else "dismissed"})
        if answer:
            return agent.submit(session, f"Ответ на вопрос агента: {answer}") | {"closed": True}
        return {"closed": True, "queued": False}

    @app.get("/api/sessions/{sid}/queue", dependencies=[Depends(require_admin)])
    async def queue_list(sid: str):
        require_session(sid)
        return [{"id": item["id"], "created": item["created"],
                 "text": agent.plain_text(item["payload"])} for item in store.queued(sid)]

    @app.delete("/api/sessions/{sid}/queue", dependencies=[Depends(require_admin)])
    async def queue_clear(sid: str, item: int | None = None):
        require_session(sid)
        removed = store.drop_queued(sid, item)
        if removed:
            store.event(sid, "notice", {"text": f"Из очереди удалено сообщений: {removed}"})
        return {"removed": removed}

    @app.post("/api/sessions/{sid}/stop", dependencies=[Depends(require_admin)])
    async def stop(sid: str):
        require_session(sid)
        agent.note_activity(sid)
        return {"stopped": agent.stop(sid)}

    @app.post("/api/approvals/{aid}", dependencies=[Depends(require_admin)])
    async def decision(aid: str, body: DecisionBody):
        item = agent.approvals.get(aid)
        if item:
            agent.note_activity(item["session"]["id"])
        agent.decide(aid, body.accepted, admin=True)
        return {"ok": True}

    @app.get("/api/approvals", dependencies=[Depends(require_admin)])
    async def approvals():
        return [{"id": aid, "sid": a["session"]["id"], "name": a["name"], "detail": a["detail"],
                 "admin_only": a["admin_only"]} for aid, a in agent.approvals.items()]

    @app.get("/api/sessions/{sid}/usage", dependencies=[Depends(require_admin)])
    async def usage(sid: str):
        require_session(sid)
        points = store.rows("SELECT id,payload,created FROM usage WHERE session_id=? ORDER BY id", (sid,))
        return {"totals": store.usage(sid), "points": [{"id": r["id"], "created": r["created"], **json.loads(r["payload"])} for r in points]}

    @app.get("/api/sessions/{sid}/map", dependencies=[Depends(require_admin)])
    async def project_map_state(sid: str):
        """Latest generated map, job history and the memory text. Reading costs nothing."""
        require_session(sid)
        return await asyncio.to_thread(project_map.state, sid)

    @app.get("/api/sessions/{sid}/map/estimate", dependencies=[Depends(require_admin)])
    async def project_map_estimate(sid: str):
        """Fresh local inventory plus the token/cost estimate. No provider request is made."""
        require_session(sid)
        inventory = await asyncio.to_thread(project_map.inventory, sid)
        estimate = project_map.estimate(sid, inventory)
        summary = {k: v for k, v in inventory.items() if k != "records"}
        return {"inventory": summary, "estimate": estimate, "files": inventory["records"][:2000],
                "token_test": "Оценка локальная и бесплатная. Кнопка «Проверить на 3 файлах» отправляет "
                              "один реальный запрос DeepSeek и сохраняет калибровку факт/оценка."}

    @app.post("/api/sessions/{sid}/map/scan", dependencies=[Depends(require_admin)])
    async def project_map_scan(sid: str, body: MapScanBody):
        require_session(sid)
        inventory = await asyncio.to_thread(project_map.inventory, sid)
        return project_map.start(sid, body.mode, body.max_cost_usd, inventory)

    @app.post("/api/sessions/{sid}/map/probe", dependencies=[Depends(require_admin)])
    async def project_map_probe(sid: str):
        """The careful paid test: one small request, actual usage versus estimate."""
        require_session(sid)
        return await project_map.probe(sid)

    @app.get("/api/sessions/{sid}/map/jobs", dependencies=[Depends(require_admin)])
    async def project_map_jobs(sid: str):
        require_session(sid)
        return project_map.jobs(sid)

    @app.post("/api/sessions/{sid}/map/jobs/{job_id}/{action}", dependencies=[Depends(require_admin)])
    async def project_map_control(sid: str, job_id: str, action: str):
        require_session(sid)
        if action not in ("pause", "resume", "cancel"):
            raise HTTPException(404, "Unknown map job action")
        job = project_map.job(job_id)
        if not job or job["session_id"] != sid:
            raise HTTPException(404, "Map job not found")
        return getattr(project_map, action)(job_id)

    @app.get("/api/sessions/{sid}/files", dependencies=[Depends(require_admin)])
    async def files(sid: str):
        return workspace_service.files(sid)

    @app.get("/api/sessions/{sid}/editor", dependencies=[Depends(require_admin)])
    async def read_editor(sid: str, path: str):
        require_session(sid)
        return workspace_service.read(sid, path)

    @app.put("/api/sessions/{sid}/editor", dependencies=[Depends(require_admin)])
    async def save_editor(sid: str, body: FileBody):
        require_session(sid)
        return workspace_service.save(sid, body.path, body.text, body.revision)

    @app.get("/api/sessions/{sid}/git", dependencies=[Depends(require_admin)])
    async def git_status(sid: str):
        require_session(sid)
        return await workspace_service.git_status(sid)

    @app.get("/api/sessions/{sid}/git/diff", dependencies=[Depends(require_admin)])
    async def git_diff(sid: str, path: str = "", staged: bool = False):
        require_session(sid)
        if path:
            safe_path(agent.workspace(sid), path)
        args = ["diff"] + (["--cached"] if staged else []) + ["--"] + ([path] if path else [])
        return {"diff": await workspace_service.git(sid, args)}

    @app.post("/api/sessions/{sid}/git", dependencies=[Depends(require_admin)])
    async def git_action(sid: str, body: GitBody):
        require_session(sid)
        for path in body.paths:
            safe_path(agent.workspace(sid), path)
        if body.action == "stage" and body.paths:
            args = ["add", "--", *body.paths]
        elif body.action == "unstage" and body.paths:
            args = ["restore", "--staged", "--", *body.paths]
        elif body.action == "commit" and body.message.strip():
            args = ["commit", "-m", body.message]
        else:
            raise HTTPException(400, "Select stage, unstage or commit with the required fields")
        result = await workspace_service.git(sid, args)
        store.event(sid, "git", {"action": body.action, "text": result})
        return {"result": result}

    @app.post("/api/sessions/{sid}/terminal", dependencies=[Depends(require_admin)])
    async def terminal_start(sid: str, body: CommandBody):
        tid = await workspace_service.start_command(require_session(sid), body.command, body.workdir)
        return {"id": tid}

    @app.post("/api/sessions/{sid}/terminal/{tid}/stdin", dependencies=[Depends(require_admin)])
    async def terminal_stdin(sid: str, tid: str, body: StdinBody):
        item = workspace_service.command(sid, tid)
        if item["process"].returncode is not None:
            raise HTTPException(409, "Process has exited")
        item["process"].stdin.write(body.text.encode())
        await item["process"].stdin.drain()
        return {"ok": True}

    @app.post("/api/sessions/{sid}/terminal/{tid}/stop", dependencies=[Depends(require_admin)])
    async def terminal_stop(sid: str, tid: str):
        await workspace_service.stop_command(sid, tid)
        return {"ok": True}

    @app.get("/api/sessions/{sid}/file", dependencies=[Depends(require_admin)])
    async def get_file(sid: str, path: str):
        file = safe_path(agent.workspace(sid), path)
        if not file.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(file, filename=file.name, media_type="application/octet-stream")

    @app.post("/api/sessions/{sid}/files", dependencies=[Depends(require_admin)])
    async def upload(sid: str, file: UploadFile = File(...), kind: str = Form("document"),
                     caption: str = Form(""), send_telegram: bool = Form(False), ask_agent: bool = Form(False)):
        session = await sync.ensure_topic(require_session(sid))
        if kind not in ("document", "photo", "audio", "voice", "video", "video_note", "animation", "sticker"):
            raise HTTPException(422, "Unknown media kind")
        name = Path(file.filename or "attachment.bin").name.replace("\\", "_").replace(":", "_")
        target = safe_path(agent.workspace(sid), secrets.token_hex(4) + "-" + name[:100])
        size = 0
        try:
            with target.open("wb") as output:
                while chunk := await file.read(65536):
                    size += len(chunk)
                    if size > 50 * 1024 * 1024:
                        raise HTTPException(413, "Maximum upload is 50 MB")
                    output.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        delivered = False
        if send_telegram:
            if not session["chat_id"]:
                raise HTTPException(409, "File saved; this web session has no Telegram chat")
            # Index the upload before the media event so the mirror does not send it twice.
            sync.record_result(sid, target, kind, await telegram.media(session, target, kind, caption))
            delivered = True
        store.event(sid, "media", {"path": target.name, "kind": kind, "direction": "web"})
        if target.suffix.lower() in (".glb", ".gltf"):
            # An uploaded model gets its provenance sidecar immediately, with the upload as its source.
            try:
                await asyncio.to_thread(models3d.register, sid, target.name,
                                        {"source": {"tool": "upload", "provider": "web"},
                                         "prompt": caption[:2000]})
            except (ModelError, OSError, ValueError):
                pass
        if ask_agent:
            agent.start(session, agent.attachment_content(target, caption))
        return {"path": target.name, "bytes": size, "telegram_delivered": delivered}

    @app.post("/api/sessions/{sid}/telegram", dependencies=[Depends(require_admin)])
    async def structured_send(sid: str, body: TelegramBody):
        session = require_session(sid)
        if not session["chat_id"]:
            raise HTTPException(409, "This session has no Telegram chat")
        methods = {"location": "sendLocation", "venue": "sendVenue", "contact": "sendContact",
                   "poll": "sendPoll", "dice": "sendDice", "text": "sendMessage"}
        if body.kind not in methods:
            raise HTTPException(422, "Use the files endpoint for binary media")
        payload = {k: v for k, v in body.payload.items() if k not in ("chat_id", "message_thread_id", "business_connection_id")}
        result = await telegram.call(methods[body.kind], payload | telegram.route(session))
        store.event(sid, "telegram_sent", {"kind": body.kind, "message_id": result["message_id"]})
        return result

    @app.get("/v1/models", dependencies=[Depends(require_admin)])
    async def models():
        response = await client.get("https://api.deepseek.com/models", headers={"Authorization": "Bearer " + config["deepseek_key"]})
        return Response(response.content, response.status_code, media_type="application/json")

    @app.post("/v1/chat/completions", dependencies=[Depends(require_admin)])
    async def completions(body: CompletionBody, request: Request):
        payload = body.model_dump(exclude_unset=True)
        sid = request.headers.get("x-session-id")
        if sid:
            require_session(sid)
        else:
            sid = store.resolve(0, -1, 0, "OpenAI-compatible API")["id"]
        upstream_request = client.build_request("POST", "https://api.deepseek.com/chat/completions", json=payload,
                                                headers={"Authorization": "Bearer " + config["deepseek_key"]}, timeout=180)
        upstream = await client.send(upstream_request, stream=True)
        if not body.stream or upstream.status_code != 200:
            data = await upstream.aread()
            await upstream.aclose()
            if upstream.status_code == 200:
                result = json.loads(data)
                if result.get("usage"):
                    store.add_usage(sid, account_usage(result["usage"], body.model, config))
            return Response(data, upstream.status_code, media_type="application/json")
        async def forward():
            try:
                async for line in upstream.aiter_lines():
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        try:
                            item = json.loads(line[5:])
                            if item.get("usage"):
                                store.add_usage(sid, account_usage(item["usage"], body.model, config))
                        except ValueError:
                            pass
                    yield line + "\n"
            finally:
                await upstream.aclose()
        return StreamingResponse(forward(), media_type="text/event-stream", headers={"X-Session-ID": sid})

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static / "index.html")

    return app
