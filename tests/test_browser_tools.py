"""Browser/web extension: URL guard, HTML extraction, search parsing and a fake headless browser."""
import base64
import shutil
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from connector.agent import Agent
from connector import browser_tools
from connector.browser_tools import BrowserTools, html_to_text, parse_results
from connector.config import Config
from connector.store import Store

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake POSIX shebang browser")

PAGE = """<!doctype html><html><head><title>  Example  Domain </title>
<style>body{color:red}</style><script>var x = "hidden script";</script></head>
<body><nav>menu noise</nav><h1>Heading one</h1><p>First  paragraph &amp; entity.</p>
<p>See <a href="https://example.org/next">the next page</a> and <a href="/relative">relative</a>.</p>
<noscript>no script noise</noscript></body></html>"""

DDG = """<html><body>
<div class="result results_links"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fone&amp;rut=x">
<b>First</b> result</a></h2>
<a class="result__snippet">Snippet <b>one</b>.</a></div>
<div class="result"><h2><a class="result__a" href="https://example.net/two">Second result</a></h2>
<div class="result__snippet">Snippet two.</div></div>
<div class="result"><a class="result__a" href="javascript:void(0)">Bad</a></div>
</body></html>"""

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def agent(tmp_path):
    store = Store(tmp_path / "test.db")
    agent = Agent(Config(tmp_path), store, AsyncMock(), AsyncMock())
    agent.approve = AsyncMock(return_value=True)
    yield agent
    store.db.close()


@pytest.fixture
def session(agent):
    return agent.store.resolve(0, 0, 1)


def build(agent, handler):
    """A BrowserTools bound to a mocked transport and an offline DNS stub."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tools = BrowserTools(agent, client)

    async def resolve(host):
        return {"localhost": ["127.0.0.1"], "internal.lan": ["10.0.0.5"]}.get(host, ["93.184.216.34"])

    tools.resolve_host = resolve
    return tools


def events(agent, sid, kind):
    return [e["payload"] for e in agent.store.events(sid) if e["kind"] == kind]


def fake_browser(folder, body='pass'):
    """Stand-in for msedge/chrome: writes the PNG asked for by --screenshot and dumps a DOM."""
    script = folder / "fake-browser"
    script.write_text(
        "#!/usr/bin/env python3\nimport base64, sys, time\n"
        f"PNG = base64.b64decode({base64.b64encode(PNG).decode()!r})\n"
        f"{body}\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.startswith('--screenshot='):\n"
        "        open(arg.split('=', 1)[1], 'wb').write(PNG)\n"
        "if '--dump-dom' in sys.argv:\n"
        "    sys.stdout.write('<html><head><title>Rendered</title></head>"
        "<body><h1>Rendered page</h1></body></html>')\n"
        "sys.exit(0)\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


# ---------------------------------------------------------------- pure helpers

def test_html_to_text_keeps_title_headings_and_absolute_links():
    title, text = html_to_text(PAGE)
    assert title == "Example Domain"
    assert "# Heading one" in text
    assert "First paragraph & entity." in text
    assert "[the next page](https://example.org/next)" in text
    assert "relative" in text and "](/relative)" not in text
    for noise in ("hidden script", "color:red", "menu noise", "no script noise"):
        assert noise not in text
    assert html_to_text(PAGE, 12)[1] == text[:12]


def test_search_results_unwrap_redirects_and_drop_bad_urls():
    results = parse_results(DDG)
    assert [r["url"] for r in results] == ["https://example.org/one", "https://example.net/two"]
    assert results[0]["title"] == "First result" and results[0]["snippet"] == "Snippet one."
    assert results[1]["snippet"] == "Snippet two."
    assert parse_results("<html><body>nothing here</body></html>") == []


# ---------------------------------------------------------------- guard

@pytest.mark.parametrize("url", ["http://127.0.0.1:8787/api/settings", "http://10.1.2.3/x",
                                 "http://localhost:8787/", "http://internal.lan/secret",
                                 "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                                 "file:///etc/passwd", "data:text/html,<h1>hi</h1>", ""])
async def test_blocked_urls_never_reach_the_network(agent, session, url):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, text="secret")

    tools = build(agent, handler)
    with pytest.raises(ValueError):
        await tools.execute(session, "web_fetch", {"url": url})
    assert not calls


async def test_redirect_to_a_private_host_is_blocked_mid_chain(agent, session):
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8787/api/settings"})
        return httpx.Response(200, text="secret")

    with pytest.raises(ValueError, match="loopback"):
        await build(agent, handler).execute(session, "web_fetch", {"url": "https://example.com/start"})


# ---------------------------------------------------------------- web_fetch / web_search

async def test_web_fetch_follows_redirects_caps_text_and_journals_a_web_event(agent, session):
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        body = PAGE + ("<p>" + "y" * 400 + "</p>" if request.url.path == "/long" else "")
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    tools = build(agent, handler)
    result = await tools.execute(session, "web_fetch", {"url": "https://example.com/old"})
    assert result["final_url"] == "https://example.com/new" and result["status"] == 200
    assert result["title"] == "Example Domain" and result["text"].startswith("# Heading one")
    assert result["truncated"] is False
    assert events(agent, session["id"], "web")[0] == {
        "url": "https://example.com/new", "title": "Example Domain", "status": 200,
        "chars": len(result["text"])}
    capped = await tools.execute(session, "web_fetch",
                                 {"url": "https://example.com/long", "max_chars": 200})
    assert len(capped["text"]) == 200 and capped["truncated"] is True


async def test_json_is_returned_as_is(agent, session):
    def handler(request):
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    result = await build(agent, handler).execute(session, "web_fetch", {"url": "https://example.com/api"})
    assert result["content_type"] == "application/json" and '"ok"' in result["text"]
    assert result["title"] == ""


async def test_oversized_body_is_cut_at_five_megabytes(agent, session):
    def handler(request):
        return httpx.Response(200, text="x" * (6 * 1024 * 1024), headers={"content-type": "text/plain"})

    result = await build(agent, handler).execute(
        session, "web_fetch", {"url": "https://example.com/big", "max_chars": 60000})
    assert result["truncated"] is True and len(result["text"]) == 60000


async def test_web_search_uses_the_configured_endpoint_and_user_agent(agent, session):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["agent"] = request.headers.get("user-agent")
        return httpx.Response(200, text=DDG, headers={"content-type": "text/html"})

    tools = build(agent, handler)
    result = await tools.execute(session, "web_search", {"query": "fast cats", "max_results": 1})
    assert seen["url"] == "https://html.duckduckgo.com/html/?q=fast%20cats"
    assert seen["agent"] == "AIGent/0.3"
    assert result["results"] == [{"title": "First result", "url": "https://example.org/one",
                                  "snippet": "Snippet one."}]
    agent.config.values["web_search_url"] = "https://searx.example.com/search"
    await tools.execute(session, "web_search", {"query": "x"})
    assert seen["url"] == "https://searx.example.com/search?q=x"


async def test_unparsable_search_page_explains_itself(agent, session):
    def handler(request):
        return httpx.Response(200, text="<html><body>blocked</body></html>",
                              headers={"content-type": "text/html"})

    result = await build(agent, handler).execute(session, "web_search", {"query": "anything"})
    assert result["results"] == [] and "web_search_url" in result["hint"]


# ---------------------------------------------------------------- tool offering

def test_tools_hidden_without_a_browser_or_when_web_is_off(agent, session, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    # A browser installed on the host would otherwise be discovered through the platform fallback.
    monkeypatch.setattr(browser_tools, "WINDOWS_BROWSERS", ())
    monkeypatch.delenv("AIGENT_BROWSER", raising=False)
    tools = BrowserTools(agent, AsyncMock())
    assert [t["function"]["name"] for t in tools.tools(session)] == ["web_fetch", "web_search"]
    agent.config.values["browser_binary"] = fake_browser(agent.config.root)
    assert [t["function"]["name"] for t in tools.tools(session)] == [
        "web_fetch", "web_search", "browser_open"]
    descriptions = {t["function"]["name"]: t["function"]["description"] for t in tools.tools(session)}
    assert "no clicking" in descriptions["browser_open"]
    agent.config.values["allow_web"] = False
    assert tools.tools(session) == []


async def test_disabled_web_refuses_execution(agent, session):
    agent.config.values["allow_web"] = False
    with pytest.raises(ValueError, match="allow_web"):
        await BrowserTools(agent, AsyncMock()).execute(session, "web_fetch", {"url": "https://example.com"})


# ---------------------------------------------------------------- browser_open

@posix_only
async def test_browser_open_saves_a_screenshot_and_feeds_a_vision_model(agent, session, tmp_path):
    agent.config.values["browser_binary"] = fake_browser(tmp_path)
    agent.config.values["model"] = "deepseek-flash"
    result = await build(agent, lambda r: httpx.Response(200)).execute(
        session, "browser_open", {"url": "https://example.com/", "width": 800, "height": 600})
    relative = result["screenshot"]
    assert relative.startswith("web/") and relative.endswith(".png")
    assert (agent.workspace(session["id"]) / relative).is_file()
    assert result["image_delivered"] is True and result["title"] == "Rendered"
    assert "Rendered page" in result["text"]
    assert result["width"] == 800 and result["height"] == 600
    vision = [m for m in events(agent, session["id"], "media") if m["direction"] == "vision"]
    assert vision and vision[0]["path"] == relative
    assert agent.pending_images[session["id"]]
    assert events(agent, session["id"], "web")[0]["screenshot"] == relative


@posix_only
async def test_non_vision_model_gets_an_explanation_and_a_media_event(agent, session, tmp_path):
    agent.config.values["browser_binary"] = fake_browser(tmp_path)
    agent.config.values["model"] = "deepseek-v4-pro"
    result = await build(agent, lambda r: httpx.Response(200)).execute(
        session, "browser_open", {"url": "https://example.com/", "include_text": False})
    assert result["image_delivered"] is False and "web_fetch" not in result
    assert "cannot see" in result["note"] and "text" not in result
    media = events(agent, session["id"], "media")
    assert media[0]["direction"] == "web" and media[0]["path"] == result["screenshot"]
    assert not agent.pending_images.get(session["id"])


@posix_only
async def test_full_page_uses_a_tall_window_and_documents_the_limit(agent, session, tmp_path):
    agent.config.values["browser_binary"] = fake_browser(tmp_path)
    tools = build(agent, lambda r: httpx.Response(200))
    result = await tools.execute(session, "browser_open",
                                 {"url": "https://example.com/", "full_page": True, "height": 600})
    assert result["height"] == 4000 and "Emulated" in result["full_page"]
    argv = tools.chrome_argv("bin", 1280, 4000, 2500, ["--dump-dom", "https://example.com/"])
    assert "--window-size=1280,4000" in argv and "--headless=new" in argv
    assert any(a.startswith("--user-data-dir=") and "browser-profile" in a for a in argv)


@posix_only
async def test_browser_timeout_is_reported_with_a_hint(agent, session, tmp_path):
    agent.config.values["browser_binary"] = fake_browser(tmp_path, body="time.sleep(60)")
    agent.config.values["browser_timeout_seconds"] = 5
    result = await build(agent, lambda r: httpx.Response(200)).execute(
        session, "browser_open", {"url": "https://example.com/"})
    assert "did not finish" in result["error"] and "web_fetch" in result["hint"]


@posix_only
async def test_missing_screenshot_returns_an_actionable_error(agent, session, tmp_path):
    agent.config.values["browser_binary"] = fake_browser(tmp_path, body="sys.exit(3)")
    result = await build(agent, lambda r: httpx.Response(200)).execute(
        session, "browser_open", {"url": "https://example.com/"})
    assert result["exit_code"] == 3 and "browser_binary" in result["hint"]


# ---------------------------------------------------------------- real headless Chromium

CHROMIUM = next((p for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")), None)


@posix_only
@pytest.mark.skipif(CHROMIUM is None, reason="no local Chromium build to integrate against")
async def test_real_chromium_screenshots_a_local_page(agent, session, tmp_path):
    import http.server
    import threading

    page = tmp_path / "index.html"
    page.write_text("<html><head><title>Local</title></head><body><h1>hi</h1></body></html>",
                    encoding="utf-8")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    agent.config.values.update(browser_binary=str(CHROMIUM), web_allow_private=True,
                               browser_timeout_seconds=120)
    try:
        result = await build(agent, lambda r: httpx.Response(200)).execute(
            session, "browser_open", {"url": f"http://127.0.0.1:{server.server_port}/index.html"})
    finally:
        server.shutdown()
    from PIL import Image
    assert result.get("error") is None, result
    with Image.open(agent.workspace(session["id"]) / result["screenshot"]) as shot:
        assert shot.width > 100 and shot.height > 100
    assert result["title"] == "Local" and "hi" in result["text"]
