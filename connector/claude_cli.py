"""Drive the user's unmodified Claude CLI through its JSONL print protocol.

Authentication belongs to the installed CLI. This module never reads browser
cookies, creates Claude OAuth credentials, or calls Anthropic model APIs itself.
"""

import asyncio
import json
import secrets
import time

from .local_providers import account_env, find_cli, process_options
from .providers import ProviderError


class ClaudeCLI:
    def __init__(self, owner, session, account):
        self.owner, self.agent, self.session, self.account = owner, owner.agent, session, account
        self.process = None
        self.controls = set()
        self.stderr_task = None
        self.error_tail = ""

    async def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def drain_stderr(self):
        while chunk := await self.process.stderr.read(4096):
            self.error_tail = (self.error_tail + chunk.decode("utf-8", errors="replace"))[-4000:]

    async def permission(self, message):
        request = message.get("request", {})
        answer = {"behavior": "deny", "message": "Unsupported host request"}
        try:
            if request.get("subtype") == "can_use_tool":
                name, inputs = request["tool_name"], request["input"]
                if name == "AskUserQuestion":
                    questions = [
                        dict(q, id=q.get("question", str(i)))
                        for i, q in enumerate(inputs.get("questions", []))
                    ]
                    answers = await self.agent.ask(self.session, questions)
                    answer = {"behavior": "allow", "updatedInput": inputs | {"answers": answers}}
                else:
                    allowed = await self.agent.approve(
                        self.session,
                        "Claude · " + name,
                        json.dumps(inputs, ensure_ascii=False, indent=2),
                        admin_only=name in ("Bash", "NotebookEdit"),
                    )
                    answer = (
                        {"behavior": "allow", "updatedInput": inputs}
                        if allowed
                        else {"behavior": "deny", "message": "Declined by user"}
                    )
            await self.send(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": answer,
                    },
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.process.returncode is None:
                await self.send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": message["request_id"],
                            "error": "Permission host error",
                        },
                    }
                )

    async def start(self):
        exe = find_cli("claude")
        if not exe:
            raise ProviderError("Установите официальный Claude Code CLI и выполните вход в нём.")
        args = [
            exe,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            "manual",
            "--permission-prompts",
            "host",
            "--permission-prompt-tool",
            "stdio",
        ]
        if self.session.get("external_id"):
            args += ["--resume", self.session["external_id"]]
            if self.session.get("forked"):
                args += ["--fork-session"]
        if self.session.get("model"):
            args += ["--model", self.session["model"]]
        self.process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.agent.workspace(self.session["id"]),
            env=account_env(self.account, self.agent.config.root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024,
            **process_options(),
        )
        self.stderr_task = asyncio.create_task(self.drain_stderr())
        await self.send(
            {
                "type": "control_request",
                "request_id": "aigent-initialize",
                "request": {"subtype": "initialize", "hooks": None},
            }
        )
        async with asyncio.timeout(60) if hasattr(asyncio, "timeout") else _timeout_compat(60):
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if (
                    message.get("type") == "control_response"
                    and message.get("response", {}).get("request_id") == "aigent-initialize"
                ):
                    if message["response"].get("subtype") == "error":
                        raise ProviderError(message["response"].get("error", "CLI initialize failed"))
                    return message["response"].get("response", {})
        raise ProviderError(
            "Claude CLI не завершил инициализацию: " + self.agent.config.redact(self.error_tail)
        )

    async def run(self, content):
        await self.start()
        sid = self.session["id"]
        text = (
            content
            if isinstance(content, str)
            else "\n".join(p["text"] for p in content if p["type"] == "text")
        )
        await self.send(
            {
                "type": "user",
                "session_id": "default",
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": text},
            }
        )
        stream = {"id": secrets.token_hex(8), "text": "", "reasoning": ""}
        last_emit, got_result = 0.0, False
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            kind = message.get("type")
            if kind == "control_request":
                task = asyncio.create_task(self.permission(message))
                self.controls.add(task)
                task.add_done_callback(self.controls.discard)
            elif kind == "system" and message.get("subtype") == "init":
                self.session = self.agent.store.update_session(
                    sid,
                    external_id=message["session_id"],
                    model=message.get("model") or self.session.get("model", ""),
                    forked=False,
                )
                self.owner.event(
                    self.session,
                    "provider_session",
                    {
                        "external_id": message["session_id"],
                        "account_id": self.account["id"],
                        "transport": "official-cli-jsonl",
                    },
                )
            elif kind == "stream_event":
                delta = message.get("event", {}).get("delta", {})
                if delta.get("type") == "text_delta":
                    stream["text"] += delta.get("text", "")
                elif delta.get("type") == "thinking_delta":
                    stream["reasoning"] += delta.get("thinking", "")
                if time.monotonic() - last_emit > 0.5:
                    last_emit = time.monotonic()
                    await self.owner.publish_stream(self.session, dict(stream))
            elif kind == "assistant":
                blocks = message.get("message", {}).get("content", [])
                stream["reasoning"] = stream["reasoning"] or "\n".join(
                    b.get("thinking", "") for b in blocks if b.get("type") == "thinking"
                )
                await self.owner.publish_stream(self.session, stream | {"done": True})
                for block in blocks:
                    if block.get("type") == "text":
                        await self.agent.tell(self.session, block["text"])
                    elif block.get("type") == "tool_use":
                        self.owner.event(
                            self.session,
                            "tool",
                            {
                                "name": block["name"],
                                "call_id": block["id"],
                                "arguments": block["input"],
                                "parent_tool_use_id": message.get("parent_tool_use_id"),
                            },
                        )
                stream = {"id": secrets.token_hex(8), "text": "", "reasoning": ""}
            elif kind == "user":
                content = message.get("message", {}).get("content", [])
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_result":
                        self.owner.event(
                            self.session, "tool_result", {"name": "Claude tool", "result": block, "call_id": block.get("tool_use_id")}
                        )
            elif kind == "result":
                got_result = True
                raw = message.get("usage") or {}
                if "input_tokens" in raw:
                    hit, write = (
                        raw.get("cache_read_input_tokens", 0),
                        raw.get("cache_creation_input_tokens", 0),
                    )
                    self.agent.store.add_usage(
                        sid,
                        {
                            "provider": "claude",
                            "account_id": self.account["id"],
                            "billing": "subscription",
                            "model": self.session.get("model") or "Claude CLI",
                            "prompt_tokens": raw["input_tokens"] + hit + write,
                            "completion_tokens": raw.get("output_tokens", 0),
                            "cache_hit_tokens": hit,
                            "cache_miss_tokens": raw["input_tokens"] + write,
                            "cache_write_tokens": write,
                            "cost_usd": None,
                            "saved_usd": None,
                            "unpriced_requests": 1,
                            "provider_cost_estimate_usd": message.get("total_cost_usd"),
                            "raw": raw,
                        },
                    )
                if message.get("session_id"):
                    self.agent.store.update_session(sid, external_id=message["session_id"])
                if message.get("is_error"):
                    raise ProviderError(
                        message.get("result") or "; ".join(message.get("errors", [])) or "Claude CLI failed"
                    )
                break
            elif kind == "system" and message.get("subtype") in ("api_retry", "permission_denied"):
                self.owner.event(self.session, "notice", {"text": json.dumps(message, ensure_ascii=False)})
        if not got_result:
            raise ProviderError(
                "Claude CLI закрыл поток без результата: " + self.agent.config.redact(self.error_tail)
            )

    async def interrupt(self):
        if self.process and self.process.returncode is None:
            await self.send(
                {
                    "type": "control_request",
                    "request_id": "aigent-interrupt",
                    "request": {"subtype": "interrupt"},
                }
            )

    async def close(self):
        for task in list(self.controls):
            task.cancel()
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.stderr_task:
            self.stderr_task.cancel()


class _timeout_compat:
    """Cancellation deadline for the supported Python 3.10 runtime."""

    def __init__(self, seconds):
        self.seconds = seconds

    async def __aenter__(self):
        task = asyncio.current_task()
        self.handle = asyncio.get_running_loop().call_later(self.seconds, task.cancel)

    async def __aexit__(self, kind, value, traceback):
        self.handle.cancel()
        if kind is asyncio.CancelledError:
            raise TimeoutError("Claude initialization timeout") from None
