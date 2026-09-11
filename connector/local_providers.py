"""Official local provider bridges. Browser cookies are never read or copied."""

import asyncio
import json
import os
import shutil
import sys
import subprocess
import time
from pathlib import Path

from .providers import ProviderError


def find_cli(provider):
    found = shutil.which(provider)
    if found:
        return found
    home = Path.home()
    if provider == "codex":
        candidates = list((home / "AppData/Local/OpenAI/Codex/bin").glob("*/codex.exe"))
    elif provider == "claude":
        candidates = list((home / "AppData/Roaming/Claude/claude-code").glob("*/claude.exe"))
        candidates += list((home / ".local/bin").glob("claude*"))
    else:
        return None
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    if provider == "claude":
        try:
            import claude_agent_sdk

            bundled = (
                Path(claude_agent_sdk.__file__).parent
                / "_bundled"
                / ("claude.exe" if os.name == "nt" else "claude")
            )
            if bundled.is_file():
                return str(bundled)
        except ImportError:
            pass
    if provider == "codex":
        bundled = Path(sys.executable).parent / "codex" / ("codex.exe" if os.name == "nt" else "codex")
        if bundled.is_file():
            return str(bundled)
    return None


def process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def account_env(account, runtime):
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        env.pop(name, None)
    if account.get("auth_path"):
        env["CODEX_HOME" if account["provider"] == "codex" else "CLAUDE_CONFIG_DIR"] = account["auth_path"]
    scratch = runtime / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
    env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
        filter(None, [env.get("GIT_CEILING_DIRECTORIES"), str(runtime)])
    )
    return env


class CodexRPC:
    def __init__(self, account, runtime, on_event, on_request):
        self.account, self.runtime = account, runtime
        self.on_event, self.on_request = on_event, on_request
        self.process, self.reader, self.stderr = None, None, None
        self.pending, self.counter = {}, 0
        self.lock = asyncio.Lock()
        self.handlers = set()

    async def start(self):
        async with self.lock:
            if self.process and self.process.returncode is None and self.reader and not self.reader.done():
                return
            exe = find_cli("codex")
            if not exe:
                raise ProviderError("Codex CLI не найден. Установите официальный Codex CLI или Desktop.")
            self.process = await asyncio.create_subprocess_exec(
                exe,
                "app-server",
                "--listen",
                "stdio://",
                cwd=self.runtime,
                env=account_env(self.account, self.runtime),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=32 * 1024 * 1024,
                **process_options(),
            )
            self.reader = asyncio.create_task(self.read())
            self.stderr = asyncio.create_task(self.drain_errors())
            await self.call(
                "initialize",
                {
                    "clientInfo": {"name": "aigent", "title": "AIGent", "version": "0.2.0"},
                    "capabilities": {"experimentalApi": True},
                },
                start=False,
            )
            await self.send({"method": "initialized", "params": {}})

    async def drain_errors(self):
        while self.process and await self.process.stderr.readline():
            pass  # Native stderr can include private account/config paths; don't put it in shared logs.

    async def send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method, params=None, start=True, timeout=90):
        if start:
            await self.start()
        self.counter += 1
        rid = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        await self.send({"id": rid, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(rid, None)

    async def dispatch(self, message):
        if "id" in message:
            try:
                result = await self.on_request(self.account, message)
                await self.send({"id": message["id"], "result": result})
            except Exception:
                await self.send(
                    {
                        "id": message["id"],
                        "error": {"code": -32603, "message": "AIGent could not complete this request"},
                    }
                )
        else:
            await self.on_event(self.account, message)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                ProviderError(message["error"].get("message", "Codex RPC error"))
                            )
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    task = asyncio.create_task(self.dispatch(message))
                    self.handlers.add(task)
                    task.add_done_callback(self.handlers.discard)
        finally:
            await self.on_event(self.account, {"method": "aigent/disconnected", "params": {}})
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ProviderError("Соединение с Codex завершилось."))

    async def close(self):
        for task in list(self.handlers):
            task.cancel()
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr):
            if task:
                task.cancel()


class LocalProviders:
    def __init__(self, agent):
        self.agent, self.store, self.config = agent, agent.store, agent.config
        self.rpcs, self.routes, self.turns, self.done, self.streams, self.clients = {}, {}, {}, {}, {}, {}
        self.snapshots, self.logins = {}, {}
        self.previews = {}

    def rpc(self, account):
        aid = account["id"]
        if aid not in self.rpcs:
            self.rpcs[aid] = CodexRPC(account, self.config.root, self.codex_event, self.codex_request)
        return self.rpcs[aid]

    def event(self, session, kind, payload):
        self.store.event(session["id"], kind, payload | {"provider": session["provider"]})

    async def publish_stream(self, session, payload):
        self.event(session, "stream", payload)
        if not session["chat_id"]:
            return
        key = (session["id"], payload["id"])
        state = self.previews.setdefault(key, {"last": 0, "message_id": None})
        if time.monotonic() - state["last"] < 3 and not payload.get("done"):
            return
        state["last"] = time.monotonic()
        text = session["provider"].capitalize() + " · поток\n"
        if payload.get("reasoning"):
            text += "Размышления:\n" + payload["reasoning"][-600:] + "\n\n"
        text += payload.get("text", "")[-900:] or "Агент работает…"
        if payload.get("done"):
            text = session["provider"].capitalize() + " · поток завершён. Полные события сохранены в AIGent."
        try:
            if state["message_id"]:
                await self.agent.telegram.call(
                    "editMessageText",
                    {"chat_id": session["chat_id"], "message_id": state["message_id"], "text": text},
                )
            elif not payload.get("done"):
                sent = await self.agent.telegram.text(session, text)
                state["message_id"] = sent["message_id"]
        except ProviderError:
            pass
        if payload.get("done"):
            self.previews.pop(key, None)

    async def codex_request(self, account, message):
        params, method = message.get("params", {}), message["method"]
        session = self.routes.get((account["id"], params.get("threadId")))
        if not session:
            if "requestApproval" in method:
                return {"decision": "decline"}
            raise ValueError("Request has no owned AIGent session")
        if "requestApproval" in method:
            accepted = await self.agent.approve(
                session,
                "Codex · " + method,
                json.dumps(params, ensure_ascii=False, indent=2),
                admin_only="command" in method.lower(),
            )
            return {"decision": "accept" if accepted else "decline"}
        if "requestUserInput" in method:
            result = await self.agent.ask(session, params.get("questions", []))
            return {
                "answers": {
                    key: {"answers": value if isinstance(value, list) else [str(value)]}
                    for key, value in result.items()
                }
            }
        raise ValueError("Unsupported native request; not approved")

    async def codex_event(self, account, message):
        params, method = message.get("params", {}), message["method"]
        if method == "aigent/disconnected":
            for (aid, _), session in self.routes.items():
                future = self.done.get(session["id"])
                if aid == account["id"] and future and not future.done():
                    future.set_exception(
                        ProviderError(
                            "Codex отключился во время хода. История сохранена; можно повторить запрос."
                        )
                    )
            return
        if method == "account/rateLimits/updated":
            self.snapshots.setdefault(account["id"], {})["limits"] = params
            return
        session = self.routes.get((account["id"], params.get("threadId")))
        if not session:
            return
        sid = session["id"]
        if method == "turn/started":
            self.turns[sid] = params["turn"]["id"]
        elif method in (
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
        ):
            iid = params.get("itemId", self.turns.get(sid, "stream"))
            stream = self.streams.setdefault((sid, iid), {"id": iid, "text": "", "reasoning": ""})
            stream["reasoning" if "/reasoning/" in method else "text"] += params.get("delta", "")
            now = time.monotonic()
            if now - stream.get("last_emit", 0) > 0.5:
                stream["last_emit"] = now
                await self.publish_stream(session, {k: v for k, v in stream.items() if k != "last_emit"})
        elif method == "item/started":
            item = params["item"]
            if item["type"] not in ("userMessage", "agentMessage", "reasoning"):
                self.event(session, "tool", {"name": item["type"], "arguments": item, "call_id": item["id"]})
        elif method == "item/commandExecution/outputDelta":
            self.event(session, "terminal", {"id": params.get("itemId"), "delta": params.get("delta", "")})
        elif method == "item/completed":
            item = params["item"]
            stream = self.streams.pop((sid, item["id"]), None)
            if stream:
                await self.publish_stream(
                    session, {k: v for k, v in stream.items() if k != "last_emit"} | {"done": True}
                )
            if item["type"] == "agentMessage" and item.get("text"):
                await self.agent.tell(session, item["text"])
            elif item["type"] not in ("userMessage", "reasoning"):
                self.event(session, "tool_result", {"name": item["type"], "result": item, "call_id": item["id"]})
        elif method == "thread/tokenUsage/updated":
            raw = params["tokenUsage"]
            fingerprint = json.dumps(raw["total"], sort_keys=True)
            key = "usage-codex:" + str(params["threadId"]) + ":" + str(params.get("turnId"))
            if self.store.get_state(key, "") != fingerprint:
                self.store.set_state(key, fingerprint)
                last = raw["last"]
                self.store.add_usage(
                    sid,
                    {
                        "provider": "codex",
                        "account_id": account["id"],
                        "billing": "subscription",
                        "model": session.get("model") or "Codex default",
                        "prompt_tokens": last["inputTokens"],
                        "completion_tokens": last["outputTokens"],
                        "cache_hit_tokens": last["cachedInputTokens"],
                        "cache_miss_tokens": max(0, last["inputTokens"] - last["cachedInputTokens"]),
                        "reasoning_tokens": last.get("reasoningOutputTokens", 0),
                        "cost_usd": None,
                        "saved_usd": None,
                        "unpriced_requests": 1,
                        "raw": raw,
                    },
                )
        elif method == "turn/plan/updated":
            self.event(session, "plan", params)
        elif method == "turn/diff/updated":
            self.event(session, "diff", {"text": params.get("diff", "")})
        elif method == "turn/completed":
            future = self.done.get(sid)
            if future and not future.done():
                future.set_result(params["turn"])
        elif method == "error":
            self.event(session, "error", {"text": self.config.redact(params.get("error", params))})

    async def run_codex(self, session, content, account):
        rpc = self.rpc(account)
        options = {
            "cwd": str(self.agent.workspace(session["id"])),
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "workspace-write",
        }
        if session.get("model"):
            options["model"] = session["model"]
        if session.get("external_id"):
            result = await rpc.call("thread/resume", options | {"threadId": session["external_id"]})
        else:
            result = await rpc.call("thread/start", options | {"serviceName": "aigent"})
        thread = result["thread"]
        session = self.store.update_session(
            session["id"], external_id=thread["id"], model=result.get("model") or session.get("model", "")
        )
        self.routes[(account["id"], thread["id"])] = session
        self.event(session, "provider_session", {"external_id": thread["id"], "account_id": account["id"]})
        inputs = self.codex_input(content)
        self.done[session["id"]] = asyncio.get_running_loop().create_future()
        result = await rpc.call(
            "turn/start", {"threadId": thread["id"], "input": inputs, "effort": session["effort"]}
        )
        self.turns[session["id"]] = result["turn"]["id"]
        try:
            result = await self.done[session["id"]]
            if result.get("error"):
                raise ProviderError(str(result["error"]))
        except asyncio.CancelledError:
            await rpc.call("turn/interrupt", {"threadId": thread["id"], "turnId": self.turns[session["id"]]})
            raise
        finally:
            self.done.pop(session["id"], None)
            self.turns.pop(session["id"], None)

    @staticmethod
    def codex_input(content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        return [
            {"type": "text", "text": p["text"]}
            if p["type"] == "text"
            else {"type": "image", "url": p["image_url"]["url"]}
            for p in content
        ]

    async def steer(self, session, text):
        turn = self.turns.get(session["id"])
        if session["provider"] != "codex" or not turn:
            raise ValueError("Steering is available for an active Codex turn")
        result = await self.rpc(self.store.account(session["account_id"])).call(
            "turn/steer",
            {"threadId": session["external_id"], "expectedTurnId": turn, "input": self.codex_input(text)},
        )
        self.event(session, "user", {"text": text})
        return result

    async def run_claude(self, session, content, account):
        from .claude_cli import ClaudeCLI

        client = ClaudeCLI(self, session, account)
        self.clients[session["id"]] = client
        try:
            await client.run(content)
        except asyncio.CancelledError:
            await client.interrupt()
            raise
        finally:
            await client.close()

    async def run(self, session, content):
        sid = session["id"]
        self.event(
            session,
            "user",
            {
                "text": content
                if isinstance(content, str)
                else "\n".join(p["text"] for p in content if p["type"] == "text")
            },
        )
        try:
            account = self.store.account(session["account_id"])
            if not account or account["provider"] != session["provider"]:
                raise ValueError("Provider account is missing")
            status = await self.status(account)
            if not status.get("connected"):
                raise ProviderError(
                    status.get("error") or "Выполните вход для этого аккаунта в разделе «Аккаунты и лимиты»."
                )
            if session["provider"] == "codex":
                await self.run_codex(session, content, account)
            elif session["provider"] == "claude":
                await self.run_claude(session, content, account)
            else:
                raise ValueError("Unknown provider")
        except asyncio.CancelledError:
            client = self.clients.get(sid)
            if client:
                try:
                    await client.interrupt()
                except Exception:
                    pass
            self.event(session, "notice", {"text": "Ход остановлен"})
        except Exception as exc:
            try:
                await self.agent.tell(session, self.config.redact(exc), "error")
            except ProviderError:
                pass
        finally:
            self.clients.pop(sid, None)
            self.store.execute("UPDATE sessions SET status='idle' WHERE id=?", (sid,))
            self.store.event(sid, "turn_completed", {"provider": session["provider"]})

    async def status(self, account, refresh=False):
        old = self.snapshots.get(account["id"], {})
        if not refresh and time.time() - old.get("checked_at", 0) < 60:
            return old
        result = {
            "provider": account["provider"],
            "checked_at": time.time(),
            "connected": False,
            "source": "official local CLI",
            "limits": old.get("limits"),
        }
        try:
            if account["provider"] == "codex":
                rpc = self.rpc(account)
                info = await rpc.call("account/read", {"refreshToken": False})
                result.update(connected=bool(info.get("account")), account=info.get("account"))
                if result["connected"]:
                    result["limits"] = await rpc.call("account/rateLimits/read")
                result["models"] = (await rpc.call("model/list", {"includeHidden": False})).get("data", [])
            elif account["provider"] == "claude":
                exe = find_cli("claude")
                if not exe:
                    raise ProviderError("Claude Code CLI not found")
                proc = await asyncio.create_subprocess_exec(
                    exe,
                    "auth",
                    "status",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=account_env(account, self.config.root),
                    **process_options(),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), 20)
                info = json.loads(stdout)
                result.update(
                    connected=info.get("loggedIn", False),
                    account=info,
                    models=[
                        {"id": "sonnet", "displayName": "Sonnet"},
                        {"id": "opus", "displayName": "Opus"},
                        {"id": "haiku", "displayName": "Haiku"},
                    ],
                )
        except Exception as exc:
            result["error"] = self.config.redact(exc)
        expected = json.loads(account.get("metadata") or "{}").get("expected_email")
        identity = (result.get("account") or {}).get("email")
        if expected and identity:
            result["identity_verified"] = expected.casefold() == identity.casefold()
            if not result["identity_verified"]:
                result["connected"] = False
                result["error"] = (
                    "Вход выполнен как " + identity + "; для этого подключения требуется " + expected
                )
        self.snapshots[account["id"]] = result
        return result

    async def close(self):
        await asyncio.gather(*(rpc.close() for rpc in self.rpcs.values()), return_exceptions=True)

    async def claude_history(self, account, external_id=None):
        args = [] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve().parents[1] / "run.py")]
        args += ["--helper", "claude-history"]
        if external_id:
            args += ["--external-id", external_id]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *args,
            cwd=self.config.root,
            env=account_env(account, self.config.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options(),
        )
        output, _ = await asyncio.wait_for(process.communicate(), 30)
        if process.returncode:
            raise ProviderError("Не удалось прочитать сессии этого профиля Claude")
        return json.loads(output)
