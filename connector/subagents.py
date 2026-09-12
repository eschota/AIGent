"""Fast code-writing subagents fanned out from the main DeepSeek agent.

The main agent calls ``spawn_subagents`` to write several small pieces of code at once.
Each subagent is a bounded, tool-less DeepSeek completion that shares one BYTE-IDENTICAL
prefix (a fixed instruction plus the caller's ``shared_context``) so DeepSeek's prompt
cache serves that prefix cheaply for every subagent in the batch — this is the "use
caching to the max, in parallel" the owner wants. Only the small task tail differs.

Subagents PRODUCE proposals (a full file or a Codex patch); they never write. Applying a
winning piece still goes through the normal write_file / apply_patch review, so nothing
reaches disk without approval (or the session's explicit auto-apply). The tool returns a
compact per-subagent summary the main agent can act on.

Scheduling is an honest hint, not magic: a rolling success score per (session, task-kind)
is kept in the store; higher-scored tasks are dispatched first and, when concurrency is
limited, run before lower-scored ones. Each finished subagent's score is recomputed from
whether it produced parseable code that passes a quick syntax check and from its cache-hit
ratio, and that feeds the rolling average — so repeated similar work tends to lead the queue.
"""

import asyncio
import json
import secrets
import time
from pathlib import Path

from .agent import tool
from .providers import DeepSeek, ProviderError

# One distinct emoji + colour per live subagent, recycled once a subagent frees it. Eight
# entries match the hard concurrency cap, so a full batch still gets unique marks.
PALETTE = [
    ("\U0001f7e6", "#4f8cff"), ("\U0001f7e9", "#3ecf8e"), ("\U0001f7e8", "#f5c451"),
    ("\U0001f7e7", "#f59b42"), ("\U0001f7ea", "#a878f0"), ("\U0001f7e5", "#f0685f"),
    ("\U0001f7eb", "#b98a5e"), ("⬜", "#c8d2dc"),
]
MAX_TASKS = 8            # per spawn: keeps emoji marks unique and the fan-out cheap
CONCURRENCY_CAP = 8      # hard ceiling regardless of the configured value
GOAL_CHARS = 60          # brief goal shown on a chip

SUBAGENT_SYSTEM = (
    "You are a fast, focused code-writing subagent inside a larger agent. You write ONE small "
    "piece of code and nothing else. You have no tools. Return ONLY a single JSON object, no prose "
    "and no markdown fence, shaped exactly as one of:\n"
    '  {"path": "relative/file.py", "content": "<full file text>", "summary": "<one line>"}\n'
    '  {"path": "relative/file.py", "patch": "*** Begin Patch\\n...\\n*** End Patch", "summary": "<one line>"}\n'
    "Use content for a new or fully rewritten file, patch for a surgical edit. Keep it minimal and "
    "correct. Your output is a PROPOSAL: the main agent reviews and applies it, so never assume it "
    "is written yet."
)


class SubAgents:
    """Extension: spawn bounded DeepSeek subagents that propose code in parallel."""

    def __init__(self, agent, client):
        self.agent, self.client = agent, client
        # Per-session live/last-batch records keyed by subagent id, and their asyncio tasks.
        self.state = {}
        self.tasks = {}
        self.dispatch_order = {}  # sid -> ids in the order they actually acquired a slot (for tests)

    # ------------------------------------------------------------------ config
    def enabled(self):
        return bool(self.agent.config.values.get("subagents_enabled", True))

    def concurrency(self):
        value = int(self.agent.config.values.get("subagent_concurrency", 4) or 4)
        return max(1, min(CONCURRENCY_CAP, value))

    def max_tokens(self):
        return max(256, int(self.agent.config.values.get("subagent_max_tokens", 2000) or 2000))

    # ------------------------------------------------------------------ DeepSeek extension protocol
    def tools(self, session):
        if not self.enabled():
            return []  # hidden entirely when disabled, so the model is never offered it
        return [tool(
            "spawn_subagents",
            "Fan out several fast code-writing subagents that run IN PARALLEL, each producing one "
            "small proposed file or patch. They share a byte-identical cached prefix (shared_context) "
            "so DeepSeek's prompt cache serves it cheaply for every subagent. Subagents have no tools "
            "and never write: apply a winning piece yourself with write_file/apply_patch (review still "
            "applies). Give each task a focused goal and, optionally, the target files, extra context "
            "and a priority score 0..1 (higher is dispatched first).",
            {"tasks": {"type": "array", "items": {"type": "object", "properties": {
                "goal": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "context": {"type": "string"},
                "score": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["goal"], "additionalProperties": False}},
             "shared_context": {"type": "string"}},
            ["tasks"])]

    async def execute(self, session, name, args):
        if name != "spawn_subagents":
            raise ValueError("Unknown subagent tool")
        if not self.enabled():
            return {"error": "Subagents are disabled in settings."}
        return await self.spawn(session, args.get("tasks") or [], str(args.get("shared_context") or ""))

    def running(self, sid):
        """True while any subagent of this session is queued or in flight."""
        return any(rec["status"] in ("queued", "running") for rec in self.state.get(sid, {}).values())

    # ------------------------------------------------------------------ scoring / scheduling hints
    @staticmethod
    def _kind(task):
        """The task-kind the rolling score is keyed by: the target file extension, else 'code'."""
        files = task.get("files") or []
        if files:
            ext = Path(str(files[0])).suffix.lstrip(".").lower()
            if ext:
                return ext
        return "code"

    def _rolling(self, sid, kind):
        try:
            return float(self.agent.store.get_state(f"subagent:score:{sid}:{kind}", ""))
        except ValueError:
            return 0.5  # no history yet: a neutral prior, neither favoured nor penalised

    def _remember_score(self, sid, kind, observed):
        # EWMA: repeated success on a kind raises its dispatch priority over time; one failure
        # lowers it. This is a queue hint only — it never reduces review or bypasses safety.
        prev = self._rolling(sid, kind)
        self.agent.store.set_state(f"subagent:score:{sid}:{kind}", round(prev * 0.7 + observed * 0.3, 4))

    @staticmethod
    def _syntax_ok(path, content):
        """A cheap Python syntax gate; other languages pass structurally (no in-process compiler)."""
        if not content:
            return True
        if str(path).endswith(".py"):
            try:
                compile(content, str(path), "exec")
            except SyntaxError:
                return False
        return True

    # ------------------------------------------------------------------ live state + events
    def _assign_mark(self, sid):
        used = {rec["emoji"] for rec in self.state.get(sid, {}).values()
                if rec["status"] in ("queued", "running")}
        for emoji, color in PALETTE:
            if emoji not in used:
                return emoji, color
        return PALETTE[len(self.state.get(sid, {})) % len(PALETTE)]

    def _emit(self, sid, phase, rec):
        rec = dict(rec)
        rec["seconds"] = self._seconds(rec)
        self.agent.store.event(sid, "subagent", {"phase": phase, **{k: rec.get(k) for k in (
            "id", "emoji", "color", "goal", "status", "kind", "context_tokens",
            "seconds", "tokens", "cost_usd", "score", "path", "summary", "error")}})

    @staticmethod
    def _seconds(rec):
        end = rec.get("finished") or time.time()
        return round(max(0.0, end - rec["started"]), 2) if rec.get("started") else 0.0

    def snapshot(self, sid):
        """Live list plus batch totals, for the panel and the API."""
        records = list(self.state.get(sid, {}).values())
        out = []
        for rec in records:
            out.append({k: rec.get(k) for k in ("id", "emoji", "color", "goal", "status", "kind",
                        "context_tokens", "tokens", "cost_usd", "score", "path", "summary", "error")}
                       | {"seconds": self._seconds(rec)})
        live = [r for r in records if r["status"] in ("queued", "running")]
        scored = [r["score"] for r in records if r.get("score") is not None]
        totals = {"count": len(live), "total": len(records),
                  "total_tokens": sum(r.get("tokens") or 0 for r in records),
                  "total_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in records), 6),
                  "avg_score": round(sum(scored) / len(scored), 3) if scored else None}
        return {"subagents": out, "totals": totals}

    # ------------------------------------------------------------------ spawn / run
    def _backend(self, session):
        """A DeepSeek backend on the session's account/model, but with a tight subagent budget."""
        base = self.agent.deepseek
        options = dict(self.agent.config.values)
        options["model"] = session.get("model") or self.agent.config["model"]
        account_id = session.get("account_id", "deepseek-default")
        options["deepseek_key"] = (self.agent.config["deepseek_key"] if account_id == "deepseek-default"
                                   else self.agent.config["account_keys"].get(account_id, ""))
        options["max_output_tokens"] = self.max_tokens()
        options["thinking"] = False  # code pieces are cheap generation; skip reasoning tokens
        return DeepSeek(options, base.client if isinstance(base, DeepSeek) else self.client)

    @staticmethod
    def _shared_prefix(shared_context):
        """The message every subagent in a batch sends FIRST, byte-identical, so it caches."""
        content = SUBAGENT_SYSTEM
        if shared_context.strip():
            content += "\n\n# Shared project context (identical for every subagent)\n" + shared_context.strip()
        return {"role": "system", "content": content}

    @staticmethod
    def _task_message(task):
        lines = ["Write exactly this one piece of code.", "Goal: " + str(task.get("goal", "")).strip()]
        if task.get("files"):
            lines.append("Target file(s): " + ", ".join(str(f) for f in task["files"]))
        if task.get("context"):
            lines.append("Task context: " + str(task["context"]).strip())
        return {"role": "user", "content": "\n".join(lines)}

    async def spawn(self, session, tasks, shared_context=""):
        sid = session["id"]
        tasks = [t for t in tasks if isinstance(t, dict) and str(t.get("goal", "")).strip()][:MAX_TASKS]
        if not tasks:
            raise ValueError("spawn_subagents requires at least one task with a goal.")
        prefix = self._shared_prefix(shared_context)
        self.state.setdefault(sid, {})
        self.tasks.setdefault(sid, {})
        self.dispatch_order[sid] = []
        records = []
        for task in tasks:
            kind = self._kind(task)
            raw = task.get("score")
            dispatch = float(raw) if isinstance(raw, (int, float)) and 0 <= raw <= 1 else self._rolling(sid, kind)
            emoji, color = self._assign_mark(sid)
            rec = {"id": "sa_" + secrets.token_hex(4), "emoji": emoji, "color": color,
                   "goal": str(task.get("goal", "")).strip()[:GOAL_CHARS], "status": "queued",
                   "kind": kind, "dispatch_score": round(dispatch, 4), "context_tokens": 0,
                   "started": 0.0, "finished": 0.0, "tokens": 0, "cost_usd": None,
                   "cache_hit": None, "cache_miss": None, "score": None, "path": None,
                   "summary": None, "error": None}
            self.state[sid][rec["id"]] = rec
            records.append((rec, task))
            self._emit(sid, "spawn", rec)
        # Higher dispatch score first: with a bounded semaphore the leading tasks acquire slots first.
        records.sort(key=lambda pair: -pair[0]["dispatch_score"])
        sem = asyncio.Semaphore(self.concurrency())
        jobs = {}
        for rec, task in records:
            job = asyncio.create_task(self._run_one(session, rec, task, prefix, sem))
            jobs[rec["id"]] = job
            self.tasks[sid][rec["id"]] = job
        try:
            results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        finally:
            for rid, job in list(self.tasks.get(sid, {}).items()):
                if not job.done():
                    job.cancel()
                self.tasks.get(sid, {}).pop(rid, None)
        compact = [r for r in results if isinstance(r, dict)]
        scored = [r["score"] for r in compact if r.get("score") is not None]
        return {"spawned": len(compact), "concurrency": self.concurrency(),
                "results": compact,
                "totals": {"total_tokens": sum(r.get("tokens") or 0 for r in compact),
                           "total_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in compact), 6),
                           "avg_score": round(sum(scored) / len(scored), 3) if scored else None},
                "note": "These are PROPOSALS, not writes. Apply the winning pieces with "
                        "write_file or apply_patch; review/approval still applies."}

    async def _run_one(self, session, rec, task, prefix, sem):
        sid = session["id"]
        async with sem:
            self.dispatch_order[sid].append(rec["id"])
            rec["status"], rec["started"] = "running", time.time()
            messages = [prefix, self._task_message(task)]
            rec["context_tokens"] = round(len(json.dumps(messages, ensure_ascii=False)) / 4)
            self._emit(sid, "update", rec)
            try:
                message, usage = await self._backend(session).complete(messages, [], self.agent._silent)
            except asyncio.CancelledError:
                return self._finish(sid, rec, status="cancelled", error="Cancelled by owner.")
            except (ProviderError, ValueError, OSError) as exc:
                return self._finish(sid, rec, status="failed", error=self.agent.config.redact(exc))
            if usage:
                usage.update(provider="deepseek", account_id=session.get("account_id", "deepseek-default"),
                             billing="api", subagent=True)
                self.agent.store.add_usage(sid, usage)
                rec["tokens"] = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
                rec["cost_usd"] = usage.get("cost_usd")
                rec["cache_hit"], rec["cache_miss"] = usage.get("cache_hit_tokens"), usage.get("cache_miss_tokens")
            proposal = self._parse(message.get("content") if message else "")
            return self._finish(sid, rec, proposal=proposal)

    @staticmethod
    def _parse(content):
        """Extract the structured proposal from the model's reply; tolerate a code fence."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text
            if text.endswith("```"):
                text = text[:-3]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or not data.get("path") or not (data.get("content") or data.get("patch")):
            return None
        return {"path": str(data["path"]),
                **({"content": str(data["content"])} if data.get("content") else {}),
                **({"patch": str(data["patch"])} if data.get("patch") else {}),
                "summary": str(data.get("summary", ""))[:200]}

    def _finish(self, sid, rec, proposal=None, status="done", error=None):
        rec["finished"] = time.time()
        structured = bool(proposal)
        success = 0.0
        if structured:
            rec["path"] = proposal["path"]
            rec["summary"] = proposal.get("summary")
            success = 1.0 if self._syntax_ok(proposal["path"], proposal.get("content")) else 0.4
        hit, miss = rec.get("cache_hit"), rec.get("cache_miss")
        # Cache efficiency only rewards a subagent that actually produced usable code; a failed
        # parse scores zero however cheap it was.
        cache_ratio = (hit / (hit + miss) if structured and hit is not None and miss is not None
                       and (hit + miss) else 0.0)
        observed = round(min(1.0, 0.6 * success + 0.4 * cache_ratio), 4)
        if status == "done":
            rec["score"] = observed
            self._remember_score(sid, rec["kind"], observed)
        rec["status"] = status if status != "done" or structured else "failed"
        if not structured and status == "done":
            error = error or "Subagent did not return a parseable {path, content|patch} proposal."
        rec["error"] = error
        self._emit(sid, "done", rec)
        out = {"id": rec["id"], "emoji": rec["emoji"], "goal": rec["goal"], "kind": rec["kind"],
               "status": rec["status"], "score": rec["score"], "tokens": rec["tokens"],
               "cache_hit": hit, "cache_miss": miss, "cost_usd": rec["cost_usd"],
               "context_tokens": rec["context_tokens"], "seconds": self._seconds(rec),
               "proposal": True}
        if proposal:
            out.update(proposal)
        if error:
            out["error"] = error
        return out

    # ------------------------------------------------------------------ cancellation
    def cancel(self, sid):
        """Stop every in-flight subagent of the session; queued/running ones become cancelled."""
        jobs = self.tasks.get(sid, {})
        stopped = 0
        for job in list(jobs.values()):
            if not job.done():
                job.cancel()
                stopped += 1
        for rec in self.state.get(sid, {}).values():
            if rec["status"] in ("queued", "running"):
                rec.update(status="cancelled", finished=time.time(),
                           error=rec.get("error") or "Cancelled by owner.")
                self._emit(sid, "done", rec)
        return {"cancelled": stopped}

    async def close(self):
        for jobs in self.tasks.values():
            for job in jobs.values():
                job.cancel()
        pending = [job for jobs in self.tasks.values() for job in jobs.values()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
