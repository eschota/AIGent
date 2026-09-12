"""Coding workers the main DeepSeek agent delegates to.

A worker is a real subagent: its own conversation, its own step budget, the workspace and
execution tools of the session, run in parallel with the other workers of the batch. The main
agent stays the orchestrator — it splits the goal into independent tasks, spawns them with one
``spawn_subagents`` call, reads the reports and runs the integrated verification itself. That is
the default for writing code: a fresh, focused context writes better code than a long one.

Every worker executes tools through the very same ``Agent.execute`` as the main agent, in the
same session: write_file and apply_patch show the exact diff for approval (or apply it when the
session has auto-apply on), commands need the administrator's approval, and nothing bypasses
the workspace boundary. Workers cannot ask the owner, publish a goal or spawn workers.

All workers of a batch send one byte-identical prefix first — the worker instruction, the
project guidance and the caller's ``shared_context`` — so DeepSeek's prompt cache serves it
cheaply for the whole fan-out; only the task tail differs.
"""

import asyncio
import json
import os
import platform
import secrets
import time

from .agent import SUMMARY_REQUEST, TOOLS, Agent, tool
from .prompting import canonical_call, tool_inventory
from .providers import DeepSeek, ProviderError

# One distinct emoji + colour per live worker, recycled once a worker frees it. Eight entries
# match the hard concurrency cap, so a full batch still gets unique marks.
PALETTE = [
    ("\U0001f7e6", "#4f8cff"), ("\U0001f7e9", "#3ecf8e"), ("\U0001f7e8", "#f5c451"),
    ("\U0001f7e7", "#f59b42"), ("\U0001f7ea", "#a878f0"), ("\U0001f7e5", "#f0685f"),
    ("\U0001f7eb", "#b98a5e"), ("⬜", "#c8d2dc"),
]
MAX_TASKS = 8            # per spawn: keeps emoji marks unique and the fan-out reviewable
CONCURRENCY_CAP = 8      # hard ceiling regardless of the configured value
STEPS_CAP = 200          # hard ceiling of model calls per worker
DEFAULT_STEPS = 40
GOAL_CHARS = 60          # brief goal shown on a chip
REPORT_CHARS = 4000
RESULT_CHARS = 60000     # one tool result a worker keeps in full
CONTEXT_CHARS = 240000   # a worker's conversation is compacted above this
KEEP_RECENT = 6          # tool results left intact by that compaction
LOG_LINES = 40
# What a worker may use: the workspace and the execution of its session. Nothing that talks to
# the owner or the goal — that is the orchestrator's job — and nothing that spawns.
WORKER_TOOLS = ("list_files", "read_file", "search_files", "write_file", "run_command",
                "exec_command", "write_stdin", "apply_patch")

WORKER_SYSTEM = """You are a coding worker inside AIGent, spawned by the main agent for ONE task.
Work autonomously until it is finished: read the files you touch before editing, make small targeted
edits (apply_patch when available; write_file for a new or tiny file), keep the project's style, and
run the checks named in the task when execution is available. Never claim a result without a tool
result that proves it, and never fabricate tool output.
Nobody answers questions here: decide, state the assumption in your report, and continue. A denied
write or command is an answer, not an error — report it and stop touching that file.
Stay inside the task: do not refactor or edit files the task does not need.
Finish with a plain-text report the main agent can act on: what changed (files), how it was verified
(commands and their outcome), and what remains or is uncertain. Reply in the language of the task."""


class SubAgents:
    """Extension: spawn coding workers that run the session's tools in parallel."""

    def __init__(self, agent, client):
        self.agent, self.client = agent, client
        self.state = {}           # sid -> worker id -> live record
        self.tasks = {}           # sid -> worker id -> asyncio.Task
        self.dispatch_order = {}  # sid -> ids in the order they actually acquired a slot

    # ------------------------------------------------------------------ config
    def enabled(self):
        return bool(self.agent.config.values.get("subagents_enabled", True))

    def concurrency(self):
        value = int(self.agent.config.values.get("subagent_concurrency", 4) or 4)
        return max(1, min(CONCURRENCY_CAP, value))

    def max_tokens(self):
        return max(256, int(self.agent.config.values.get("subagent_max_tokens", 8192) or 8192))

    def max_steps(self):
        value = int(self.agent.config.values.get("subagent_max_steps", DEFAULT_STEPS) or DEFAULT_STEPS)
        return max(1, min(STEPS_CAP, value))

    # ------------------------------------------------------------------ DeepSeek extension protocol
    def tools(self, session):
        if not self.enabled():
            return []  # hidden entirely when disabled, so the model is never offered it
        return [tool(
            "spawn_subagents",
            "Delegate coding work to workers that run IN PARALLEL in this session. Each worker has the "
            "workspace and execution tools, a fresh context and its own step budget: it reads, edits, runs "
            "the checks you name and returns a report; its writes go through the same review as yours. Use "
            "it by default for any code change beyond one small edit: one task per file or module, with "
            "the acceptance criteria and the checks to run, all in ONE call. Then verify the integration "
            "yourself. shared_context is sent identically to every worker (cached): put the design, the "
            "conventions and the file map there, not in each task.",
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
        """True while any worker of this session is queued or in flight."""
        return any(rec["status"] in ("queued", "running") for rec in self.state.get(sid, {}).values())

    # ------------------------------------------------------------------ what a worker sees
    def worker_tools(self, session):
        """The session's workspace and execution tools, and nothing that reaches the owner or spawns."""
        tools = list(TOOLS)
        if self.agent.workspace_service:
            from .workspace import EXTRA_TOOLS
            tools = tools + list(EXTRA_TOOLS)
        return [t for t in tools if t["function"]["name"] in WORKER_TOOLS]

    def prefix(self, session, tools, shared_context=""):
        """The messages every worker of a batch sends FIRST, byte-identical, so the cache serves them."""
        sid = session["id"]
        lines = [WORKER_SYSTEM.strip(), "", "## Tools available"]
        for title, items in tool_inventory(tools):
            lines.append(title + ":")
            lines.extend(items)
        lines.append("Only these tools exist. You cannot ask the owner, publish a goal or spawn workers.")
        lines += ["", "## Approvals"]
        if session.get("auto_approve"):
            lines.append("Auto-apply is ON for this session: writes, patches and approved commands are applied "
                         "without asking. Review your own diff before sending it.")
        else:
            lines.append("Auto-apply is OFF: each write, patch and command is shown to the owner for approval. "
                         "A denial is an answer, not an error: report it.")
        if not self.agent.config["allow_commands"]:
            lines.append("Command execution is disabled in this installation: verify by reading files back "
                         "and name the checks the main agent should run.")
        lines += ["", "## Environment"]
        lines.append(f"Host OS: {platform.system() or os.name} ({os.name}). "
                     + ("Use PowerShell syntax and Windows paths in commands."
                        if os.name == "nt" else "Use POSIX shell syntax."))
        lines.append(f"Workspace root: {self.agent.workspace(sid)}. Paths in tools are relative to it, "
                     "with forward slashes.")
        messages = [{"role": "system", "content": "\n".join(lines)},
                    {"role": "user", "content": self.agent.guidance(sid)}]
        if shared_context.strip():
            messages.append({"role": "user", "content": "# Shared context from the main agent (identical for "
                                                        "every worker)\n" + shared_context.strip()})
        return messages

    @staticmethod
    def task_message(task):
        lines = ["# Your task", str(task.get("goal", "")).strip()]
        if task.get("files"):
            lines.append("Files: " + ", ".join(str(f) for f in task["files"]))
        if task.get("context"):
            lines.append("Context: " + str(task["context"]).strip())
        lines.append("Do the work with the tools, then finish with your report.")
        return {"role": "user", "content": "\n".join(lines)}

    def _backend(self, session):
        """A DeepSeek backend on the session's account and model, with the worker's output budget."""
        base = self.agent.deepseek
        options = dict(self.agent.config.values)
        options["model"] = session.get("model") or self.agent.config["model"]
        account_id = session.get("account_id", "deepseek-default")
        options["deepseek_key"] = (self.agent.config["deepseek_key"] if account_id == "deepseek-default"
                                   else self.agent.config["account_keys"].get(account_id, ""))
        options["max_output_tokens"] = self.max_tokens()
        return DeepSeek(options, base.client if isinstance(base, DeepSeek) else self.client)

    # ------------------------------------------------------------------ live state + events
    def _assign_mark(self, sid):
        used = {rec["emoji"] for rec in self.state.get(sid, {}).values()
                if rec["status"] in ("queued", "running")}
        for emoji, color in PALETTE:
            if emoji not in used:
                return emoji, color
        return PALETTE[len(self.state.get(sid, {})) % len(PALETTE)]

    @staticmethod
    def _seconds(rec):
        end = rec.get("finished") or time.time()
        return round(max(0.0, end - rec["started"]), 2) if rec.get("started") else 0.0

    def _public(self, rec):
        return {k: rec.get(k) for k in ("id", "emoji", "color", "goal", "status", "steps", "activity",
                                        "detail", "files", "report", "error", "tokens", "cost_usd",
                                        "cache_hit", "cache_miss", "context_chars", "log")} | {
            "seconds": self._seconds(rec)}

    def _emit(self, sid, phase, rec):
        self.agent.store.event(sid, "subagent", {"phase": phase, **self._public(rec)})

    def _log(self, rec, line):
        rec["log"].append(line[:200])
        del rec["log"][:-LOG_LINES]

    def snapshot(self, sid):
        """Live list plus batch totals, for the panel and the API."""
        records = list(self.state.get(sid, {}).values())
        live = [r for r in records if r["status"] in ("queued", "running")]
        totals = {"count": len(live), "total": len(records),
                  "total_tokens": sum(r.get("tokens") or 0 for r in records),
                  "total_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in records), 6),
                  "total_steps": sum(r.get("steps") or 0 for r in records)}
        return {"subagents": [self._public(r) for r in records], "totals": totals}

    # ------------------------------------------------------------------ spawn / run
    async def spawn(self, session, tasks, shared_context=""):
        sid = session["id"]
        tasks = [t for t in tasks if isinstance(t, dict) and str(t.get("goal", "")).strip()][:MAX_TASKS]
        if not tasks:
            raise ValueError("spawn_subagents requires at least one task with a goal.")
        tools = self.worker_tools(session)
        prefix = self.prefix(session, tools, shared_context)
        self.state.setdefault(sid, {})
        self.tasks.setdefault(sid, {})
        self.dispatch_order[sid] = []
        records = []
        for task in tasks:
            raw = task.get("score")
            priority = float(raw) if isinstance(raw, (int, float)) and 0 <= raw <= 1 else 0.5
            emoji, color = self._assign_mark(sid)
            rec = {"id": "sa_" + secrets.token_hex(4), "emoji": emoji, "color": color,
                   "goal": str(task.get("goal", "")).strip()[:GOAL_CHARS], "status": "queued",
                   "priority": priority, "steps": 0, "activity": None, "detail": "", "files": [],
                   "report": "", "error": None, "started": 0.0, "finished": 0.0, "tokens": 0,
                   "cost_usd": None, "cache_hit": None, "cache_miss": None, "context_chars": 0, "log": []}
            self.state[sid][rec["id"]] = rec
            records.append((rec, task))
            self._emit(sid, "spawn", rec)
        # Higher priority first: with a bounded semaphore the leading tasks acquire slots first.
        records.sort(key=lambda pair: -pair[0]["priority"])
        sem = asyncio.Semaphore(self.concurrency())
        jobs = {}
        for rec, task in records:
            job = asyncio.create_task(self._run_one(session, rec, task, prefix, tools, sem))
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
        return {"spawned": len(compact), "concurrency": self.concurrency(), "results": compact,
                "totals": {"total_tokens": sum(r.get("tokens") or 0 for r in compact),
                           "total_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in compact), 6),
                           "total_steps": sum(r.get("steps") or 0 for r in compact),
                           "files": sorted({f for r in compact for f in r.get("files") or []})},
                "note": "Workers ran the tools themselves: their changes are on disk (reviewed or auto-applied). "
                        "Read the reports, then run the integrated checks yourself before closing the goal."}

    @staticmethod
    def _chars(messages):
        return sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else 0 for m in messages)

    def _fit(self, messages):
        """Keep a long worker conversation sendable: compact stale tool results, never the prefix."""
        if self._chars(messages) <= CONTEXT_CHARS:
            return messages
        fitted, _count, _saved = Agent.compact_tool_results(messages, KEEP_RECENT)
        return fitted

    def _account(self, session, rec, usage):
        if not usage:
            return
        usage.update(provider="deepseek", account_id=session.get("account_id", "deepseek-default"),
                     billing="api", subagent=True)
        self.agent.store.add_usage(session["id"], usage)
        rec["tokens"] += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        if usage.get("cost_usd") is not None:
            rec["cost_usd"] = round((rec["cost_usd"] or 0.0) + usage["cost_usd"], 6)
        for key in ("cache_hit", "cache_miss"):
            value = usage.get(key + "_tokens")
            if value is not None:
                rec[key] = (rec[key] or 0) + value

    async def _run_one(self, session, rec, task, prefix, tools, sem):
        sid = session["id"]
        async with sem:
            self.dispatch_order[sid].append(rec["id"])
            rec["status"], rec["started"] = "running", time.time()
            self._emit(sid, "update", rec)
            # Same session (workspace, approvals, events), separate conversation (read cache).
            worker = dict(session, conversation=f"{sid}/{rec['id']}")
            allowed = {t["function"]["name"] for t in tools}
            messages = list(prefix) + [self.task_message(task)]
            backend = self._backend(session)
            repeats = {}
            try:
                for step in range(1, self.max_steps() + 1):
                    rec["steps"] = step
                    messages = self._fit(messages)
                    rec["context_chars"] = self._chars(messages)
                    message, usage = await backend.complete(messages, tools, self.agent._silent)
                    self._account(session, rec, usage)
                    messages.append(message)
                    calls = message.get("tool_calls") or []
                    if not calls:
                        return self._finish(sid, rec, report=message.get("content") or "")
                    for call in calls:
                        result = await self._call(worker, rec, call, allowed, repeats)
                        content = json.dumps(result, ensure_ascii=False, default=str)
                        if len(content) > RESULT_CHARS:
                            content = json.dumps({"truncated": True, "chars": len(content),
                                                  "output": content[:RESULT_CHARS],
                                                  "note": "Result truncated; narrow the query or read a range."},
                                                 ensure_ascii=False)
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
                    self._emit(sid, "update", rec)
                # The budget is spent: one toolless call reports honestly instead of silence.
                messages.append({"role": "user", "content": SUMMARY_REQUEST})
                message, usage = await backend.complete(self._fit(messages), [], self.agent._silent)
                self._account(session, rec, usage)
                return self._finish(sid, rec, report=(message or {}).get("content") or "", status="exhausted",
                                    error=f"Step budget of {self.max_steps()} model calls spent before the task "
                                          "was finished; the report says where it stopped.")
            except asyncio.CancelledError:
                return self._finish(sid, rec, status="cancelled", error="Cancelled by owner.")
            except (ProviderError, ValueError, OSError) as exc:
                return self._finish(sid, rec, status="failed", error=self.agent.config.redact(exc))

    async def _call(self, worker, rec, call, allowed, repeats):
        """One tool call of a worker, through the session's own Agent.execute."""
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"])
        except ValueError:
            return {"error": "Tool arguments must be valid JSON matching the schema."}
        if name not in allowed:
            return {"error": f"Tool {name} is not available to a worker; finish with a report instead."}
        signature = canonical_call(name, args)
        repeats[signature] = repeats.get(signature, 0) + 1
        if repeats[signature] >= 3:
            return {"error": "Repeated identical call; change approach or finish with a report.", "loop_guard": True}
        detail = str(args.get("path") or args.get("cmd") or args.get("query") or
                     " ".join(args.get("argv") or []) or "")[:120] if isinstance(args, dict) else ""
        rec["activity"], rec["detail"] = name, detail
        self._log(rec, f"{name} {detail}".strip())
        self._emit(worker["id"], "update", rec)
        try:
            result = await self.agent.execute(worker, name, args)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError, ProviderError, TypeError, KeyError, asyncio.TimeoutError) as exc:
            result = {"error": self.agent.config.redact(f"{type(exc).__name__}: {exc}".rstrip(": "))}
            hint = self.agent.error_hint(exc, args)
            if hint:
                result["hint"] = hint
            self._log(rec, f"  ✗ {result['error'][:120]}")
            return result
        if isinstance(result, dict):
            if result.get("written"):
                rec["files"].append(str(result["written"]))
            for changed in result.get("changed") or []:
                rec["files"].append(str(changed))
            if result.get("denied"):
                self._log(rec, f"  ⛔ {name} denied by owner")
            elif result.get("error"):
                self._log(rec, f"  ✗ {str(result['error'])[:120]}")
        return result

    def _finish(self, sid, rec, report="", status="done", error=None):
        rec["finished"] = time.time()
        rec["status"] = status
        rec["report"] = (report or "")[:REPORT_CHARS]
        rec["error"] = error
        rec["activity"], rec["detail"] = None, ""
        rec["files"] = sorted(set(rec["files"]))
        if status == "done" and not rec["report"]:
            rec["status"], rec["error"] = "failed", "Worker finished without a report."
        self._emit(sid, "done", rec)
        out = {"id": rec["id"], "emoji": rec["emoji"], "goal": rec["goal"], "status": rec["status"],
               "report": rec["report"], "files": rec["files"], "steps": rec["steps"],
               "tokens": rec["tokens"], "cost_usd": rec["cost_usd"], "cache_hit": rec["cache_hit"],
               "cache_miss": rec["cache_miss"], "seconds": self._seconds(rec)}
        if rec["error"]:
            out["error"] = rec["error"]
        return out

    # ------------------------------------------------------------------ cancellation
    def cancel(self, sid):
        """Stop every in-flight worker of the session; queued/running ones become cancelled."""
        jobs = self.tasks.get(sid, {})
        stopped = 0
        for job in list(jobs.values()):
            if not job.done():
                job.cancel()
                stopped += 1
        for rec in self.state.get(sid, {}).values():
            if rec["status"] in ("queued", "running"):
                rec.update(status="cancelled", finished=time.time(), activity=None,
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
