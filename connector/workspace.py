"""Workspace editing, review, Git and bounded streaming command processes."""

import asyncio
import difflib
import hashlib
import os
import secrets
import time
from pathlib import Path

from .agent import safe_path, tool, STRING
from .local_providers import process_options

EXTRA_TOOLS = [
    tool(
        "exec_command",
        "Run a shell command in the workspace after administrator approval. Returns output and a session_id for running commands.",
        {"cmd": STRING, "workdir": STRING, "yield_time_ms": {"type": "integer"}},
        ["cmd"],
    ),
    tool(
        "write_stdin",
        "Continue a running command; empty chars reads output. Nonempty input requires review.",
        {"session_id": STRING, "chars": STRING},
        ["session_id"],
    ),
    tool(
        "apply_patch",
        "Apply Codex-style *** Begin Patch / *** Add File / *** Update File / *** Delete File / *** End Patch edits. Update hunks use @@ and space/+/- lines. Exact changes are reviewed before application.",
        {"patch": STRING},
        ["patch"],
    ),
    tool(
        "update_plan",
        "Show and update a concise task plan.",
        {
            "plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": STRING,
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["step", "status"],
                },
            }
        },
        ["plan"],
    ),
    tool(
        "request_user_input",
        "Ask the user for missing requirements and wait for answers.",
        {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": STRING, "question": STRING},
                    "required": ["id", "question"],
                },
            }
        },
        ["questions"],
    ),
]


def revision(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def parse_patch(root, patch):
    if len(patch) > 500000:
        raise ValueError("Patch too large")
    lines = patch.strip().splitlines()
    if len(lines) < 2 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Use *** Begin Patch and *** End Patch markers")
    plans, index = {}, 1

    def register(name, new):
        path = safe_path(root, name)
        if name in plans:
            raise ValueError("Each path may appear only once in a patch")
        if path.exists() and path.stat().st_size > 500000:
            raise ValueError("File exceeds patch size limit")
        old = path.read_text(encoding="utf-8") if path.exists() else None
        plans[name] = {"path": path, "old": old, "new": new, "revision": revision(path)}

    while index < len(lines) - 1:
        header = lines[index]
        index += 1
        if header.startswith("*** Add File: "):
            name = header.removeprefix("*** Add File: ")
            if safe_path(root, name).exists():
                raise ValueError("Add File target already exists")
            content = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                if not lines[index].startswith("+"):
                    raise ValueError("Add File lines must start with +")
                content.append(lines[index][1:])
                index += 1
            register(name, "\n".join(content) + ("\n" if content else ""))
        elif header.startswith("*** Delete File: "):
            name = header.removeprefix("*** Delete File: ")
            if not safe_path(root, name).is_file():
                raise ValueError("Delete File target not found")
            register(name, None)
        elif header.startswith("*** Update File: "):
            name = header.removeprefix("*** Update File: ")
            path = safe_path(root, name)
            old = path.read_text(encoding="utf-8")
            content = old.splitlines()
            target = name
            if lines[index].startswith("*** Move to: "):
                target = lines[index].removeprefix("*** Move to: ")
                index += 1
                if safe_path(root, target).exists():
                    raise ValueError("Move target already exists")
            cursor = 0
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                if not lines[index].startswith("@@"):
                    raise ValueError("Update File requires @@ hunks")
                anchor = lines[index][2:].strip()
                index += 1
                if anchor:
                    positions = [n for n in range(cursor, len(content)) if content[n] == anchor]
                    if not positions:
                        raise ValueError("Hunk anchor not found")
                    cursor = positions[0] + 1
                before, after = [], []
                while index < len(lines) - 1 and not lines[index].startswith(("@@", "*** ")):
                    line = lines[index]
                    if not line or line[0] not in " +-":
                        raise ValueError("Hunk lines must start with space, + or -")
                    if line[0] in " -":
                        before.append(line[1:])
                    if line[0] in " +":
                        after.append(line[1:])
                    index += 1
                eof = index < len(lines) - 1 and lines[index] == "*** End of File"
                if eof:
                    index += 1
                matches = [
                    n
                    for n in range(cursor, len(content) - len(before) + 1)
                    if content[n : n + len(before)] == before and (not eof or n + len(before) == len(content))
                ]
                if not before:
                    matches = [len(content)]
                if len(matches) != 1:
                    raise ValueError("Patch context missing or ambiguous; include more surrounding lines")
                pos = matches[0]
                content[pos : pos + len(before)] = after
                cursor = pos + len(after)
            new = "\n".join(content) + ("\n" if old.endswith("\n") and content else "")
            if target != name:
                register(name, None)
            register(target, new)
        else:
            raise ValueError("Unknown patch operation: " + header[:100])
    if not plans:
        raise ValueError("Empty patch")
    return plans


class WorkspaceService:
    def __init__(self, agent):
        self.agent, self.store = agent, agent.store
        self.processes = {}

    def files(self, sid):
        root = self.agent.workspace(sid)
        result = []
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if d not in (".git", ".venv", ".local", "node_modules", ".aigent", "__pycache__")
                and not (Path(base) / d).is_symlink()
            ]
            for name in files:
                if len(result) >= 2000:
                    return result
                try:
                    path = safe_path(root, (Path(base) / name).relative_to(root).as_posix())
                    if path.is_file():
                        result.append(
                            {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size}
                        )
                except ValueError:
                    continue
        return result

    def read(self, sid, name):
        path = safe_path(self.agent.workspace(sid), name)
        if not path.is_file() or path.stat().st_size > 1000000:
            raise ValueError("Select a text file up to 1 MB")
        return {"path": name, "text": path.read_text(encoding="utf-8"), "revision": revision(path)}

    def save(self, sid, name, text, expected):
        root = self.agent.workspace(sid)
        path = safe_path(root, name)
        if revision(path) != expected:
            raise ValueError("File changed on disk; reload before saving")
        if len(text.encode()) > 1000000:
            raise ValueError("File exceeds 1 MB")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.agent.read_cache.pop(sid, None)
        self.store.event(sid, "file_saved", {"path": name, "revision": revision(path)})
        return self.read(sid, name)

    async def git(self, sid, args):
        root = self.agent.workspace(sid)
        probe = await asyncio.create_subprocess_exec("git", "-C", str(root), "rev-parse", "--show-toplevel",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **process_options())
        top, error = await asyncio.wait_for(probe.communicate(), 10)
        if probe.returncode:
            raise ValueError("В этой рабочей папке нет Git-репозитория.")
        if Path(top.decode("utf-8").strip()).resolve() != root.resolve():
            raise ValueError("Откройте корневую папку Git-проекта. Родительский репозиторий не изменяется из вложенной рабочей папки.")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), 30)
        if proc.returncode:
            raise ValueError(err.decode("utf-8", errors="replace")[:1500])
        return out.decode("utf-8", errors="replace")

    async def git_status(self, sid):
        status, branch, diff = await asyncio.gather(
            self.git(sid, ["status", "--porcelain=v1", "-z"]),
            self.git(sid, ["branch", "--show-current"]),
            self.git(sid, ["diff", "--stat"]),
        )
        changes, entries, index = [], status.split("\0"), 0
        while index < len(entries):
            item = entries[index]
            index += 1
            if not item:
                continue
            changes.append({"status": item[:2], "path": item[3:]})
            if item[0] in "RC" and index < len(entries):
                index += 1
        return {"branch": branch.strip(), "changes": changes, "stat": diff}

    async def start_command(self, session, command, workdir="."):
        root = self.agent.workspace(session["id"])
        cwd = safe_path(root, workdir)
        if not cwd.is_dir():
            raise ValueError("Working directory not found")
        tid = secrets.token_hex(8)
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        }
        temp = root / ".aigent/tmp"
        temp.mkdir(parents=True, exist_ok=True)
        env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), PYTHONUNBUFFERED="1")
        env["GIT_CEILING_DIRECTORIES"] = str(root.parent)
        argv = (
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + command,
            ]
            if os.name == "nt"
            else ["/bin/sh", "-lc", command]
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_options(),
            **({"start_new_session": True} if os.name != "nt" else {}),
        )
        item = {
            "id": tid,
            "sid": session["id"],
            "process": proc,
            "text": "",
            "command": command,
            "created": time.time(),
            "exit_code": None,
        }
        self.processes[tid] = item
        self.store.event(
            session["id"], "terminal", {"id": tid, "command": command, "delta": "$ " + command + "\n"}
        )
        item["reader"] = asyncio.create_task(self.collect(item))
        return tid

    async def collect(self, item):
        try:
            while chunk := await item["process"].stdout.read(4096):
                text = chunk.decode("utf-8", errors="replace")
                if len(item["text"]) < 200000:
                    text = text[: 200000 - len(item["text"])]
                    item["text"] += text
                    self.store.event(
                        item["sid"], "terminal", {"id": item["id"], "delta": self.agent.config.redact(text)}
                    )
            item["exit_code"] = await item["process"].wait()
            self.store.event(
                item["sid"],
                "terminal",
                {
                    "id": item["id"],
                    "exit_code": item["exit_code"],
                    "delta": "\n[exit " + str(item["exit_code"]) + "]\n",
                },
            )
        except asyncio.CancelledError:
            pass

    def command(self, sid, tid):
        item = self.processes.get(tid)
        if not item or item["sid"] != sid:
            raise ValueError("Terminal not found")
        return item

    async def stop_command(self, sid, tid):
        item = self.command(sid, tid)
        proc = item["process"]
        if proc.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    **process_options(),
                )
                await killer.wait()
            else:
                import signal

                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()

    async def execute(self, session, name, args):
        sid = session["id"]
        if name == "update_plan":
            self.store.event(sid, "plan", {"plan": args["plan"]})
            return {"updated": True}
        if name == "request_user_input":
            return {"answers": await self.agent.ask(session, args["questions"])}
        if name == "apply_patch":
            plans = parse_patch(self.agent.workspace(sid), args["patch"])
            diff = "".join(
                "".join(
                    difflib.unified_diff(
                        (p["old"] or "").splitlines(True),
                        (p["new"] or "").splitlines(True),
                        fromfile=n,
                        tofile=n,
                    )
                )
                for n, p in plans.items()
            )
            if not await self.agent.approve(session, "apply_patch", diff):
                return {"denied": True}
            for name, plan in plans.items():
                safe_path(self.agent.workspace(sid), name)
                if revision(plan["path"]) != plan["revision"]:
                    raise ValueError("File changed since patch review")
            for plan in plans.values():
                if plan["new"] is None:
                    trash = self.agent.workspace(sid) / ".aigent/trash" / secrets.token_hex(8)
                    trash.parent.mkdir(parents=True, exist_ok=True)
                    plan["path"].replace(trash)
                else:
                    plan["path"].parent.mkdir(parents=True, exist_ok=True)
                    plan["path"].write_text(plan["new"], encoding="utf-8")
            self.agent.read_cache.pop(sid, None)
            self.store.event(sid, "diff", {"text": diff})
            return {"changed": list(plans)}
        if name == "exec_command":
            if not self.agent.config["allow_commands"]:
                return {"error": "Enable command requests in settings"}
            workdir = safe_path(self.agent.workspace(sid), args.get("workdir", "."))
            if not workdir.is_dir():
                raise ValueError("Working directory does not exist")
            if not await self.agent.approve(session, "exec_command", args["cmd"] + "\ncwd: " + str(workdir), admin_only=True):
                return {"denied": True}
            tid = await self.start_command(session, args["cmd"], args.get("workdir", "."))
            await asyncio.sleep(min(max(args.get("yield_time_ms", 1000), 100), 10000) / 1000)
        elif name == "write_stdin":
            tid = args["session_id"]
            item = self.command(sid, tid)
            if args.get("chars"):
                if not await self.agent.approve(session, "write_stdin", args["chars"], admin_only=True):
                    return {"denied": True}
                item["process"].stdin.write(args["chars"].encode())
                await item["process"].stdin.drain()
            await asyncio.sleep(0.3)
        else:
            raise ValueError("Unknown workspace tool")
        item = self.command(sid, tid)
        return {
            "session_id": tid if item["exit_code"] is None else None,
            "exit_code": item["exit_code"],
            "output": item["text"][-20000:],
        }

    async def close(self):
        await asyncio.gather(
            *(self.stop_command(item["sid"], tid) for tid, item in self.processes.items()),
            return_exceptions=True,
        )
