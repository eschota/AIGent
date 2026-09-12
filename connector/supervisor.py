"""Keep the connector alive while it rewrites its own code.

AIGent edits its own source, so a syntax error or a crashing import must not leave the owner
without a workspace to fix it from. The supervisor runs the server in a child process and:

* restarts it when it exits unexpectedly, with a growing delay so a boot loop cannot spin;
* remembers the last revision that actually served traffic and rolls back to it when a fresh
  revision cannot start, so a broken self-edit degrades to the previous working code;
* writes every decision to `.local/supervisor.log` and to the state file the interface reads.

`python run.py --supervise` uses it. A single run (`python run.py`) is unchanged.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WATCHED = ("connector", "run.py")
HEALTHY_SECONDS = 25  # a child that serves this long is considered a good revision
MAX_BACKOFF = 60
ROLLBACK_AFTER = 2  # consecutive early exits of a new revision before restoring the snapshot
GIVE_UP_AFTER = 5  # early exits with no known-good revision before we stop, not loop forever


def revision(root: Path):
    """Content signature of the code the child will import."""
    import hashlib

    digest = hashlib.sha256()
    for name in WATCHED:
        target = root / name
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            if "__pycache__" in path.parts or not path.is_file():
                continue
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def snapshot(root: Path, store: Path, rev: str):
    """Copy the known-good code aside so a broken edit can be undone without git."""
    target = store / rev
    if target.exists():
        return target
    for name in WATCHED:
        source = root / name
        destination = target / name
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for old in sorted(store.iterdir(), key=lambda p: p.stat().st_mtime)[:-3]:
        if old.is_dir() and old.name != rev:
            shutil.rmtree(old, ignore_errors=True)
    return target


def restore(root: Path, saved: Path):
    for name in WATCHED:
        source = saved / name
        if not source.exists():
            continue
        destination = root / name
        if source.is_dir():
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


class Supervisor:
    def __init__(self, root: Path, data: Path, argv):
        self.root, self.data, self.argv = root, data, argv
        self.state_path = data / "supervisor.json"
        self.log_path = data / "supervisor.log"
        self.snapshots = data / "snapshots"
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.good = None
        self.failures = 0
        self.restarts = 0

    def log(self, message):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print("[supervisor] " + message, flush=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def publish(self, **fields):
        state = {"updated": time.time(), "restarts": self.restarts,
                 "good_revision": self.good, "failures": self.failures, **fields}
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(self):
        self.log(f"supervising {' '.join(self.argv)}")
        while True:
            rev = revision(self.root)
            self.publish(state="starting", revision=rev)
            started = time.monotonic()
            code = self.spawn()
            alive = time.monotonic() - started
            if code == 0:
                self.log("child exited cleanly; stopping")
                self.publish(state="stopped", revision=rev, exit_code=0)
                return 0
            self.restarts += 1
            self.log(f"revision {rev} exited with {code} after {alive:.0f}s")
            if alive >= HEALTHY_SECONDS:
                self.failures = 0
                self.good = rev
                snapshot(self.root, self.snapshots, rev)
            else:
                self.failures += 1
                if self.good and self.good != rev and self.failures >= ROLLBACK_AFTER:
                    saved = self.snapshots / self.good
                    if saved.is_dir():
                        restore(self.root, saved)
                        self.log(f"rolled back to the last revision that served traffic: {self.good}")
                        self.publish(state="rolled_back", revision=rev, restored=self.good)
                        self.failures = 0
                        continue
                if self.good is None and self.failures >= GIVE_UP_AFTER:
                    # The child keeps dying at once and there is no good revision to fall back on —
                    # almost always the port is already served by another instance. Stop rather
                    # than respawn forever (the loop that produced 142 restarts).
                    self.log(f"revision {rev} never started ({self.failures} early exits, exit {code}); "
                             "giving up — is another instance already running on this port?")
                    self.publish(state="gave_up", revision=rev, exit_code=code)
                    return code
            delay = min(MAX_BACKOFF, 2 ** min(self.failures, 5))
            self.publish(state="restarting", revision=rev, exit_code=code, delay=delay)
            self.log(f"restarting in {delay}s")
            time.sleep(delay)

    def spawn(self):
        env = dict(os.environ, AIGENT_SUPERVISED="1")
        try:
            process = subprocess.Popen(self.argv, cwd=str(self.root), env=env)
        except OSError as exc:
            self.log(f"cannot start child: {exc}")
            return 1
        try:
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
            raise


def supervise(root: Path, data: Path, arguments):
    child = [sys.executable, str(root / "run.py"), *arguments]
    return Supervisor(root, data, child).run()
