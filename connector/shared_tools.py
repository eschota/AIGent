"""Shared tools: one implementation that every chat can use, whatever agent runs it.

A shared tool is executed by the connector itself, not by the provider, so a Codex or
Claude session that cannot call AIGent's own tools still receives the result in its chat.
The same tools are offered to the DeepSeek agent through the extension protocol.

Farm renders are slow — a video can take twenty minutes or more — so a submitted task is
persisted and polling resumes after a restart instead of being lost.
"""

import asyncio
import json
import re
import secrets
import time
from urllib.parse import urlparse

from .agent import STRING, safe_path, tool
from .providers import ProviderError

FARM = "https://autorig.online"
# Verified against the live farm on 2026-09-12.
WORKFLOWS = {
    "fast": {"file": "", "title": "Быстрый (LTX-13B)", "seconds": 2.0, "render": "~30 с", "audio": False},
    "hq": {"file": "gen_animation_hq_by_url.json", "title": "HQ (LTX-2 19B, камера + звук)",
           "seconds": 4.0, "render": "~9 мин", "audio": True},
}
# Some render nodes return the clip as a temporary preview, which the farm's own quality gate
# drops as `real_output_artifact_missing`. The task is cheap to resubmit and usually lands on a
# node that saves a real artifact, so a rejection is retried instead of failing the chat.
RETRYABLE = ("real_output_artifact_missing", "artifact", "no non-temporary")
ATTEMPTS = 3
RETRY_SECONDS = 5
TIMEOUTS = {"image": 1800, "video": 3600}
FPS = 25
MAX_FRAMES = 300  # RenderPrompt.frame_count is clamped to 300 by the farm API
VIDEO_SIZE_STEP = 32  # routing.clamp_video_dims: 64..512 in multiples of 32
DEFAULT_VIDEO_SIZE = "512x256"  # 16:8 — the frame ratio the owner wants for clips
DEFAULT_VIDEO_FRAMES = 121  # 5 s at FPS — the clip length the owner wants
POLL_SECONDS = 10


class SharedTools:
    # One chat can keep several renders in flight: images are quick, videos run for many minutes.
    SLOTS = 3
    def __init__(self, agent, client, skills=None):
        self.agent, self.client, self.skills = agent, client, skills
        self.jobs = {}

    # ------------------------------------------------------------------ catalogue
    def catalogue(self):
        return [
            {"name": "image", "title": "Изображение · бесплатная ферма",
             "description": "Генерация изображения на подключённой ферме AutoRig. "
                            "Результат приходит вложением в этот чат. Токены модели не расходуются.",
             "fields": [{"name": "prompt", "label": "Что сгенерировать", "type": "text", "required": True}],
             "billing": "free-farm", "background": True, "providers": "any"},
            {"name": "video", "title": "Видео из кадра · бесплатная ферма",
             "description": "Анимация изображения из рабочей папки (workflow gen_animation_by_url). "
                            "Рендер может идти 20+ минут: чат остаётся свободным, ожидание переживает перезапуск.",
             "fields": [{"name": "path", "label": "Файл изображения в рабочей папке", "type": "text", "required": True},
                        {"name": "prompt", "label": "Движение камеры и сцены", "type": "text", "required": False},
                        {"name": "frames", "label": "Кадров при 25 fps; по умолчанию 121 ≈ 5 с, максимум 241 ≈ 10 с", "type": "text", "required": False},
                        {"name": "size", "label": "Размер кадра; по умолчанию 512x256 (16:8), 64–512 с шагом 32", "type": "text", "required": False},
                        {"name": "quality", "label": "Качество: fast — 2 с за ~30 с; hq — 4 с со звуком за ~9 мин",
                         "type": "text", "required": False},
                        {"name": "work_flow", "label": "Имя воркфлоу вручную (переопределяет качество)", "type": "text", "required": False}],
             "billing": "free-farm", "background": True, "providers": "any"},
            {"name": "skill", "title": "Приложить скилл",
             "description": "Копирует найденный скилл из индекса Skill Manager в рабочую папку этого чата.",
             "fields": [{"name": "query", "label": "Название или тег скилла", "type": "text", "required": True}],
             "billing": "local", "background": False, "providers": "any"},
        ]

    # ------------------------------------------------------------------ DeepSeek extension protocol
    def tools(self, session):
        return [
            tool("shared_image", "Generate one image on the connected free farm and receive it in this chat. "
                                 "Works in any session and costs no model tokens.", {"prompt": STRING}, ["prompt"]),
            tool("shared_video", "Animate an existing workspace image on the connected free farm and receive the mp4 "
                                 "in this chat. quality=hq runs LTX-2 19B with the camera-control LoRA and an audio track "
                 "(about four seconds, ~9 minutes of render); quality=fast is the two-second default. "
                 "frames is the clip length at 25 fps (121 ~ 5 s, 241 ~ 10 s) and size is "
                 "WIDTHxHEIGHT up to 512x512, default 512x256 (16:8); a deployment whose template lacks the $frames placeholder "
                 "renders its built-in length instead. A render may take 20+ minutes; the tool waits and survives a restart. "
                                 "Never poll the farm with a shell command.",
                 {"path": STRING, "prompt": STRING, "work_flow": STRING, "size": STRING,
                  "quality": {"type": "string", "enum": ["fast", "hq"]},
                  "frames": {"type": "integer", "minimum": 9, "maximum": 300}}, ["path"]),
            tool("shared_skill", "Attach an indexed Markdown skill from Codex/Claude/project sessions to this chat.",
                 {"query": STRING}, ["query"]),
        ]

    async def execute(self, session, name, args):
        mapping = {"shared_image": "image", "shared_video": "video", "shared_skill": "skill"}
        return await self.run(session, mapping[name], args, wait=True)

    # ------------------------------------------------------------------ execution
    async def run(self, session, name, args, wait=False):
        if name == "skill":
            return self.attach_skill(session, str(args.get("query", "")))
        if name not in ("image", "video"):
            raise ValueError("Unknown shared tool")
        prompt = str(args.get("prompt", "")).strip()[:6000]
        if name == "image" and not prompt:
            raise ValueError("Опишите, что нужно сгенерировать")
        if name == "video" and not str(args.get("path", "")).strip():
            raise ValueError("Укажите путь к изображению в рабочей папке")
        sid = session["id"]
        if len(self.active(sid)) >= self.SLOTS:
            raise ValueError(f"В этом чате заняты все {self.SLOTS} слота фермы. Дождитесь результата.")
        quality = str(args.get("quality", "")).strip().lower()
        chosen = str(args.get("work_flow", "")).strip() or WORKFLOWS.get(quality, {}).get("file", "")
        extra = (chosen, args.get("frames") or 0, str(args.get("size", "")).strip())
        if wait:
            return await self.produce(session, name, prompt, str(args.get("path", "")), *extra)
        self.spawn(session, name, self.produce(session, name, prompt, str(args.get("path", "")), *extra))
        self.agent.store.event(sid, "tool", {"name": "shared_" + name,
                                             "arguments": {k: str(v)[:300] for k, v in args.items()},
                                             "call_id": "shared-" + secrets.token_hex(4)})
        return {"started": True, "tool": name, "billing": "free-farm",
                "note": "Результат придёт в этот чат вложением; чат остаётся свободным."}

    def active(self, sid):
        """Live farm tasks of this chat; finished ones are dropped from the table."""
        jobs = self.jobs.setdefault(sid, {})
        for token, task in list(jobs.items()):
            if task.done():
                jobs.pop(token, None)
        return list(jobs.values())

    def running(self, sid):
        return bool(self.active(sid))

    def spawn(self, session, name, runner):
        task = asyncio.create_task(self.background(session, name, runner))
        self.jobs.setdefault(session["id"], {})[secrets.token_hex(4)] = task
        return task

    async def background(self, session, name, runner):
        sid = session["id"]
        try:
            result = await runner
            self.agent.store.event(sid, "tool_result", {"name": "shared_" + name, "result": result})
        except (ValueError, ProviderError, OSError) as exc:
            self.forget(sid)
            self.agent.store.event(sid, "error", {"text": "Ферма: " + self.agent.config.redact(exc)})
        # A cancelled wait is left to propagate without a notice: cancellation happens on every
        # graceful shutdown/restart (the farm task keeps running and resume() picks it up again),
        # so logging "прервано" here fired on every restart cycle and spammed the event log.

    async def produce(self, session, kind, prompt, path="", work_flow="", frames=0, size=""):
        sid = session["id"]
        for attempt in range(1, ATTEMPTS + 1):
            job = await (self.submit_image(session, prompt) if kind == "image"
                         else self.submit_video(session, path, prompt, work_flow, frames, size))
            try:
                return await self.finish(session, job)
            except asyncio.CancelledError:  # a restart must keep the task resumable
                raise
            except ProviderError as exc:
                self.forget(sid, job)
                retryable = any(marker in str(exc).lower() for marker in RETRYABLE)
                if not retryable or attempt == ATTEMPTS:
                    raise
                self.agent.store.event(sid, "notice", {
                    "text": f"Узел фермы не сохранил результат ({attempt}/{ATTEMPTS - 1}); "
                            f"отправляю задачу заново — другой узел обычно отдаёт файл."})
                await asyncio.sleep(RETRY_SECONDS)
            except BaseException:
                self.forget(sid, job)
                raise

    # ------------------------------------------------------------------ submissions
    async def submit_image(self, session, prompt):
        sid = session["id"]
        submitted = await self.request("POST", "/renderfin/api-render", json={"prompt": prompt})
        job = self.job("image", submitted, prompt)
        self.remember(sid, job)
        self.agent.store.event(sid, "notice", {"text": f"Ферма · изображение · задача {job['task_id']}"})
        return job

    @staticmethod
    def video_frames(value):
        """LTXV wants 8*k+1 frames; the farm API clamps frame_count to 300 (12 s)."""
        count = int(value or 0) or DEFAULT_VIDEO_FRAMES
        count = max(9, min(MAX_FRAMES, count))
        return max(1, round((count - 1) / 8)) * 8 + 1

    @staticmethod
    def video_size(value):
        """Farm video dimensions: 64..512, multiples of 32; empty means the 16:8 default."""
        if not value:
            return 0, 0
        try:
            width, height = (int(v) for v in str(value).lower().split("x"))
        except ValueError:
            raise ValueError("Размер кадра задаётся как ШИРИНАxВЫСОТА, например 512x288") from None
        fit = lambda v: max(64, min(512, round(v / VIDEO_SIZE_STEP) * VIDEO_SIZE_STEP))  # noqa: E731
        return fit(width), fit(height)

    async def submit_video(self, session, path, prompt, work_flow="", frames=0, size=""):
        """Animate a reviewed workspace frame on the farm.

        `work_flow` selects one of the canonical animation workflows the farm advertises; empty
        uses the farm default. `frames` and `size` travel as frame_count / main_size_*, which the
        farm honours once its animation template carries the $frames, $width and $height
        placeholders; older deployments ignore them and render the length baked into the workflow.
        """
        sid = session["id"]
        source = safe_path(self.agent.workspace(sid), path)
        if not source.is_file():
            raise ValueError("Файл не найден в рабочей папке: " + path)
        payload, name = await asyncio.to_thread(self.prepare_frame, source)
        uploaded = await self.request("POST", "/dev/api/scratch", files={"file": (name, payload, "image/png")})
        image_url = uploaded.get("url", "")
        if not image_url.startswith(FARM + "/"):
            raise ProviderError("Ферма не вернула публичную ссылку на кадр")
        body = {"image_url": image_url, "prompt": prompt or "slow cinematic camera push in"}
        if work_flow:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", work_flow):
                raise ValueError("Недопустимое имя воркфлоу фермы")
            body["work_flow"] = work_flow
        count = self.video_frames(frames)
        if count:
            body["frame_count"] = count
        width, height = self.video_size(size or DEFAULT_VIDEO_SIZE)
        if width:
            body["main_size_width"], body["main_size_height"] = width, height
        submitted = await self.request("POST", "/renderfin/api-render", json=body)
        job = self.job("video", submitted, prompt, source=path, image_url=image_url,
                       work_flow=work_flow or "default", frames=count or None,
                       size=f"{width}x{height}" if width else None)
        if not job["output_url"].endswith(".mp4"):
            raise ProviderError("Ферма не приняла кадр в видео-воркфлоу: " + str(submitted)[:200])
        self.remember(sid, job)
        self.agent.store.event(sid, "notice", {"text": f"Ферма · видео · задача {job['task_id']} · кадр {path}"})
        return job

    @staticmethod
    def job(kind, submitted, prompt, **extra):
        task_id = str(submitted.get("task_id", ""))
        output_url = str(submitted.get("output_url", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", task_id) or not output_url.startswith(FARM + "/"):
            raise ProviderError("Ферма вернула неожиданный ответ: " + str(submitted)[:200])
        return {"kind": kind, "task_id": task_id, "output_url": output_url,
                "prompt": prompt[:300], "started": time.time(), **extra}

    # ------------------------------------------------------------------ waiting and delivery
    async def finish(self, session, job):
        sid, kind = session["id"], job["kind"]
        row = await self.poll(sid, job)
        if row is None:
            return {"task_id": job["task_id"], "pending": True, "output_url": job["output_url"],
                    "note": "Ферма ещё рендерит; задача не отменена, ожидание продолжится."}
        root = self.agent.workspace(sid)
        folder = safe_path(root, "shared")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (job["task_id"] + (".mp4" if kind == "video" else ".png"))
        await self.download(job["output_url"], target)
        self.forget(sid, job)
        relative = target.relative_to(root).as_posix()
        self.agent.store.event(sid, "media", {"path": relative, "kind": "video" if kind == "video" else "image",
                                              "direction": "generated", "prompt": job.get("prompt", "")[:300],
                                              "billing": "free-farm"})
        result = {"task_id": job["task_id"], "path": relative, "bytes": target.stat().st_size,
                  "billing": "free-farm", "seconds": round(time.time() - job.get("started", time.time()))}
        if kind == "video":
            result["source_frame"] = job.get("source")
        elif session.get("provider", "deepseek") == "deepseek":
            try:
                result |= self.agent.queue_image(session, target)
            except ValueError as exc:  # a non-vision model still receives the file
                result["vision"] = str(exc)
        return result

    async def poll(self, sid, job):
        """Wait for a farm task inside the tool. A shell command must never poll for this.

        The poll ticks every POLL_SECONDS for the whole render (20+ minutes), so a per-tick notice
        floods the event log, the context and the Telegram mirror. Only a genuinely new stage is
        announced — each distinct stage once (submitted → rendering → done/failed) — and the
        repeated same-stage ticks in between are silent.
        """
        deadline, seen = time.monotonic() + TIMEOUTS.get(job["kind"], 1800), set()
        while time.monotonic() < deadline:
            rows = await self.request("GET", "/renderfin/api-render-get-task-by-url",
                                      params={"url": job["output_url"]})
            row = rows[0] if rows else {}
            stage = str(row.get("status", "pending")).lower()
            if stage not in seen:
                self.agent.store.event(sid, "notice", {"text": f"Ферма · {job['kind']} · {stage}",
                                                       "task_id": job["task_id"]})
                seen.add(stage)
            if stage in {"error", "failed", "discarded"}:
                detail = str(row.get("error") or row.get("error_string") or stage)[:400]
                raise ProviderError(f"задача {job['task_id']} отклонена ({row.get('workflow', '')}): {detail}")
            if stage in {"done", "ready"}:
                return row
            await asyncio.sleep(POLL_SECONDS)
        return None

    @staticmethod
    def prepare_frame(source):
        """Farm animation workflows expect a moderate frame; a 2048px still is downscaled first."""
        from io import BytesIO

        from PIL import Image

        with Image.open(source) as image:
            image.load()
            frame = image.convert("RGB")
            if max(frame.size) > 1024:
                frame.thumbnail((1024, 1024))
            buffer = BytesIO()
            frame.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), source.stem[:40] + ".png"

    # ------------------------------------------------------------------ durable pending tasks
    @staticmethod
    def state_key(sid, task_id):
        """One row per task, so every slot survives a restart on its own."""
        return "farm:%s:%s" % (sid, task_id)

    def remember(self, sid, job):
        self.agent.store.set_state(self.state_key(sid, job["task_id"]), json.dumps(job, ensure_ascii=False))

    def forget(self, sid, job=None):
        if job is None:
            self.agent.store.execute("DELETE FROM state WHERE key LIKE ?", ("farm:" + sid + ":%",))
            return
        self.agent.store.execute("DELETE FROM state WHERE key=?", (self.state_key(sid, job["task_id"]),))

    def pending(self):
        rows = self.agent.store.rows("SELECT key,value FROM state WHERE key LIKE 'farm:%'")
        jobs = []
        for row in rows:
            sid, _, task_id = row["key"][5:].rpartition(":")
            jobs.append((sid or task_id, json.loads(row["value"])))  # legacy rows kept a bare session id
        return jobs

    def resume(self):
        """After a restart, keep waiting for farm tasks that were still rendering."""
        resumed = []
        for sid, job in self.pending():
            session = self.agent.store.session(sid)
            if not session or session.get("deleted") or time.time() - job.get("started", 0) > TIMEOUTS.get(job["kind"], 1800):
                self.forget(sid)
                continue
            # No per-task "продолжаю ждать" notice: resume runs on every startup, so over a restart
            # loop during one long render it paired with the cancel notice into dozens of events.
            # poll() re-announces the real stage; the render itself is never restarted here.
            self.spawn(session, job["kind"], self.finish(session, job))
            resumed.append(job["task_id"])
        return resumed

    def attach_skill(self, session, query):
        if not self.skills:
            raise ValueError("Индекс скиллов недоступен")
        found = self.skills.search(query, limit=1)
        if not found:
            raise ValueError("Скилл не найден в индексе")
        return self.skills.attach(found[0]["id"], session["id"])

    # ------------------------------------------------------------------ transport
    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, FARM + path, timeout=120, **kwargs)
        if response.status_code >= 400:
            raise ProviderError(f"Ферма HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    async def download(self, url, target):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"autorig.online", "www.autorig.online"} or parsed.username:
            raise ValueError("Артефакт должен приходить с подключённого HTTPS-origin фермы")
        size = 0
        async with self.client.stream("GET", url, timeout=300, follow_redirects=False) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("Неожиданный редирект артефакта")
            with target.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 200 * 1024 * 1024:
                        raise ValueError("Артефакт превышает 200 MB")
                    output.write(chunk)
        return size

    async def close(self):
        tasks = [task for jobs in self.jobs.values() for task in jobs.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
