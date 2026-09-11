"""Web access for the agent: read a page as text, search, and screenshot it in a real browser.

Three tools, all read-only, so none of them asks for an approval:

* ``web_fetch`` downloads one URL with httpx and turns HTML into readable text.
* ``web_search`` asks DuckDuckGo's HTML endpoint and parses the result list. This is a
  lightweight scraper of a page that is not an API: it can break at any time, so the
  endpoint is configurable (`web_search_url`) and can be pointed at a self-hosted SearXNG.
* ``browser_open`` renders the page in headless Edge/Chrome/Chromium and saves a PNG into the
  session workspace, so the user sees exactly what the agent saw and can open it in the chat.

Every URL passes the same guard: only http/https, and the host must resolve to a public
address. That keeps the connector's own admin API, the loopback interface and the local
network unreachable through a model-supplied URL (SSRF). `web_allow_private` lifts the
address check for a deliberately local installation (and for the integration test).

There is no clicking, typing or scrolling: driving a page needs a CDP client, which this
installation deliberately does not depend on. The tool descriptions say so, so the model
does not plan around an interaction that cannot happen.
"""

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from .agent import STRING, safe_path, tool

USER_AGENT = "AIGent/0.3"
FETCH_TIMEOUT = 30.0
MAX_BYTES = 5 * 1024 * 1024
DEFAULT_CHARS = 20000
MAX_CHARS = 60000
MAX_REDIRECTS = 5
BROWSER_TIMEOUT = 60
DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
OUTPUT_CHARS = 4000
FULL_PAGE_HEIGHT = 4000

BROWSER_NAMES = ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser")
WINDOWS_BROWSERS = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

SKIP_TAGS = {"script", "style", "noscript", "nav", "svg", "canvas", "template", "iframe",
             "form", "aside", "footer", "select"}
HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
BLOCK_TAGS = ({"p", "div", "section", "article", "li", "ul", "ol", "tr", "td", "th", "table",
               "br", "hr", "header", "main", "blockquote", "pre", "dl", "dt", "dd",
               "figure", "figcaption"} | set(HEADINGS))


# ---------------------------------------------------------------------- HTML → text


class Readable(HTMLParser):
    """Collect visible text, keep heading levels and a bounded number of links."""

    def __init__(self, link_budget=60):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.link_budget = [], "", link_budget
        self._skip, self._in_title, self._links = 0, False, []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in HEADINGS:
            self.parts.append("\n\n" + HEADINGS[tag] + " ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self._links.append((len(self.parts), dict(attrs).get("href") or ""))

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a" and self._links:
            start, href = self._links.pop()
            text = " ".join("".join(self.parts[start:]).split())
            if text and self.link_budget > 0 and href.startswith(("http://", "https://")):
                del self.parts[start:]
                self.parts.append(f"[{text}]({href})")
                self.link_budget -= 1
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def html_to_text(html, limit=DEFAULT_CHARS):
    """Return ``(title, text)`` for an HTML document; tolerant of broken markup."""
    parser = Readable()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError, RecursionError):
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return " ".join(parser.title.split()), text[:max(1, limit)]


# ---------------------------------------------------------------------- search results


def result_url(href):
    """Unwrap a DuckDuckGo redirect (`/l/?uddg=…`) and keep only http(s) targets."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    target = href
    if "uddg=" in href:
        query = parse_qs(urlsplit(href).query)
        target = (query.get("uddg") or [""])[0] or href
    return target if target.startswith(("http://", "https://")) else ""


class Results(HTMLParser):
    """Parse DuckDuckGo's HTML result list: `result__a` anchors and `result__snippet` blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._mode, self._depth, self._buffer, self._href = None, 0, [], ""

    def handle_starttag(self, tag, attrs):
        if self._mode:
            self._depth += 1
            return
        attributes = dict(attrs)
        classes = attributes.get("class") or ""
        if tag == "a" and "result__a" in classes:
            self._mode, self._depth, self._buffer = "title", 0, []
            self._href = attributes.get("href") or ""
        elif "result__snippet" in classes:
            self._mode, self._depth, self._buffer = "snippet", 0, []

    def handle_endtag(self, tag):
        if not self._mode:
            return
        if self._depth:
            self._depth -= 1
            return
        text = " ".join("".join(self._buffer).split())
        if self._mode == "title":
            url = result_url(self._href)
            if url and text:
                self.results.append({"title": text[:300], "url": url, "snippet": ""})
        elif self.results and not self.results[-1]["snippet"]:
            self.results[-1]["snippet"] = text[:500]
        self._mode, self._buffer = None, []

    def handle_data(self, data):
        if self._mode:
            self._buffer.append(data)


def parse_results(html):
    parser = Results()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError, RecursionError):
        pass
    seen, unique = set(), []
    for item in parser.results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return unique


def clamp(value, default, low, high):
    try:
        number = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        number = default
    return min(max(number, low), high)


class BrowserTools:
    def __init__(self, agent, client):
        self.agent, self.client = agent, client

    @property
    def config(self):
        return self.agent.config

    # ------------------------------------------------------------------ discovery
    def enabled(self):
        return bool(self.config.values.get("allow_web", True))

    def binary(self):
        """Locate a Chromium-family browser: setting, PATH, known Windows install, test override."""
        configured = str(self.config.values.get("browser_binary") or "").strip()
        if configured and Path(configured).exists():
            return configured
        for name in BROWSER_NAMES:
            found = shutil.which(name)
            if found:
                return found
        for pattern in WINDOWS_BROWSERS:
            expanded = os.path.expandvars(pattern)
            if "%" not in expanded and Path(expanded).is_file():
                return expanded
        override = str(os.environ.get("AIGENT_BROWSER") or "").strip()
        return override if override and Path(override).exists() else ""

    # ------------------------------------------------------------------ extension protocol
    def tools(self, session):
        if not self.enabled():
            return []
        offered = [
            tool("web_fetch", "Download one http(s) URL and read it as text: HTML becomes readable text "
                 "with headings and a few links, JSON and plain text are returned as they are. "
                 "No approval needed. Private, loopback and link-local addresses are blocked.",
                 {"url": STRING, "max_chars": {"type": "integer", "minimum": 200, "maximum": MAX_CHARS}},
                 ["url"]),
            tool("web_search", "Search the web and get titles, URLs and snippets. Lightweight HTML scraping "
                 "of a search engine, not an official API: it can return nothing if the engine changes its "
                 "markup — then fetch a known URL directly with web_fetch. No approval needed.",
                 {"query": STRING, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
                 ["query"]),
        ]
        if self.binary():
            offered.append(tool(
                "browser_open", "Open an http(s) page in a headless Edge/Chrome browser, save a PNG "
                "screenshot into the workspace (shown to the user in the chat) and return the page text. "
                "One shot only: there is no clicking, typing, scrolling or navigation inside the page — "
                "for anything beyond the first screen use web_fetch or a direct URL. No approval needed.",
                {"url": STRING, "width": {"type": "integer", "minimum": 320, "maximum": 2560},
                 "height": {"type": "integer", "minimum": 200, "maximum": 8000},
                 "full_page": {"type": "boolean"},
                 "wait_ms": {"type": "integer", "minimum": 0, "maximum": 20000},
                 "include_text": {"type": "boolean"}}, ["url"]))
        return offered

    async def execute(self, session, name, args):
        if not self.enabled():
            raise ValueError("Web access is disabled in settings (allow_web).")
        if name == "web_fetch":
            return await self.run_fetch(session, args)
        if name == "web_search":
            return await self.run_search(session, args)
        if name == "browser_open":
            return await self.run_browser(session, args)
        raise ValueError("Unknown browser tool.")

    # ------------------------------------------------------------------ URL guard
    async def resolve_host(self, host):
        """Addresses a hostname maps to. Separate method so tests can run without DNS."""
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    @staticmethod
    def public(address):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                    or ip.is_reserved or ip.is_unspecified)

    async def check_url(self, raw):
        """Only http/https, and only hosts that resolve to public addresses."""
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("url must be a nonempty string.")
        url = raw.strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ValueError("Only http:// and https:// URLs are allowed "
                             "(file:, data:, ftp: and about: are refused).")
        host = parts.hostname
        if not host:
            raise ValueError("URL has no host.")
        if self.config.values.get("web_allow_private"):
            return url
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                addresses = await self.resolve_host(host)
            except (OSError, socket.gaierror) as exc:
                raise ValueError(f"Could not resolve host {host}: {exc}") from None
        if not addresses:
            raise ValueError(f"Could not resolve host {host}.")
        for address in addresses:
            if not self.public(address):
                raise ValueError(f"Host {host} resolves to {address}: private, loopback and link-local "
                                 "addresses are not reachable from web tools.")
        return url

    # ------------------------------------------------------------------ HTTP
    async def fetch(self, url):
        """GET with an explicit redirect loop, so every hop is re-checked against the guard."""
        current = await self.check_url(url)
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,"
                   "application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                   "Accept-Language": "ru,en;q=0.8"}
        for _ in range(MAX_REDIRECTS + 1):
            async with self.client.stream("GET", current, headers=headers, timeout=FETCH_TIMEOUT,
                                          follow_redirects=False) as response:
                location = response.headers.get("location")
                if response.status_code in (301, 302, 303, 307, 308) and location:
                    current = await self.check_url(urljoin(current, location))
                    continue
                body, total, truncated = [], 0, False
                async for chunk in response.aiter_bytes():
                    if total + len(chunk) > MAX_BYTES:
                        body.append(chunk[:MAX_BYTES - total])
                        truncated = True
                        break
                    body.append(chunk)
                    total += len(chunk)
                raw = b"".join(body)
                encoding = response.charset_encoding or "utf-8"
                try:
                    text = raw.decode(encoding, "replace")
                except LookupError:
                    text = raw.decode("utf-8", "replace")
                return {"final_url": current, "status": response.status_code, "bytes": len(raw),
                        "content_type": (response.headers.get("content-type") or "").split(";")[0]
                        .strip().lower(), "text": text, "truncated": truncated}
        raise ValueError("Too many redirects.")

    async def run_fetch(self, session, args):
        limit = clamp(args.get("max_chars"), DEFAULT_CHARS, 200, MAX_CHARS)
        data = await self.fetch(str(args.get("url") or ""))
        html = "html" in data["content_type"] or (not data["content_type"]
                                                  and "<html" in data["text"][:2000].lower())
        if html:
            title, full = html_to_text(data["text"], MAX_CHARS)
        else:
            title, full = "", data["text"][:MAX_CHARS]
        text = full[:limit]
        result = {"url": str(args.get("url") or ""), "final_url": data["final_url"],
                  "status": data["status"], "content_type": data["content_type"], "title": title,
                  "text": self.config.redact(text), "chars": len(text),
                  "truncated": data["truncated"] or len(full) > limit}
        self.agent.store.event(session["id"], "web", {"url": data["final_url"], "title": title,
                                                      "status": data["status"], "chars": len(text)})
        return result

    async def run_search(self, session, args):
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query must be a nonempty string.")
        count = clamp(args.get("max_results"), 10, 1, 10)
        template = str(self.config.values.get("web_search_url") or "").strip() or DEFAULT_SEARCH_URL
        if "{query}" not in template:
            template += ("&" if "?" in template else "?") + "q={query}"
        data = await self.fetch(template.replace("{query}", quote(query, safe="")))
        results = parse_results(data["text"])[:count]
        self.agent.store.event(session["id"], "web", {"url": data["final_url"], "title": "Поиск: " + query,
                                                      "status": data["status"], "chars": len(data["text"]),
                                                      "results": len(results)})
        result = {"query": query, "results": results, "source": data["final_url"],
                  "note": "Lightweight HTML scrape of a search engine, not an official API."}
        if not results:
            result["hint"] = ("No results were parsed: the engine may have changed its markup or blocked "
                              "the request. Fetch a known URL with web_fetch, or set web_search_url in "
                              "settings to a SearXNG instance.")
        return result

    # ------------------------------------------------------------------ headless browser
    @property
    def timeout(self):
        return clamp(self.config.values.get("browser_timeout_seconds"), BROWSER_TIMEOUT, 5, 600)

    async def spawn(self, argv, cwd, limit=None):
        """Run the browser without a shell; on timeout kill the tree and keep partial output."""
        limit = self.timeout if limit is None else limit
        env = {k: v for k, v in os.environ.items()
               if not any(x in k.upper() for x in ("TOKEN", "SECRET", "KEY", "PASSWORD"))}
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, timed_out = b"", False
        try:
            output = await asyncio.wait_for(process.stdout.read(), limit)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        except (asyncio.TimeoutError, TimeoutError):
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
        return process.returncode, output.decode("utf-8", "replace"), timed_out

    def chrome_argv(self, binary, width, height, wait, extra):
        profile = (self.config.root / "browser-profile")
        profile.mkdir(parents=True, exist_ok=True)
        return [binary, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                f"--window-size={width},{height}", f"--virtual-time-budget={wait}",
                f"--user-data-dir={profile}"] + extra

    async def run_browser(self, session, args):
        binary = self.binary()
        if not binary:
            raise ValueError("No Chromium-family browser was found. Install Microsoft Edge or Google "
                             "Chrome, or set browser_binary in settings.")
        url = await self.check_url(str(args.get("url") or ""))
        width = clamp(args.get("width"), 1280, 320, 2560)
        height = clamp(args.get("height"), 800, 200, 8000)
        if args.get("full_page"):
            height = max(height, FULL_PAGE_HEIGHT)
        wait = clamp(args.get("wait_ms"), 2500, 0, 20000)
        include_text = args.get("include_text", True) is not False
        sid = session["id"]
        root = self.agent.workspace(sid)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        relative = f"web/{stamp}-{hashlib.sha256(url.encode()).hexdigest()[:8]}.png"
        target = safe_path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        code, output, timed_out = await self.spawn(
            self.chrome_argv(binary, width, height, wait, [f"--screenshot={target}", url]), root)
        if timed_out:
            return {"error": f"The browser did not finish within {self.timeout} s and was stopped.",
                    "hint": "Retry with a smaller wait_ms, or read the page as text with web_fetch.",
                    "url": url}
        if not target.is_file() or not target.stat().st_size:
            return {"error": "The browser exited without writing a screenshot.", "exit_code": code,
                    "output": self.config.redact(output[:OUTPUT_CHARS]), "url": url,
                    "hint": "Check browser_binary in settings, or read the page with web_fetch."}
        title, text = "", ""
        if include_text:
            dom_code, dom, dom_timeout = await self.spawn(
                self.chrome_argv(binary, width, height, wait, ["--dump-dom", url]), root)
            if not dom_timeout and dom_code == 0:
                title, text = html_to_text(dom, DEFAULT_CHARS)
        result = {"url": url, "title": title, "screenshot": relative, "width": width, "height": height,
                  "image_delivered": False}
        if include_text:
            result["text"] = self.config.redact(text)
        if args.get("full_page"):
            result["full_page"] = ("Emulated with a tall window; content below "
                                   f"{height}px is still not captured.")
        from .vision import VISION_MODELS
        if (session.get("model") or self.config["model"]) in VISION_MODELS:
            self.agent.queue_image(session, target, detail="original")
            result["image_delivered"] = True
        else:
            self.agent.store.event(sid, "media", {"path": relative, "kind": "image", "direction": "web",
                                                  "url": url, "title": title})
            result["note"] = ("The screenshot is saved and visible to the user, but this model cannot see "
                              "images: rely on the page text or web_fetch, or ask the user to switch to a "
                              "vision-capable model.")
        self.agent.store.event(sid, "web", {"url": url, "title": title, "screenshot": relative,
                                            "chars": len(text)})
        return result
