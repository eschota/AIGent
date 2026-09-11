"""Start one web server + one Telegram long poller. All state stays in this project."""
import argparse
import os
import sys
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
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--helper", choices=["claude-history"])
    parser.add_argument("--supervise", action="store_true",
                        help="Run under a supervisor that restarts the server and rolls back a broken self-edit")
    parser.add_argument("--external-id")
    args = parser.parse_args()
    if args.helper == "claude-history":
        import dataclasses
        import json
        from claude_agent_sdk import list_sessions, get_session_info, get_session_messages
        if args.external_id:
            info = get_session_info(args.external_id)
            result = {"info": dataclasses.asdict(info) if info else None,
                      "messages": [dataclasses.asdict(m) for m in get_session_messages(args.external_id, limit=100)]}
        else:
            result = [dataclasses.asdict(s) for s in list_sessions(limit=50)]
        print(json.dumps(result, ensure_ascii=True))
        return
    project = Path(__file__).resolve().parent
    root = args.data_dir.resolve() if args.data_dir else project / ".local"
    if args.supervise and not os.environ.get("AIGENT_SUPERVISED"):
        from connector.supervisor import supervise
        root.mkdir(parents=True, exist_ok=True)
        passthrough = [a for a in sys.argv[1:] if a != "--supervise"]
        raise SystemExit(supervise(project, root, passthrough))
    temp = root / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    os.environ.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))
    import tempfile
    tempfile.tempdir = str(temp)
    app = create_app(root, polling=not args.no_telegram)
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
