"""SSH extension: argv construction, approval gating and process handling with a fake ssh client."""
import stat
import sys
from unittest.mock import AsyncMock

import pytest

from connector.agent import Agent
from connector.config import Config
from connector.ssh_tools import SSHTools
from connector.store import Store

HOST = {"id": "prod", "host": "1.2.3.4", "port": 2222, "user": "ubuntu",
        "identity": "C:/Users/me/.ssh/id_ed25519", "label": "Prod server", "allow_commands": True}

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake POSIX shebang client")


class Approve:
    """Stand-in for Agent.approve: records the review request and answers without a UI."""

    def __init__(self, answer=True):
        self.answer, self.calls = answer, []

    async def __call__(self, session, name, detail, admin_only=False):
        self.calls.append({"name": name, "detail": detail, "admin_only": admin_only})
        return self.answer


@pytest.fixture
def agent(tmp_path):
    store = Store(tmp_path / "test.db")
    agent = Agent(Config(tmp_path), store, AsyncMock(), AsyncMock())
    agent.config.values["ssh_binary"] = "/opt/openssh/ssh"
    agent.approve = Approve()
    yield agent
    store.db.close()


@pytest.fixture
def session(agent):
    return agent.store.resolve(0, 0, 1)


def fake_client(folder, name, body):
    """A stand-in ssh/scp: prints its argv, then runs the given body (exit code or sleep)."""
    script = folder / name
    script.write_text("#!/usr/bin/env python3\nimport sys, time\n"
                      'sys.stdout.write("ARGV " + " ".join(sys.argv[1:]) + "\\n")\n'
                      "sys.stdout.flush()\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def notices(agent, sid):
    return [e["payload"]["text"] for e in agent.store.events(sid) if e["kind"] == "notice"]


# ---------------------------------------------------------------- offering and argv (no subprocess)

def test_tools_are_hidden_until_a_host_is_configured(agent, session):
    ssh = SSHTools(agent)
    assert ssh.tools(session) == []
    agent.config.values["ssh_hosts"] = [HOST]
    assert [t["function"]["name"] for t in ssh.tools(session)] == [
        "ssh_hosts", "ssh_exec", "ssh_upload", "ssh_download"]


def test_malformed_and_option_like_hosts_are_ignored(agent):
    agent.config.values["ssh_hosts"] = [
        HOST, "not-a-dict", {"id": "", "host": "a"}, {"id": "b", "host": ""},
        {"id": "-oProxyCommand=x", "host": "h"}, {"id": "c", "host": "-oProxyCommand=x"},
        {"id": "d", "host": "h", "user": "-x"}, {"id": "e", "host": "h", "port": 0},
        {"id": "f", "host": "h with space"}]
    assert [h["id"] for h in SSHTools(agent).hosts()] == ["prod"]


def test_ssh_argv_is_batch_mode_with_port_identity_and_command(agent):
    agent.config.values["ssh_hosts"] = [HOST]
    ssh = SSHTools(agent)
    argv = ssh.ssh_argv(ssh.host("prod"), "uptime && df -h", 120)
    assert argv[0] == "/opt/openssh/ssh"
    assert argv[-2:] == ["ubuntu@1.2.3.4", "uptime && df -h"]
    assert argv[-4:-2] == ["-p", "2222"]
    for option in ("BatchMode=yes", "StrictHostKeyChecking=accept-new", "ConnectTimeout=30",
                   "ServerAliveInterval=15", "IdentitiesOnly=yes"):
        assert option in argv
    assert argv[argv.index("-i") + 1] == HOST["identity"]
    assert "ConnectTimeout=10" in ssh.ssh_argv(ssh.host("prod"), "true", 10)


def test_scp_argv_uses_capital_port_and_remote_side(agent, tmp_path):
    agent.config.values["ssh_hosts"] = [HOST]
    agent.config.values["ssh_binary"] = str(fake_client(tmp_path, "ssh", "sys.exit(0)"))
    fake_client(tmp_path, "scp", "sys.exit(0)")
    ssh = SSHTools(agent)
    argv = ssh.scp_argv(ssh.host("prod"), "/local/file.txt", "ubuntu@1.2.3.4:/srv/file.txt", 120)
    assert argv[0].endswith("scp")
    assert argv[-4:] == ["-P", "2222", "/local/file.txt", "ubuntu@1.2.3.4:/srv/file.txt"]


async def test_unknown_host_and_disabled_commands(agent, session):
    agent.config.values["ssh_hosts"] = [HOST | {"allow_commands": False}]
    ssh = SSHTools(agent)
    with pytest.raises(ValueError, match="Unknown ssh host"):
        await ssh.execute(session, "ssh_exec", {"host_id": "staging", "command": "id"})
    result = await ssh.execute(session, "ssh_exec", {"host_id": "prod", "command": "id"})
    assert result == {"error": "Commands are disabled for this host in settings."}
    assert not agent.approve.calls


async def test_ssh_hosts_listing_needs_no_approval(agent, session):
    agent.config.values["ssh_hosts"] = [HOST]
    result = await SSHTools(agent).execute(session, "ssh_hosts", {})
    assert result["hosts"] == [{"id": "prod", "label": "Prod server",
                                "address": "ubuntu@1.2.3.4:2222", "allow_commands": True}]
    assert not agent.approve.calls


async def test_remote_path_and_workspace_paths_are_validated(agent, session):
    agent.config.values["ssh_hosts"] = [HOST]
    ssh = SSHTools(agent)
    (agent.workspace(session["id"]) / "note.txt").write_text("x", encoding="utf-8")
    for args in ({"host_id": "prod", "path": "note.txt", "remote_path": "/srv/a\nrm -rf /"},
                 {"host_id": "prod", "path": "note.txt", "remote_path": "-oProxyCommand=x"},
                 {"host_id": "prod", "path": "../escape.txt", "remote_path": "/srv/a"},
                 {"host_id": "prod", "path": "/etc/passwd", "remote_path": "/srv/a"}):
        with pytest.raises(ValueError):
            await ssh.execute(session, "ssh_upload", args)
    assert not agent.approve.calls


# ---------------------------------------------------------------- execution against a fake client

@posix_only
async def test_denied_approval_never_starts_ssh(agent, session, tmp_path):
    agent.config.values["ssh_hosts"] = [HOST]
    agent.config.values["ssh_binary"] = str(fake_client(tmp_path, "ssh", "sys.exit(0)"))
    agent.approve = Approve(False)
    result = await SSHTools(agent).execute(session, "ssh_exec", {"host_id": "prod", "command": "id"})
    assert result == {"denied": True}
    assert agent.approve.calls[0]["admin_only"] is True
    assert agent.approve.calls[0]["detail"].startswith("ubuntu@1.2.3.4$ id")


@posix_only
async def test_exec_returns_merged_output_and_exit_code(agent, session, tmp_path):
    agent.config.values["ssh_hosts"] = [HOST]
    agent.config.values["ssh_binary"] = str(fake_client(tmp_path, "ssh", 'sys.stderr.write("warn\\n")\nsys.exit(3)'))
    result = await SSHTools(agent).execute(session, "ssh_exec", {"host_id": "prod", "command": "id"})
    assert result["exit_code"] == 3 and not result["timed_out"] and result["host_id"] == "prod"
    assert "BatchMode=yes" in result["output"] and "ubuntu@1.2.3.4 id" in result["output"]
    assert "warn" in result["output"]


@posix_only
async def test_exit_255_emits_an_unreachable_notice(agent, session, tmp_path):
    agent.config.values["ssh_hosts"] = [HOST]
    agent.config.values["ssh_binary"] = str(fake_client(tmp_path, "ssh", "sys.exit(255)"))
    result = await SSHTools(agent).execute(session, "ssh_exec", {"host_id": "prod", "command": "id"})
    assert result["exit_code"] == 255 and "authorized" in result["hint"]
    assert any("255" in text for text in notices(agent, session["id"]))


@posix_only
async def test_timeout_kills_ssh_and_keeps_partial_output(agent, session, tmp_path):
    agent.config.values["ssh_hosts"] = [HOST]
    agent.config.values["ssh_binary"] = str(fake_client(tmp_path, "ssh", "time.sleep(60)"))
    result = await SSHTools(agent).execute(
        session, "ssh_exec", {"host_id": "prod", "command": "sleep 60", "timeout_seconds": 5})
    assert result["timed_out"] is True and result["exit_code"] != 0
    assert "ARGV" in result["output"]


@posix_only
async def test_download_over_the_size_cap_is_removed(agent, session, tmp_path):
    agent.config.values["ssh_hosts"] = [HOST]
    target = agent.workspace(session["id"]) / "big.bin"
    agent.config.values["ssh_binary"] = str(fake_client(tmp_path, "ssh", "sys.exit(0)"))
    fake_client(tmp_path, "scp",
                f'open({str(target)!r}, "wb").write(b"x" * (51 * 1024 * 1024))\nsys.exit(0)')
    result = await SSHTools(agent).execute(
        session, "ssh_download", {"host_id": "prod", "remote_path": "/srv/big.bin", "path": "big.bin"})
    assert result["downloaded"] is False and "50 MB" in result["error"]
    assert not target.exists()


@posix_only
async def test_upload_sends_the_workspace_file(agent, session, tmp_path):
    agent.config.values["ssh_hosts"] = [HOST]
    (agent.workspace(session["id"]) / "note.txt").write_text("hello", encoding="utf-8")
    agent.config.values["ssh_binary"] = str(fake_client(tmp_path, "ssh", "sys.exit(0)"))
    fake_client(tmp_path, "scp", "sys.exit(0)")
    result = await SSHTools(agent).execute(
        session, "ssh_upload", {"host_id": "prod", "path": "note.txt", "remote_path": "/srv/note.txt"})
    assert result["uploaded"] is True and result["exit_code"] == 0
    assert "ubuntu@1.2.3.4:/srv/note.txt" in result["output"]
    assert agent.approve.calls[0]["name"] == "ssh_upload prod"
