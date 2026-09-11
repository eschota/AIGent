import asyncio
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .agent import Agent, safe_path
from .config import Config, password_hash, verify_password
from .providers import DeepSeek, ProviderError, TelegramAPI, account_usage
from .store import Store
from .telegram import Bot


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
    max_context_chars: int = Field(default=200000, ge=8000, le=2000000)
    max_steps: int = Field(default=12, ge=1, le=40)


class ChatBody(BaseModel):
    text: str = Field(min_length=1, max_length=40000)


class SessionBody(BaseModel):
    title: str = Field(default="Web session", min_length=1, max_length=100)


class DecisionBody(BaseModel):
    accepted: bool


class TelegramBody(BaseModel):
    kind: str
    payload: dict


class CompletionBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[dict]
    stream: bool = False


def create_app(root: Path | None = None, polling=True):
    config = Config(root or Path(__file__).resolve().parents[1] / ".local")
    store = Store(config.root / "sessions.sqlite3")
    client = httpx.AsyncClient()
    telegram = TelegramAPI(config, client)
    agent = Agent(config, store, DeepSeek(config, client), telegram)
    bot = Bot(config, store, telegram, agent)
    sessions, failures = {}, {}

    @asynccontextmanager
    async def lifespan(app):
        if polling:
            bot.task = asyncio.create_task(bot.poll())
        yield
        if bot.task:
            bot.task.cancel()
            await asyncio.gather(bot.task, return_exceptions=True)
        await agent.shutdown()
        await client.aclose()
        store.db.close()

    app = FastAPI(title="AIGent", version="0.1.0", lifespan=lifespan,
                  description="Local agent connector. Authenticate with the admin cookie or connector Bearer token.")
    app.state.config, app.state.store, app.state.agent, app.state.bot = config, store, agent, bot
    app.state.client = client

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
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    def bearer(request):
        return request.headers.get("authorization", "").removeprefix("Bearer ")

    def require_admin(request: Request, authorization: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False))):
        token = bearer(request)
        if token and hmac.compare_digest(token, config["connector_token"]):
            return True
        entry = sessions.get(request.cookies.get("ide_admin", ""))
        if entry and entry[0] > time.time() and entry[1] == config["admin_password"]:
            return True
        raise HTTPException(401, "Administrator authentication required")

    def require_session(sid):
        session = store.session(sid)
        if not session:
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
        return {"status": "ok", "configured": config.ready}

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
        token = secrets.token_urlsafe(32)
        sessions[token] = (time.time() + 43200, config["admin_password"])
        response.set_cookie("ide_admin", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=43200)
        return {"ok": True}

    @app.post("/api/logout", dependencies=[Depends(require_admin)])
    async def logout(request: Request, response: Response):
        sessions.pop(request.cookies.get("ide_admin", ""), None)
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

    @app.get("/api/status", dependencies=[Depends(require_admin)])
    async def status():
        return {"bot": bot.status, "username": bot.username, "error": bot.last_error,
                "usage": store.usage(), "sessions": len(store.sessions()),
                "running": len(agent.jobs), "model": config["model"]}

    @app.get("/api/balance", dependencies=[Depends(require_admin)])
    async def balance():
        try:
            return await bot.balance()
        except httpx.HTTPError:
            raise HTTPException(502, "DeepSeek balance network error") from None

    @app.get("/api/sessions", dependencies=[Depends(require_admin)])
    async def list_sessions():
        return [s | {"usage": store.usage(s["id"])} for s in store.sessions()]

    @app.post("/api/sessions", dependencies=[Depends(require_admin)])
    async def new_session(body: SessionBody):
        return store.resolve(0, 0, 0, body.title, new=True)

    @app.post("/api/sessions/{sid}/topics", dependencies=[Depends(require_admin)])
    async def new_topic(sid: str, body: SessionBody):
        original = require_session(sid)
        if not original["chat_id"]:
            raise HTTPException(409, "Select a Telegram session first")
        created = await telegram.call("createForumTopic", {"chat_id": original["chat_id"], "name": body.title})
        session = store.resolve(original["chat_id"], created["message_thread_id"], original["user_id"], body.title, new=True)
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
        agent.start(require_session(sid), body.text)
        return {"accepted": True}

    @app.post("/api/sessions/{sid}/stop", dependencies=[Depends(require_admin)])
    async def stop(sid: str):
        require_session(sid)
        return {"stopped": agent.stop(sid)}

    @app.post("/api/approvals/{aid}", dependencies=[Depends(require_admin)])
    async def decision(aid: str, body: DecisionBody):
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

    @app.get("/api/sessions/{sid}/files", dependencies=[Depends(require_admin)])
    async def files(sid: str):
        root = agent.workspace(sid)
        result = []
        for path in root.rglob("*"):
            if len(result) >= 500:
                break
            if path.is_file():
                try:
                    checked = safe_path(root, path.relative_to(root).as_posix())
                    result.append({"path": checked.relative_to(root).as_posix(), "size": checked.stat().st_size})
                except ValueError:
                    continue
        return result

    @app.get("/api/sessions/{sid}/file", dependencies=[Depends(require_admin)])
    async def get_file(sid: str, path: str):
        file = safe_path(agent.workspace(sid), path)
        if not file.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(file, filename=file.name, media_type="application/octet-stream")

    @app.post("/api/sessions/{sid}/files", dependencies=[Depends(require_admin)])
    async def upload(sid: str, file: UploadFile = File(...), kind: str = Form("document"),
                     caption: str = Form(""), send_telegram: bool = Form(False), ask_agent: bool = Form(False)):
        session = require_session(sid)
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
        store.event(sid, "media", {"path": target.name, "kind": kind, "direction": "web"})
        delivered = False
        if send_telegram:
            if not session["chat_id"]:
                raise HTTPException(409, "File saved; this web session has no Telegram chat")
            await telegram.media(session, target, kind, caption)
            delivered = True
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
