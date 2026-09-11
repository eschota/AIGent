"""Keep pytest and upload scratch files inside a fresh checkout too."""
import tempfile
from pathlib import Path


def pytest_configure(config):
    runtime = Path(__file__).resolve().parents[1] / ".local"
    runtime.mkdir(exist_ok=True)
    scratch = runtime / "tmp"
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir = str(scratch)
