"""Start one web server + one Telegram long poller. All state stays in this project."""
import argparse
import os
import threading
import webbrowser
from pathlib import Path

import uvicorn

from connector.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent / ".local"
    temp = root / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    os.environ.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))
    import tempfile
    tempfile.tempdir = str(temp)
    app = create_app(root)
    url = f"http://127.0.0.1:{args.port}/"
    if not app.state.config.ready:
        url += "#setup=" + app.state.config["setup_token"]
    print(f"AIGent: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    # HTTP access logs can expose connector query data; application events live in SQLite.
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
