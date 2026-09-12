"""The supervisor tells the truth about a child that is serving, not only about one that died."""

import json
import sys

from connector import supervisor as supervisor_module
from connector.supervisor import Supervisor


def test_a_child_that_serves_is_published_as_running(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor_module, "HEALTHY_SECONDS", 0.2)
    root = tmp_path / "project"
    (root / "connector").mkdir(parents=True)
    (root / "run.py").write_text("print('stub')\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    child = [sys.executable, "-c", "import time; time.sleep(0.6)"]
    supervisor = Supervisor(root, data, child)

    code = supervisor.spawn()

    assert code == 0
    state = json.loads((data / "supervisor.json").read_text(encoding="utf-8"))
    assert state["state"] == "running" and state["pid"] and state["revision"]


def test_a_child_that_dies_at_once_is_never_called_running(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor_module, "HEALTHY_SECONDS", 5)
    root = tmp_path / "project"
    (root / "connector").mkdir(parents=True)
    (root / "run.py").write_text("print('stub')\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    supervisor = Supervisor(root, data, [sys.executable, "-c", "raise SystemExit(3)"])

    assert supervisor.spawn() == 3
    assert not (data / "supervisor.json").exists(), "nothing claimed the dead child was serving"
