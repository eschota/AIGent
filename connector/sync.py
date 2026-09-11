"""Two-way mirror between IDE sessions and Telegram forum topics.

Every session created in the IDE gets its own topic in one configured supergroup, so the same
conversation can be continued from a phone and from the desktop. Media produced or received by a
session is uploaded to that topic and kept in Telegram; the local copy is a cache that may be
evicted once a `file_id` is recorded.

Telegram's Bot API cannot list the history of a topic, so `media_files` in the local sqlite
database is the only index of those `file_id` values. Back it up together with the workspaces.
"""

import asyncio
import hashlib
import mimetypes
import time
from pathlib import Path

from .agent import safe_path
from .providers import ProviderError

# Telegram's hard limit is 4096 UTF-16 code units; leave room for continuation markers.
CHUNK = 3500
# Kinds mirrored from the event journal. Everything else stays in the IDE journal only.
MIRRORED = ("assistant", "error")
METHODS = {"photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio", "voice": "sendVoice",
           "animation": "sendAnimation", "video_note": "sendVideoNote", "sticker": "sendSticker",
           "document": "sendDocument"}


def split_text(text, limit=CHUNK):
    """Split a long answer on line boundaries and number the parts."""
    text = text or ""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(text)
    if len(parts) == 1:
        return parts
    return [f"{part}\n… ({index}/{len(parts)})" for index, part in enumerate(parts, 1)]


def file_reference(result):
    """Pull (file_id, file_unique_id, size) out of any sendX result."""
    for key in ("document", "video", "audio", "voice", "animation", "video_note", "sticker", "photo"):
        value = (result or {}).get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = max(value, key=lambda v: v.get("file_size", 0))
        return value.get("file_id"), value.get("file_unique_id"), value.get("file_size", 0)
    return None, None, 0


def media_kind(kind, path: Path):
    if kind in METHODS:
        return kind
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime.startswith("image/") and not mime.endswith("svg+xml"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def digest_file(path: Path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


class SessionSync:
    """Mirrors sessions into topics, uploads media and keeps the mirror pointer per session."""

    UPLOAD_LIMIT = 50 * 1024 * 1024

    def __init__(self, config, store, telegram, agent):
        self.config, self.store, self.telegram, self.agent = config, store, telegram, agent
        self.queue = asyncio.Queue(maxsize=500)
        self.locks, self.last_call = {}, {}
        self.worker = None
        self.tools, self.inflight, self.reported = {}, {}, set()
        self.interval = 1.0  # at most one Telegram request per second and per chat

    # ------------------------------------------------------------------ configuration
    @property
    def chat_id(self):
        try:
            return int(str(self.config["telegram_sync_chat_id"] or "").strip() or 0)
        except ValueError:
            return 0

    @property
    def enabled(self):
        return bool(self.chat_id and self.config["telegram_sync"] and self.config["telegram_token"])

    def handles(self, session, kind=None):
        """True when this session's text output is delivered by the mirror instead of Agent.tell."""
        return bool(self.enabled and session and session.get("chat_id") == self.chat_id
                    and (kind is None or kind in MIRRORED))

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.pump())
        if self.observe not in self.store.observers:
            self.store.observers.append(self.observe)
        return self.worker

    async def close(self):
        if self.observe in self.store.observers:
            self.store.observers.remove(self.observe)
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    async def drain(self):
        """Wait for every queued mirror job. Used by the API backfill and by tests."""
        await self.queue.join()

    async def pump(self):
        while True:
            item = await self.queue.get()
            try:
                await self.handle(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.fail(item.get("sid"), exc)
            finally:
                if item.get("job") == "media":
                    self.inflight[item["sid"]] = max(0, self.inflight.get(item["sid"], 1) - 1)
                self.queue.task_done()

    def fail(self, sid, exc):
        """A broken mirror is reported once per session, never once per message."""
        if sid in self.reported:
            return
        self.reported.add(sid)
        self.store.event(sid, "notice", {"text": "Зеркало Telegram недоступно: "
                                                 + self.config.redact(exc) + " Работа в IDE продолжается."})

    # ------------------------------------------------------------------ transport
    async def guard(self, chat, factory):
        """Sequential per chat, with 429 flood-control retries."""
        lock = self.locks.setdefault(chat, asyncio.Lock())
        async with lock:
            for attempt in range(4):
                wait = self.interval - (time.monotonic() - self.last_call.get(chat, -self.interval))
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last_call[chat] = time.monotonic()
                try:
                    return await factory()
                except ProviderError as exc:
                    after = getattr(exc, "retry_after", 0)
                    if attempt == 3 or not (after or getattr(exc, "retryable", False)):
                        raise
                    await asyncio.sleep(after or 2 ** attempt)

    async def call(self, chat, method, payload, files=None):
        return await self.guard(chat, lambda: self.telegram.call(method, payload, files))

    def route(self, session):
        return {"chat_id": session["chat_id"]} | ({"message_thread_id": session["topic_id"]}
                                                  if session["topic_id"] else {})

    async def post(self, session, text, **extra):
        result = None
        for part in split_text(text):
            if part.strip():
                result = await self.call(session["chat_id"], "sendMessage",
                                         self.route(session) | {"text": part} | extra)
        return result

    # ------------------------------------------------------------------ topics
    async def ensure_topic(self, session, title=None):
        """Create (or lazily retry) the forum topic that mirrors this session."""
        if not self.enabled or not session:
            return session
        sid = session["id"]
        state = self.store.sync_state(sid)
        retry = bool(state and state["status"] == "fallback" and not session["topic_id"])
        if session.get("chat_id") and not retry:
            return session
        if session.get("chat_id") and session["chat_id"] != self.chat_id:
            return session
        chat = self.chat_id
        name = (title or session.get("title") or "AIGent")[:128]
        topic, status = 0, "ready"
        try:
            created = await self.call(chat, "createForumTopic", {"chat_id": chat, "name": name})
            topic = created["message_thread_id"]
        except ProviderError as exc:
            status = "fallback"
            self.store.event(sid, "notice", {"text": "Telegram не создал топик (" + self.config.redact(exc)
                                             + "). Сессия зеркалится в общий топик; попробую снова "
                                               "при следующем сообщении. Включите Topic Mode и права бота."})
        self.store.execute("UPDATE sessions SET chat_id=?,topic_id=? WHERE id=?", (chat, topic, sid))
        session = self.store.session(sid)
        last = self.store.rows("SELECT MAX(id) AS last FROM events WHERE session_id=?", (sid,))[0]["last"]
        self.store.set_sync_state(sid, chat_id=chat, topic_id=topic, status=status,
                                  last_event_id=(state or {}).get("last_event_id") or last or 0)
        if not (state or {}).get("header_message_id"):
            await self.header(session)
        return session

    async def header(self, session):
        provider = session.get("provider") or "deepseek"
        model = session.get("model") or "по умолчанию"
        workspace = session.get("workspace") or "отдельная папка сессии"
        text = (f"🧩 AIGent · {session['title']}\nСессия: {session['id']}\n"
                f"Провайдер: {provider} · модель: {model}\nПроект: {workspace}\n"
                "Пишите сюда — сообщения попадут в эту же сессию IDE. /status, /stop, /new.")
        try:
            result = await self.post(session, text)
        except ProviderError as exc:
            self.fail(session["id"], exc)
            return None
        if result:
            self.store.set_sync_state(session["id"], header_message_id=result["message_id"])
        return result

    async def rename(self, session, title):
        if not self.handles(session) or not session["topic_id"]:
            return False
        await self.call(session["chat_id"], "editForumTopic",
                        {"chat_id": session["chat_id"], "message_thread_id": session["topic_id"],
                         "name": title[:128]})
        return True

    async def closed(self, session):
        """A deleted session closes its topic; the topic and its media are never deleted."""
        if not self.handles(session) or not session["topic_id"]:
            return False
        try:
            await self.post(session, "🗃 Сессия удалена в IDE. Топик закрыт; файлы и история сохранены.")
            await self.call(session["chat_id"], "closeForumTopic",
                            {"chat_id": session["chat_id"], "message_thread_id": session["topic_id"]})
        except ProviderError as exc:
            self.fail(session["id"], exc)
            return False
        return True

    async def backfill(self, limit=300):
        """Give every non-deleted session without a topic one. Idempotent and resumable."""
        if not self.enabled:
            return {"enabled": False, "created": 0, "skipped": 0}
        created, skipped = 0, 0
        for row in self.store.sessions() + self.store.sessions(archived=True):
            if created >= limit:
                break
            if row.get("chat_id"):
                skipped += 1
                continue
            try:
                await self.ensure_topic(row)
                created += 1
            except ProviderError as exc:
                self.fail(row["id"], exc)
                break
        return {"enabled": True, "created": created, "skipped": skipped}

    # ------------------------------------------------------------------ mirroring
    def push(self, item):
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.fail(item.get("sid"), ValueError("очередь зеркала переполнена"))
            return False
        return True

    def observe(self, sid, kind, payload, event_id):
        """Store observer: called synchronously inside store.event(); never blocks the agent."""
        if not sid or not self.enabled:
            return
        if kind == "tool":
            self.tools.setdefault(sid, {})
            name = payload.get("name") or "tool"
            self.tools[sid][name] = self.tools[sid].get(name, 0) + 1
            return
        if kind not in MIRRORED and kind not in ("media", "turn_completed"):
            return
        session = self.store.session(sid)
        if not session or session.get("chat_id") != self.chat_id:
            return
        if kind == "media":
            self.inflight[sid] = self.inflight.get(sid, 0) + 1
            if not self.push({"job": "media", "sid": sid, "payload": payload, "event_id": event_id}):
                self.inflight[sid] = max(0, self.inflight[sid] - 1)
            return
        if kind == "turn_completed":
            self.push({"job": "tools", "sid": sid, "event_id": event_id})
            return
        self.push({"job": "event", "sid": sid, "kind": kind, "payload": payload, "event_id": event_id})

    def seen(self, sid, event_id):
        """Dedup across restarts: an event id at or below the pointer was already mirrored."""
        state = self.store.sync_state(sid)
        return bool(state and event_id and event_id <= (state["last_event_id"] or 0))

    def mark(self, sid, event_id):
        state = self.store.sync_state(sid)
        if event_id and event_id > ((state or {}).get("last_event_id") or 0):
            self.store.set_sync_state(sid, last_event_id=event_id)

    async def handle(self, item):
        sid = item["sid"]
        session = self.store.session(sid)
        if not session or session.get("chat_id") != self.chat_id:
            return
        if item["job"] == "media":
            await self.upload(session, item["payload"], item["event_id"])
            return
        if self.seen(sid, item.get("event_id")):
            return
        if item["job"] == "tools":
            await self.tool_summary(session)
        elif item["job"] == "event":
            prefix = "⚠️ " if item["kind"] == "error" else ""
            text = (item["payload"] or {}).get("text") or ""
            if text.strip():
                await self.post(session, prefix + text)
        elif item["job"] == "text":
            await self.post(session, item["text"])
        self.mark(sid, item.get("event_id"))

    async def tool_summary(self, session):
        """One compact message per turn instead of an event per tool call."""
        used = self.tools.pop(session["id"], None)
        if not used:
            return
        parts = [name + (f" ×{count}" if count > 1 else "") for name, count in used.items()]
        await self.post(session, "🛠 Инструменты за ход: " + ", ".join(parts))

    async def mirror_user(self, session, text, attachments=()):
        """Explicit hook for a message sent from the IDE; Telegram-sent text is already visible."""
        if not self.handles(session) or not (text or attachments):
            return False
        body = "👤 " + (text or "")
        if attachments:
            body += "\n📎 " + ", ".join(str(a) for a in attachments)
        self.push({"job": "text", "sid": session["id"], "text": body})
        return True

    # ------------------------------------------------------------------ media
    async def upload(self, session, payload, event_id):
        sid = session["id"]
        name = payload.get("path")
        if not name:
            return
        if payload.get("direction") == "in":
            # Already stored in Telegram: record the reference the bot downloaded it from.
            if payload.get("file_id"):
                self.store.record_media(sid, name, file_id=payload["file_id"], kind=payload.get("kind"))
            self.mark(sid, event_id)
            return
        existing = self.store.media_file(sid, name)
        if existing and existing["file_id"]:
            self.mark(sid, event_id)
            return
        try:
            path = safe_path(self.agent.workspace(sid), name)
        except ValueError:
            return  # Private paths (.local, credentials, anything outside the workspace) are never sent.
        if not path.is_file():
            return
        size = path.stat().st_size
        if size > self.UPLOAD_LIMIT:
            self.store.event(sid, "notice", {"text": f"Файл {name} ({size // 1024 // 1024} MB) больше лимита "
                                                     "Bot API 50 MB: хранится только локально."})
            self.mark(sid, event_id)
            return
        kind = media_kind(payload.get("kind"), path)
        result = await self.guard(session["chat_id"],
                                  lambda: self.telegram.media(session, path, kind, payload.get("caption", "")))
        file_id, unique, reported = file_reference(result)
        self.store.record_media(sid, name, file_id=file_id, file_unique_id=unique, kind=kind,
                                size=size or reported, message_id=(result or {}).get("message_id"),
                                sha256=digest_file(path))
        self.store.event(sid, "media_synced", {"path": name, "kind": kind, "file_id": file_id, "size": size})
        self.mark(sid, event_id)
        if file_id and self.config["telegram_media_offload"] and size > self.offload_bytes:
            self.evict(sid, name)

    def record_result(self, sid, path: Path, kind, result):
        """Index a file this process already sent itself, so the mirror does not upload it twice."""
        file_id, unique, reported = file_reference(result)
        return self.store.record_media(sid, path.name, file_id=file_id, file_unique_id=unique, kind=kind,
                                       size=(path.stat().st_size if path.is_file() else reported),
                                       message_id=(result or {}).get("message_id"))

    @property
    def offload_bytes(self):
        return max(1, int(self.config["telegram_media_offload_mb"])) * 1024 * 1024

    def evict(self, sid, name):
        """Free local space for a file that is verifiably stored in Telegram."""
        record = self.store.media_file(sid, name)
        if not record or not record["file_id"]:
            raise ValueError("Локальная копия удаляется только после подтверждённой загрузки в Telegram.")
        path = safe_path(self.agent.workspace(sid), name)
        existed = path.is_file()
        if existed:
            path.unlink()
            self.store.event(sid, "notice", {"text": f"Локальная копия {name} освобождена; файл хранится "
                                                     "в Telegram и скачивается по запросу."})
        return {"path": name, "evicted": existed, "file_id": record["file_id"]}

    async def restore(self, sid, name):
        """Re-download a media file from Telegram into the workspace when the cache is empty."""
        path = safe_path(self.agent.workspace(sid), name)
        if path.is_file():
            return path
        record = self.store.media_file(sid, name)
        if not record or not record["file_id"]:
            raise ValueError("Файл не найден ни локально, ни в индексе Telegram.")
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.guard(self.chat_id or 0, lambda: self.telegram.download(record["file_id"], path))
        self.store.event(sid, "notice", {"text": f"Файл {name} восстановлен из Telegram."})
        return path

    # ------------------------------------------------------------------ reporting
    def topic_url(self, session):
        chat = session.get("chat_id") or 0
        if chat >= 0:
            return ""
        internal = str(chat).removeprefix("-100")
        return f"https://t.me/c/{internal}" + (f"/{session['topic_id']}" if session["topic_id"] else "")

    def info(self, session):
        sid = session["id"]
        state = self.store.sync_state(sid) or {}
        files = self.store.media_files(sid)
        return {"enabled": self.enabled, "chat_id": session.get("chat_id") or 0,
                "topic_id": session.get("topic_id") or 0, "topic_url": self.topic_url(session),
                "status": state.get("status") or ("external" if session.get("chat_id") else "off"),
                "mirrored": self.handles(session),
                "last_mirrored_event": state.get("last_event_id") or 0,
                "media_count": len(files), "media_in_telegram": sum(bool(f["file_id"]) for f in files),
                "pending_uploads": self.inflight.get(sid, 0)}

    async def catch_up(self, sid):
        """Mirror events recorded while the mirror was down, once, in order."""
        session = self.store.session(sid)
        if not self.handles(session):
            return 0
        state = self.store.sync_state(sid) or {}
        count = 0
        for event in self.store.events(sid, state.get("last_event_id") or 0):
            if event["kind"] in MIRRORED or event["kind"] in ("media", "turn_completed"):
                job = {"media": "media", "turn_completed": "tools"}.get(event["kind"], "event")
                self.push({"job": job, "sid": sid, "kind": event["kind"],
                           "payload": event["payload"], "event_id": event["id"]})
                count += 1
        return count
