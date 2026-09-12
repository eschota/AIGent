"""The DeepSeek Harness engine: AIGent sessions run on `dsh`, AIGent keeps everything around it.

One harness runtime per session, started lazily through the official Python SDK (a JSON-RPC
server on stdio, bundled as a single executable). The runtime owns the model loop — turns,
steps, tools, subagents, goals, compaction, prompt caching — and AIGent owns what it did before:
the interface and its event journal, Telegram, approvals for its own tools, the farm and media
tools, the skill index and the supervisor restart. Those tools reach the engine over MCP
(see `mcp_bridge`), so a farm render called by the engine still runs in this process.

What a turn does here is translation: the runtime's session events become the journal events the
interface already renders (`tool`, `tool_result`, `assistant`, `subagent`, `goal`, `usage`), the
final answer goes out through `Agent.tell` (so Telegram sees it), a message the owner sends
mid-turn is forwarded into the running turn, and stop kills the runtime.

Known limits of harness 0.1.5rc1 this engine works around: the Windows sandbox runner is broken,
so the engine runs with `danger-full-access` (AIGent's own tools keep their approvals); the SDK
cannot resume a session id in a fresh runtime, so every runtime starts a new engine session and
the first prompt carries a short brief of the AIGent session it continues.
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from .prompting import CORE
from .providers import ProviderError, account_usage
from .subagents import PALETTE

HOME_DIR = "dsh"
PROFILE = "sdk"
POLL_SECONDS = 0.25
QUEUE_EVERY = 4          # polls between looks at the owner's queue (~1 s)
IDLE_MINUTES = 20        # a runtime nobody used for this long is closed; the next turn starts a new one
BRIEF_CHARS = 1500
GOAL_STATUS = {"active": "active", "paused": "blocked", "blocked": "blocked", "complete": "done"}
MCP_SERVER = "aigent"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"

# The system-prompt persona of every runtime: the language rule sits above everything else,
# because a DeepSeek model handed English instructions and Russian tasks drifts into Chinese.
PERSONA_PREFIX = ("You are a coding agent powered by the {{model}} model, working inside AIGent for its owner. "
                  "Всегда отвечай владельцу по-русски (если он сам не пишет на другом языке); "
                  "код, команды и имена файлов остаются как есть.")

ENGINE_INSTRUCTIONS = """# AIGent

Ты работаешь внутри AIGent, рабочего пространства владельца. Владелец читает твою работу в
интерфейсе AIGent и в Telegram. Отвечай по-русски, если владелец не пишет на другом языке;
промежуточные комментарии и итоговый отчёт — тоже по-русски.

""" + CORE.split("Working style.", 1)[1].strip() + """

AIGent's own tools are the `mcp__aigent__*` tools: the AutoRig farm (images, video, 3D), video
inspection and assembly, the skill index, `send_file` to deliver a workspace file to the owner,
`server_status` and `restart_server` for the AIGent server itself. A long farm render waits inside
its tool; never poll it with a shell loop.

When the workspace is the AIGent repository (connector/, desktop/, tests/): run the checks
(`.venv\\Scripts\\python.exe -m pytest -q`, `.venv\\Scripts\\python.exe -m ruff check connector tests`,
`node desktop/ui.test.cjs` for interface changes), commit only the files you changed to main,
then call `mcp__aigent__restart_server` and verify with `mcp__aigent__server_status` after the
restart. Temporary helper scripts belong in `.tmp/`, never in the project root.
"""


def dsh_available():
    try:
        import deepseek_harness  # noqa: F401
    except ImportError:
        return False
    return True


class Runtime:
    """One harness process serving one AIGent session."""

    def __init__(self, sid, harness, dsh_session, cwd, model):
        self.sid, self.harness, self.dsh_session, self.cwd, self.model = sid, harness, dsh_session, cwd, model
        self.started = self.last_used = time.time()
        self.prompts = 0
        self.turn = None  # the live TurnMapper while a turn runs


class DshEngine:
    """Extension-free engine: `Agent.run` hands the turn here when the session is on dsh."""

    def __init__(self, agent, config, store, factory=None):
        self.agent, self.config, self.store = agent, config, store
        self.factory = factory  # test seam: something that builds a harness like DeepSeekHarness(config=...)
        self.runtimes = {}

    # ------------------------------------------------------------------ selection
    def enabled(self):
        return self.config.values.get("engine", "dsh") == "dsh" and (self.factory is not None or dsh_available())

    def handles(self, session):
        return self.enabled() and (session.get("provider") or "deepseek") == "deepseek"

    def home(self):
        path = self.config.root / HOME_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def base_url(self):
        return self.config.values.get("mcp_base_url") or f"http://127.0.0.1:{os.environ.get('AIGENT_PORT', '8787')}"

    # ------------------------------------------------------------------ profile
    def instructions(self):
        """The global AGENTS.md the harness loads for every session: AIGent's working style."""
        path = self.home() / "AGENTS.md"
        if not path.exists() or path.read_text(encoding="utf-8") != ENGINE_INSTRUCTIONS:
            path.write_text(ENGINE_INSTRUCTIONS, encoding="utf-8")
        return path

    def patch_text(self, session, workspace):
        sid = session["id"]
        skills = (Path(workspace) / ".claude" / "skills").as_posix()
        return "\n".join([
            "# Generated by AIGent for one session; regenerated on every runtime start.",
            "- id: system-prompt",
            "  config:",
            f"    personaPrefix: {json.dumps(PERSONA_PREFIX)}",
            "    personaSuffix: 'Your working directory is {{cwd}}.'",
            "- id: session-telemetry-otel",
            "  config:",
            "    mode: DISABLED",
            "- id: skill-filesystem",
            "  config:",
            "    customSkillDirs:",
            f"      - {json.dumps(skills)}",
            "- insert:",
            f"    - id: mcp-{MCP_SERVER}",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {MCP_SERVER}",
            "        transport: streamable-http",
            f"        url: {json.dumps(self.base_url() + '/api/mcp/' + sid)}",
            "        headers:",
            f"          Authorization: {json.dumps('Bearer ' + self.config['connector_token'])}",
            "",
        ])

    def patch_file(self, session, workspace):
        folder = self.home() / "patches"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{session['id']}.patch.yml"
        path.write_text(self.patch_text(session, workspace), encoding="utf-8")
        return path

    def api_key(self, session):
        account_id = session.get("account_id", "deepseek-default")
        return (self.config["deepseek_key"] if account_id == "deepseek-default"
                else self.config["account_keys"].get(account_id, ""))

    def model(self, session):
        return session.get("model") or self.config["model"]

    # ------------------------------------------------------------------ runtimes
    def build(self, session, workspace):
        """A harness for this session (blocking handshake; call it in a thread)."""
        model = self.model(session)
        self.instructions()
        patch = self.patch_file(session, workspace)
        options = dict(provider="deepseek-official", model=model, cwd=str(workspace),
                       dsh_home=str(self.home()), profile=PROFILE, patches=(str(patch),),
                       max_tokens=int(self.config["max_output_tokens"]),
                       env={"DEEPSEEK_API_KEY": self.api_key(session), "DSH_PERMISSION_MODE": "danger-full-access",
                            "DSH_TELEMETRY_MODE": "DISABLED"})
        effort = self.config.values.get("dsh_reasoning_effort") or ""
        if effort:
            options["reasoning_effort"] = effort
        if self.factory is not None:
            harness = self.factory(**options)
        else:
            from deepseek_harness import DeepSeekHarness
            harness = DeepSeekHarness(**options)
        harness.start()
        return harness, model

    def runtime(self, session):
        """The session's live runtime, started if needed; replaced when the model or workspace changed."""
        sid = session["id"]
        workspace = self.agent.workspace(sid)
        current = self.runtimes.get(sid)
        if current and (current.model != self.model(session) or current.cwd != str(workspace)):
            self.close_runtime(sid, "параметры сессии изменились")
            current = None
        if current is None:
            harness, model = self.build(session, workspace)
            generation = f"{int(time.time())}-{secrets.token_hex(3)}"  # unique even within one second
            current = Runtime(sid, harness, f"{sid}-{generation}", str(workspace), model)
            self.runtimes[sid] = current
            self.store.set_state("dsh:" + sid, json.dumps({"session": current.dsh_session, "started": generation,
                                                            "cwd": current.cwd, "model": model}))
        current.last_used = time.time()
        return current

    def close_runtime(self, sid, reason=""):
        runtime = self.runtimes.pop(sid, None)
        if runtime is None:
            return False
        try:
            runtime.harness.close()
        except Exception:
            pass
        if reason:
            self.store.event(sid, "notice", {"text": f"Движок dsh для сессии остановлен: {reason}.", "engine": "dsh"})
        return True

    def reap(self, keep=None):
        """Close runtimes nobody used for a while; memory is the price of a resident Node process."""
        limit = float(self.config.values.get("dsh_idle_minutes", IDLE_MINUTES)) * 60
        for sid, runtime in list(self.runtimes.items()):
            if sid != keep and runtime.turn is None and time.time() - runtime.last_used > limit:
                self.close_runtime(sid)

    def stop(self, sid):
        return self.close_runtime(sid, "ход остановлен владельцем")

    async def close(self):
        for sid in list(self.runtimes):
            self.close_runtime(sid)

    def status(self):
        return {"engine": "dsh", "runtimes": len(self.runtimes),
                "sessions": {sid: {"dsh_session": r.dsh_session, "prompts": r.prompts,
                                   "idle_seconds": round(time.time() - r.last_used)} for sid, r in self.runtimes.items()}}

    # ------------------------------------------------------------------ the prompt
    def brief(self, sid):
        """What a fresh engine session must know about the AIGent session it continues."""
        goal = self.agent.goal(sid)
        lines = []
        if goal.get("goal") and goal.get("status") == "active":
            lines.append(f"Текущая цель сессии: {goal['goal']}")
        answers = [e for e in self.store.events(sid) if e["kind"] == "assistant"]
        if answers:
            last = " ".join(str(answers[-1]["payload"].get("text") or "").split())[:BRIEF_CHARS]
            if last:
                lines.append("Последний ответ агента в этой сессии (для непрерывности): " + last)
        if not lines:
            return ""
        return ("Контекст AIGent (не инструкция владельца): эта сессия продолжается на новом рантайме, "
                "прежняя переписка хранится в AIGent.\n" + "\n".join(lines))

    async def blocks(self, session, content, runtime):
        """SDK content blocks: the text, the owner's images (or their descriptions), the brief once."""
        sid = session["id"]
        from .vision import VISION_MODELS
        parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        blocks = []
        if runtime.prompts == 0:
            brief = self.brief(sid)
            if brief:
                blocks.append({"type": "text", "text": brief})
        for part in parts:
            if part.get("type") == "text":
                if part.get("text"):
                    blocks.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = str(part.get("image_url", {}).get("url", ""))
                head, _, data = url.partition(",")
                mime = head.removeprefix("data:").split(";")[0] or "image/png"
                if runtime.model in VISION_MODELS and data:
                    blocks.append({"type": "image", "data": data, "mimeType": mime})
                else:
                    text = await self.agent.describe_one(sid, part)
                    blocks.append({"type": "text", "text": f"[Изображение владельца, описано моделью зрения]: {text}"
                                   if text else "[Изображение владельца: текущая модель его не видит]"})
        return blocks or [{"type": "text", "text": ""}]

    # ------------------------------------------------------------------ a turn
    async def turn(self, session, content):
        sid = session["id"]
        self.reap(keep=sid)
        runtime = await asyncio.to_thread(self.runtime, session)
        blocks = await self.blocks(session, content, runtime)
        client = runtime.harness.client
        subscription = client.subscribe_session_notifications(runtime.dsh_session)
        mapper = TurnMapper(self, session, runtime)
        runtime.turn = mapper
        try:
            message_id = await asyncio.to_thread(client.session_prompt, runtime.dsh_session, blocks,
                                                 notification_subscription=subscription)
            runtime.prompts += 1
            mapper.message_id = message_id
            polls = 0
            while not mapper.finished:
                subscription.drain(mapper.handle)
                if mapper.finished:
                    break
                polls += 1
                if polls % QUEUE_EVERY == 0:
                    await self.forward_queued(session, runtime, subscription)
                await asyncio.sleep(POLL_SECONDS)
            text = mapper.final_answer()
            if text:
                self.agent._delivered[sid] = True
                await self.agent.tell(session, text)
            else:
                self.store.event(sid, "notice", {"text": "Движок завершил ход без текстового ответа.", "engine": "dsh"})
        except asyncio.CancelledError:
            self.stop(sid)
            raise
        except Exception as exc:
            self.close_runtime(sid, "рантайм упал: " + self.config.redact(exc))
            raise ProviderError(self.config.redact(f"dsh: {type(exc).__name__}: {exc}")) from None
        finally:
            runtime.turn = None
            runtime.last_used = time.time()
            try:
                subscription.close()
            except Exception:
                pass

    async def forward_queued(self, session, runtime, subscription):
        """An owner message sent mid-turn goes into the running turn instead of waiting behind it."""
        sid = session["id"]
        item = self.store.take_queued(sid)
        if not item:
            return False
        content = item["payload"]
        blocks = await self.blocks(session, content, runtime)
        await asyncio.to_thread(runtime.harness.client.session_prompt, runtime.dsh_session, blocks,
                                notification_subscription=subscription)
        self.store.event(sid, "user", {"text": self.agent.plain_text(content), "inline": True})
        self.store.event(sid, "queue_started", {"id": item["id"], "auto": False, "inline": True})
        self.store.event(sid, "notice", {"text": "Сообщение владельца получено посреди хода — передано движку, "
                                                 "не прерывая работу.", "absorbed": 1})
        self.agent.auto_pending.discard(sid)
        return True


class TurnMapper:
    """Turns the runtime's notifications into AIGent journal events for one turn."""

    def __init__(self, engine, session, runtime):
        self.engine, self.session, self.runtime = engine, session, runtime
        self.agent, self.store = engine.agent, engine.store
        self.sid = session["id"]
        self.message_id = None
        self.received = False
        self.finished = False
        self.final_text = ""
        self.pending_text = ""
        self.calls = {}          # callId -> display name
        self.children = {}       # child session id -> chip record
        self.marks = 0

    # ------------------------------------------------------------------ dispatch
    def handle(self, note):
        method, payload = note.method, note.payload
        if method == "session.status":
            if payload.get("sessionId") == self.runtime.dsh_session and self.received \
                    and payload.get("status") == "idle":
                self.finish()
            return
        if method == "subagent.started":
            self.child_started(payload)
            return
        if method == "subagent.finished":
            self.child_finished(payload)
            return
        if method != "session.event":
            return
        event = payload.get("event") or {}
        if payload.get("sessionId") != self.runtime.dsh_session:
            self.child_event(payload.get("sessionId"), event)
            return
        kind = event.get("type")
        data = event.get("data") or {}
        if not self.received:
            # Stale notifications of an earlier turn may still be in flight: nothing counts until
            # the runtime acknowledges this turn's own prompt in its inbox.
            if kind == "agent/inbox/spliced" and self.message_id and \
                    self.message_id in json.dumps(data.get("inserted") or [], default=str):
                self.received = True
            return
        handler = getattr(self, "on_" + kind.replace("/", "_"), None) if kind else None
        if handler:
            handler(data)

    def finish(self):
        if self.finished:
            return
        self.finished = True

    def final_answer(self):
        """What the model said last in this turn: the text the owner gets as the answer."""
        return (self.final_text or self.pending_text).strip()

    # ------------------------------------------------------------------ session events
    def on_assistant_message(self, data):
        message = data.get("message") or {}
        reasoning, text = [], []
        for block in message.get("content") or []:
            if block.get("type") == "reasoning" and block.get("text"):
                reasoning.append(block["text"])
            elif block.get("type") == "text" and block.get("text"):
                text.append(block["text"])
        step = f"{data.get('turn', 0)}-{data.get('step', 0)}"
        if reasoning:
            self.store.event(self.sid, "stream", {"id": "dsh-" + step, "text": "", "reasoning": "\n".join(reasoning),
                                                  "done": True})
        if text:
            self.pending_text = "\n".join(text)
        usage = data.get("usage") or {}
        if usage:
            self.account(usage)

    def on_tool_call(self, data):
        name = str(data.get("name") or "").removeprefix(MCP_PREFIX)
        try:
            arguments = json.loads(data.get("arguments") or "{}")
        except ValueError:
            arguments = {"raw": data.get("arguments")}
        if self.pending_text:
            self.store.event(self.sid, "assistant", {"text": self.pending_text, "phase": "commentary"})
            self.pending_text = ""
        self.calls[data.get("callId")] = name
        self.store.event(self.sid, "tool", {"name": name, "arguments": arguments, "call_id": data.get("callId"),
                                            "step": data.get("step"), "ceiling": None, "engine": "dsh"})

    def on_tool_result(self, data):
        call_id = (data.get("message") or {}).get("source", {}).get("callId") or data.get("callId")
        name = self.calls.get(call_id, "tool")
        texts = []
        for block in (data.get("message") or {}).get("content") or []:
            for inner in block.get("content") or ([block] if block.get("type") == "text" else []):
                if inner.get("type") == "text" and inner.get("text"):
                    texts.append(inner["text"])
        joined = "\n".join(texts)
        try:
            result = json.loads(joined) if joined.startswith("{") or joined.startswith("[") else {"text": joined}
        except ValueError:
            result = {"text": joined}
        self.store.event(self.sid, "tool_result", {"name": name, "result": result, "call_id": call_id})

    def on_step_end(self, data):
        if self.pending_text:
            self.final_text = self.pending_text
            self.pending_text = ""

    def on_turn_end(self, data):
        reason = (data.get("reason") or {}).get("kind")
        if reason and reason != "completed":
            self.store.event(self.sid, "notice", {"text": f"Движок завершил ход: {reason}.", "engine": "dsh"})

    def on_goal_change(self, data):
        snapshot = data.get("snapshot") or data.get("goal") or data
        objective = snapshot.get("objective") if isinstance(snapshot, dict) else None
        if not objective:
            return
        status = GOAL_STATUS.get(snapshot.get("phase"), "active")
        self.agent.save_goal(self.sid, goal=str(objective)[:200], status=status, source="model")

    def on_approval_asked(self, data):
        self.store.event(self.sid, "notice", {"text": "Движок запросил одобрение: " + str(data.get("reason") or data.get("toolName") or "")[:300]})

    def on_approval_decided(self, data):
        self.store.event(self.sid, "notice", {"text": f"Решение по запросу движка: {data.get('outcome')}."})

    # ------------------------------------------------------------------ children (workers)
    def child_started(self, payload):
        child = payload.get("childSessionId") or payload.get("agentId")
        if not child:
            return
        emoji, color = PALETTE[self.marks % len(PALETTE)]
        self.marks += 1
        rec = {"id": child, "emoji": emoji, "color": color, "goal": str(payload.get("label") or "субагент")[:60],
               "status": "running", "steps": 0, "activity": None, "detail": "", "files": [], "report": "",
               "error": None, "tokens": 0, "cost_usd": None, "started": time.time(), "log": []}
        self.children[child] = rec
        self.emit_child("spawn", rec)

    def child_event(self, child, event):
        rec = self.children.get(child)
        if not rec:
            return
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "step/start":
            rec["steps"] = int(data.get("step") or rec["steps"] + 1)
        elif kind == "tool/call":
            rec["activity"] = str(data.get("name") or "").removeprefix(MCP_PREFIX)
            try:
                args = json.loads(data.get("arguments") or "{}")
            except ValueError:
                args = {}
            rec["detail"] = str(args.get("file_path") or args.get("path") or args.get("command") or "")[:120]
            rec["log"].append(f"{rec['activity']} {rec['detail']}".strip())
            del rec["log"][:-40]
            self.emit_child("update", rec)
        elif kind == "assistant/message":
            texts = [b.get("text") for b in (data.get("message") or {}).get("content") or [] if b.get("type") == "text"]
            if texts:
                rec["report"] = "\n".join(t for t in texts if t)[:4000]
            usage = data.get("usage") or {}
            if usage:
                rec["tokens"] += int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0) \
                    + int(usage.get("cacheReadTokens") or 0)
                self.account(usage)

    def child_finished(self, payload):
        child = payload.get("childSessionId") or payload.get("agentId")
        rec = self.children.get(child)
        if not rec:
            return
        rec["status"] = "done" if payload.get("status") == "ok" else "failed"
        rec["activity"] = None
        if payload.get("status") != "ok":
            rec["error"] = str(payload.get("error") or payload.get("stopReason") or "failed")[:300]
        self.emit_child("done", rec)

    def emit_child(self, phase, rec):
        self.store.event(self.sid, "subagent", {"phase": phase, **{k: rec.get(k) for k in (
            "id", "emoji", "color", "goal", "status", "steps", "activity", "detail", "files", "report",
            "error", "tokens", "cost_usd", "log")}, "seconds": round(time.time() - rec["started"], 2),
            "engine": "dsh"})

    # ------------------------------------------------------------------ accounting
    def account(self, usage):
        hit = int(usage.get("cacheReadTokens") or 0)
        miss = int(usage.get("inputTokens") or 0)
        raw = {"prompt_tokens": hit + miss, "completion_tokens": int(usage.get("outputTokens") or 0),
               "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss,
               "reasoning_tokens": int(usage.get("reasoningTokens") or 0)}
        record = account_usage(raw, self.runtime.model, self.engine.config)
        record.update(provider="deepseek", account_id=self.session.get("account_id", "deepseek-default"),
                      billing="api", engine="dsh")
        self.store.add_usage(self.sid, record)
