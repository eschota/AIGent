"""Remote work over SSH with the system OpenSSH client.

Only the stock `ssh`/`scp` binaries are used (Windows 10+ ships them in
`C:\\Windows\\System32\\OpenSSH\\`), always with key-based authentication and never with an
interactive prompt: `BatchMode=yes` turns a missing or unauthorized key into a plain failure
instead of a password question. Passwords are never accepted, asked for or stored.

Hosts are configured by the owner in settings (`ssh_hosts`); the model may only address a host by
its configured id, so a command can never be pointed at an arbitrary machine. Every command and
every file transfer goes through the same administrator approval as `run_command`.
"""

import asyncio
import os
import shutil
from pathlib import Path

from .agent import STRING, safe_path, tool

OUTPUT_CHARS = 24000
READ_LIMIT = 400000
TRANSFER_LIMIT = 50 * 1024 * 1024
UNREACHABLE = 255


class SSHTools:
    def __init__(self, agent):
        self.agent = agent

    # ------------------------------------------------------------------ configuration
    def hosts(self):
        """Configured hosts, normalized and filtered: a malformed entry is simply not offered."""
        result = []
        for item in self.agent.config.values.get("ssh_hosts") or []:
            if not isinstance(item, dict):
                continue
            host_id = str(item.get("id") or "").strip()
            host = str(item.get("host") or "").strip()
            user = str(item.get("user") or "").strip()
            if not host_id or not host or not safe_token(host_id) or not safe_token(host) or not safe_token(user):
                continue
            raw_port = item.get("port")
            try:
                port = 22 if raw_port in (None, "") else int(raw_port)
            except (TypeError, ValueError):
                continue
            if not 1 <= port <= 65535:
                continue
            result.append({"id": host_id, "host": host, "port": port, "user": user,
                           "identity": str(item.get("identity") or "").strip(),
                           "label": str(item.get("label") or "").strip() or host_id,
                           "allow_commands": bool(item.get("allow_commands", True))})
        return result

    def host(self, host_id):
        for item in self.hosts():
            if item["id"] == host_id:
                return item
        raise ValueError("Unknown ssh host id; call ssh_hosts for the configured ids.")

    def binary(self, name="ssh"):
        """Locate ssh/scp: an explicit setting first, then PATH, then the Windows OpenSSH folder."""
        override = str(self.agent.config.values.get("ssh_binary") or "").strip()
        if override:
            path = Path(override)
            if name == "ssh":
                # Hand back exactly what the owner configured: rewriting separators breaks a
                # path that only the target shell understands (WSL, Cygwin, a wrapper script).
                return override
            sibling = path.with_name(name + path.suffix)
            if sibling.exists():
                return str(sibling)
        found = shutil.which(name)
        if found:
            return found
        windows = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "OpenSSH" / (name + ".exe")
        if windows.exists():
            return str(windows)
        raise ValueError(f"OpenSSH client '{name}' was not found. Install OpenSSH or set ssh_binary in settings.")

    def timeout(self, requested=None):
        default = self.agent.config.values.get("ssh_timeout_seconds") or 120
        try:
            value = int(requested if requested else default)
        except (TypeError, ValueError):
            value = 120
        return min(max(value, 5), 3600)

    @staticmethod
    def target(host):
        return f"{host['user']}@{host['host']}" if host["user"] else host["host"]

    def options(self, host, limit):
        argv = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={min(30, limit)}", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3"]
        if host["identity"]:
            argv += ["-o", "IdentitiesOnly=yes", "-i", host["identity"]]
        return argv

    def ssh_argv(self, host, command, limit):
        return ([self.binary("ssh")] + self.options(host, limit) + ["-p", str(host["port"]),
                self.target(host), command])

    def scp_argv(self, host, source, destination, limit):
        return ([self.binary("scp")] + self.options(host, limit) + ["-P", str(host["port"]),
                source, destination])

    # ------------------------------------------------------------------ extension protocol
    def tools(self, session):
        if not self.hosts():
            return []
        return [
            tool("ssh_hosts", "List the SSH hosts the owner configured. Remote work is only possible "
                 "with these ids.", {}, []),
            tool("ssh_exec", "Run one command on a configured SSH host through its login shell. "
                 "Requires administrator approval. Key-based authentication only; there is no password "
                 "prompt and a command that outlives the timeout is stopped and returns partial output.",
                 {"host_id": STRING, "command": STRING,
                  "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 3600}},
                 ["host_id", "command"]),
            tool("ssh_upload", "Copy a workspace file to a configured SSH host with scp. Requires "
                 "administrator approval.", {"host_id": STRING, "path": STRING, "remote_path": STRING},
                 ["host_id", "path", "remote_path"]),
            tool("ssh_download", "Copy a remote file from a configured SSH host into the workspace with "
                 "scp, at most 50 MB. Requires administrator approval.",
                 {"host_id": STRING, "remote_path": STRING, "path": STRING},
                 ["host_id", "remote_path", "path"]),
        ]

    async def execute(self, session, name, args):
        if name == "ssh_hosts":
            return {"hosts": [{"id": h["id"], "label": h["label"],
                               "address": f"{self.target(h)}:{h['port']}",
                               "allow_commands": h["allow_commands"]} for h in self.hosts()]}
        host = self.host(str(args.get("host_id") or ""))
        if name == "ssh_exec":
            return await self.run_remote(session, host, args)
        if name == "ssh_upload":
            return await self.transfer(session, host, args, upload=True)
        if name == "ssh_download":
            return await self.transfer(session, host, args, upload=False)
        raise ValueError("Unknown ssh tool.")

    # ------------------------------------------------------------------ commands
    async def run_remote(self, session, host, args):
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a nonempty string.")
        if not host["allow_commands"]:
            return {"error": "Commands are disabled for this host in settings."}
        limit = self.timeout(args.get("timeout_seconds"))
        argv = self.ssh_argv(host, command, limit)
        if not await self.agent.approve(session, "ssh_exec " + host["id"],
                                        f"{self.target(host)}$ {command}\nport: {host['port']}\n"
                                        "Runs on the remote host with that account's permissions.", True):
            return {"denied": True}
        code, output, timed_out = await self.spawn(session, argv, limit)
        result = {"host_id": host["id"], "exit_code": code, "output": output, "timed_out": timed_out}
        if timed_out:
            result["note"] = ("Команда остановлена по таймауту; вывод выше — частичный.")
        if code == UNREACHABLE and not timed_out:
            self.agent.store.event(session["id"], "notice", {
                "text": f"SSH {host['id']}: соединение не удалось (код 255). "
                        "Проверьте host, key и что ключ авторизован на сервере.",
                "host_id": host["id"]})
            result["hint"] = "SSH could not connect: check host, key, and that the key is authorized."
        return result

    # ------------------------------------------------------------------ transfers
    async def transfer(self, session, host, args, upload):
        remote = args.get("remote_path")
        if not isinstance(remote, str) or not remote.strip():
            raise ValueError("remote_path must be a nonempty string.")
        if "\n" in remote or "\r" in remote:
            raise ValueError("remote_path must not contain line breaks.")
        if remote.startswith("-"):
            raise ValueError("remote_path must not start with '-'.")
        root = self.agent.workspace(session["id"])
        local = safe_path(root, args.get("path"))
        limit = self.timeout(args.get("timeout_seconds"))
        remote_side = f"{self.target(host)}:{remote}"
        if upload:
            if not local.is_file():
                raise FileNotFoundError("File not found in the workspace: " + str(args.get("path")))
            if local.stat().st_size > TRANSFER_LIMIT:
                raise ValueError("Maximum transfer size is 50 MB.")
            argv = self.scp_argv(host, str(local), remote_side, limit)
            detail = f"upload {args['path']} → {remote_side}\n{local.stat().st_size} bytes"
            name = "ssh_upload " + host["id"]
        else:
            argv = self.scp_argv(host, remote_side, str(local), limit)
            detail = f"download {remote_side} → {args['path']}"
            name = "ssh_download " + host["id"]
        if not await self.agent.approve(session, name, detail, True):
            return {"denied": True}
        if not upload:
            local.parent.mkdir(parents=True, exist_ok=True)
        code, output, timed_out = await self.spawn(session, argv, limit)
        result = {"host_id": host["id"], "exit_code": code, "output": output, "timed_out": timed_out,
                  "remote_path": remote, "path": args["path"]}
        if code == UNREACHABLE and not timed_out:
            self.agent.store.event(session["id"], "notice", {
                "text": f"SSH {host['id']}: соединение не удалось (код 255). "
                        "Проверьте host, key и что ключ авторизован на сервере.",
                "host_id": host["id"]})
            result["hint"] = "SSH could not connect: check host, key, and that the key is authorized."
        if upload:
            result["uploaded"] = code == 0
            return result
        if local.is_file() and local.stat().st_size > TRANSFER_LIMIT:
            local.unlink()
            return result | {"downloaded": False,
                             "error": "Downloaded file exceeds 50 MB and was removed."}
        result["downloaded"] = code == 0 and local.is_file()
        if local.is_file():
            result["bytes"] = local.stat().st_size
        self.agent.read_cache.pop(session["id"], None)
        return result

    # ------------------------------------------------------------------ process
    async def spawn(self, session, argv, limit):
        """Run an OpenSSH binary without a shell; a timeout kills the tree and keeps partial output."""
        env = {k: v for k, v in os.environ.items()
               if not any(x in k.upper() for x in ("TOKEN", "SECRET", "KEY", "PASSWORD"))}
        env["SSH_ASKPASS_REQUIRE"] = "never"
        env.pop("SSH_ASKPASS", None)
        root = self.agent.workspace(session["id"])
        chunks, total, timed_out = [], 0, False
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=root, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        async def pump():
            nonlocal total
            while True:
                chunk = await process.stdout.read(8192)
                if not chunk:
                    break
                if total < READ_LIMIT:
                    chunks.append(chunk)
                    total += len(chunk)

        reader = asyncio.create_task(pump())
        try:
            await asyncio.wait_for(process.wait(), limit)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            if process.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                else:
                    process.kill()
                await process.wait()
            await asyncio.gather(reader, return_exceptions=True)
        text = b"".join(chunks).decode("utf-8", errors="replace")[:OUTPUT_CHARS]
        return process.returncode, self.agent.config.redact(text), timed_out


def safe_token(value):
    """Host, user and id come from settings but still must not become an ssh option or a new line."""
    return not value.startswith("-") and not any(c in value for c in "\r\n \t")
