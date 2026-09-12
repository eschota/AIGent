"""Drive a Chrome window from the agent, over the DevTools protocol.

The owner asked for a tool that opens a browser and does things in it — uploading a reel to their
YouTube channel being the first. It is a visible Chrome, not a headless one, but it runs on its
own profile directory under the data root: Chrome 136+ silently ignores ``--remote-debugging-port``
on the user's default data directory (a defence against session theft), so the everyday Chrome
cannot be driven and is never touched. The owner signs in once, by hand, in the window the agent
opens; that login then persists in the AIGent profile.

No Playwright, no websocket package: the DevTools protocol is JSON over a WebSocket, and the few
frames this needs fit in a hundred lines below. Pages built from shadow DOM (YouTube Studio is
Polymer through and through) are read and clicked through a deep walk, refs are stamped on the
elements themselves, and clicks are real mouse events at the element's centre — the difference
between a button that reacts and one that does not.

Tools: chrome_open, chrome_snapshot, chrome_click, chrome_type, chrome_press, chrome_upload,
chrome_wait, chrome_screenshot, chrome_eval. Uploads, evaluation and the Chrome launch go through
the session's approval; nothing is ever copied out of the owner's everyday profile.
"""

import asyncio
import base64
import json
import os
import secrets
import shutil
import struct
import subprocess
import time
from pathlib import Path

import httpx

from .agent import STRING, safe_path, tool

DEFAULT_PORT = 9333
KEYS = {  # DevTools key events need the Windows virtual key code as well as the key name
    "Enter": (13, "\r"), "Tab": (9, "\t"), "Escape": (27, ""), "Backspace": (8, ""), "Delete": (46, ""),
    "ArrowUp": (38, ""), "ArrowDown": (40, ""), "ArrowLeft": (37, ""), "ArrowRight": (39, ""),
    "Home": (36, ""), "End": (35, ""), "PageUp": (33, ""), "PageDown": (34, ""), "Space": (32, " "),
}
MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "shift": 8}
SNAPSHOT_JS = r"""
(() => {
  const all = [];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document);
  const visible = (el) => { const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el); return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const interactive = (el) => {
    const tag = el.tagName.toLowerCase(); const role = el.getAttribute('role') || '';
    return ['a','button','input','textarea','select','summary'].includes(tag) || /^(button|link|menuitem|option|radio|checkbox|tab|textbox|switch|combobox|listbox|treeitem)$/.test(role)
      || el.getAttribute('contenteditable') === 'true' || /^(ytcp-button|tp-yt-paper-item|tp-yt-paper-radio-button|tp-yt-paper-checkbox|ytcp-icon-button|yt-formatted-string|ytcp-dropdown-trigger|tp-yt-paper-tab)$/.test(tag);
  };
  const label = (el) => {
    const own = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('title'), el.getAttribute('alt')].find(v => v && v.trim());
    let text = own || el.innerText || el.textContent || el.value || '';
    return String(text).trim().replace(/\s+/g, ' ').slice(0, 90);
  };
  const out = []; let n = 0;
  for (const el of all) {
    if (!interactive(el) || !visible(el)) continue;
    if (el.tagName.toLowerCase() === 'input' && el.type === 'hidden') continue;
    el.setAttribute('data-aigent-ref', String(n));
    const item = { ref: n, role: (el.getAttribute('role') || el.tagName.toLowerCase()) + (el.type && el.tagName.toLowerCase() === 'input' ? ':' + el.type : ''), label: label(el) };
    if (el.href) item.href = String(el.href).slice(0, 140);
    if (/^(input|textarea)$/i.test(el.tagName) && el.value) item.value = String(el.value).slice(0, 80);
    if (el.getAttribute('aria-checked') || el.checked) item.checked = el.getAttribute('aria-checked') === 'true' || !!el.checked;
    out.push(item); n++;
    if (out.length >= 200) break;
  }
  const chunks = []; let total = 0;
  for (const el of all) {
    if (total > 6000) break;
    if (el.children.length && !el.shadowRoot) continue;
    if (!visible(el)) continue;
    const t = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    if (t && t.length > 1 && !chunks.includes(t)) { chunks.push(t.slice(0, 200)); total += t.length; }
  }
  return { url: location.href, title: document.title, elements: out, text: chunks.join('\n') };
})()
"""
FIND_JS = r"""
((ref, selector, text) => {
  const all = [];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document);
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; };
  let found = null;
  if (ref !== null && ref !== undefined && ref !== '') found = all.find(el => el.getAttribute('data-aigent-ref') === String(ref)) || null;
  else if (selector) { for (const root of [document, ...all.filter(e => e.shadowRoot).map(e => e.shadowRoot)]) { const el = root.querySelector(selector); if (el && visible(el)) { found = el; break; } } }
  else if (text) { const needle = String(text).toLowerCase(); found = all.find(el => visible(el) && (el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim().toLowerCase() === needle)
      || all.find(el => visible(el) && ['a','button','input','textarea','select'].includes(el.tagName.toLowerCase()) && (el.getAttribute('aria-label') || el.innerText || el.textContent || '').toLowerCase().includes(needle))
      || all.find(el => visible(el) && !el.children.length && (el.innerText || el.textContent || '').toLowerCase().includes(needle)) || null; }
  if (!found) return null;
  for (const el of all) if (el.hasAttribute('data-aigent-target')) el.removeAttribute('data-aigent-target');
  found.scrollIntoView({ block: 'center', inline: 'center' });
  const r = found.getBoundingClientRect();
  found.setAttribute('data-aigent-target', '1');
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, tag: found.tagName.toLowerCase(), label: (found.getAttribute('aria-label') || found.innerText || found.value || '').trim().slice(0, 80), type: found.type || '' };
})
"""
TARGET_JS = r"""
(() => { const all = []; const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document); return all.find(el => el.getAttribute('data-aigent-target') === '1') || null; })()
"""
FOCUS_JS = r"""
((clear) => { const all = []; const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document); const el = all.find(e => e.getAttribute('data-aigent-target') === '1'); if (!el) return false;
  el.focus(); if (clear) { if ('value' in el && !el.isContentEditable) { el.value = ''; el.dispatchEvent(new Event('input', { bubbles: true })); } else if (el.isContentEditable) { const s = getSelection(); s.selectAllChildren(el); document.execCommand('delete'); } }
  return true; })
"""


class CDPSocket:
    """Just enough WebSocket client for the DevTools protocol: text frames, ping/pong, close."""

    def __init__(self, url):
        self.url, self.reader, self.writer, self.next_id, self.pending = url, None, None, 0, {}
        self.events = []

    async def connect(self, timeout=10):
        assert self.url.startswith("ws://")
        host_port, _, path = self.url[5:].partition("/")
        host, _, port = host_port.partition(":")
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(host, int(port or 80)), timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.writer.write((f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        await self.writer.drain()
        head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), timeout)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("DevTools websocket handshake failed: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        return self

    @staticmethod
    def frame(payload, opcode=1):
        head = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            head.append(0x80 | length)
        elif length < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", length)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", length)
        mask = secrets.token_bytes(4)
        return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    @staticmethod
    def parse(data):
        """Split one server frame off `data`: (fin, opcode, payload, rest) or None when incomplete."""
        if len(data) < 2:
            return None
        fin, opcode, masked, length = data[0] & 0x80, data[0] & 0x0F, data[1] & 0x80, data[1] & 0x7F
        offset = 2
        if length == 126:
            if len(data) < 4:
                return None
            length, offset = struct.unpack(">H", data[2:4])[0], 4
        elif length == 127:
            if len(data) < 10:
                return None
            length, offset = struct.unpack(">Q", data[2:10])[0], 10
        mask = b""
        if masked:
            mask, offset = data[offset:offset + 4], offset + 4
        if len(data) < offset + length:
            return None
        payload = data[offset:offset + length]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(fin), opcode, payload, data[offset + length:]

    async def send(self, message):
        self.writer.write(self.frame(json.dumps(message).encode()))
        await self.writer.drain()

    async def messages(self, timeout):
        """Yield decoded JSON messages until `timeout` seconds pass without a complete frame."""
        buffer, text = b"", b""
        deadline = time.monotonic() + timeout
        while True:
            parsed = self.parse(buffer)
            while parsed is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                chunk = await asyncio.wait_for(self.reader.read(65536), remaining)
                if not chunk:
                    raise ConnectionError("DevTools websocket closed")
                buffer += chunk
                parsed = self.parse(buffer)
            fin, opcode, payload, buffer = parsed
            if opcode == 0x8:
                raise ConnectionError("DevTools websocket closed by Chrome")
            if opcode == 0x9:
                self.writer.write(self.frame(payload, 0xA))
                await self.writer.drain()
                continue
            if opcode in (0x1, 0x0):
                text += payload
                if fin:
                    try:
                        yield json.loads(text.decode())
                    finally:
                        text = b""

    async def call(self, method, params=None, timeout=30):
        self.next_id += 1
        ident = self.next_id
        await self.send({"id": ident, "method": method, "params": params or {}})
        async for message in self.messages(timeout):
            if message.get("id") == ident:
                if "error" in message:
                    raise ValueError(f"{method}: {message['error'].get('message', message['error'])}")
                return message.get("result") or {}
            if "method" in message:
                self.events.append(message)
                del self.events[:-50]
        raise TimeoutError(f"{method}: no reply from Chrome within {timeout}s")

    async def close(self):
        if self.writer:
            try:
                self.writer.write(self.frame(b"", 0x8))
                await self.writer.drain()
            except Exception:
                pass
            self.writer.close()


class ChromeTools:
    """Extension: the owner's Chrome as a tool, one tab per session."""

    def __init__(self, agent, client=None):
        self.agent, self.client = agent, client or httpx.AsyncClient()
        self.tabs = {}     # sid -> {"target": id, "socket": CDPSocket}
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ configuration
    @property
    def config(self):
        return self.agent.config

    def port(self):
        return int(self.config.values.get("chrome_debug_port", DEFAULT_PORT) or DEFAULT_PORT)

    def binary(self):
        configured = self.config.values.get("browser_binary") or ""
        candidates = [configured] if configured else []
        program = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [os.path.join(program, "Google", "Chrome", "Application", "chrome.exe"),
                       os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
                       shutil.which("chrome") or "", shutil.which("google-chrome") or ""]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        raise ValueError("Chrome was not found; set browser_binary in the settings.")

    def user_data_dir(self):
        """AIGent's own Chrome profile directory.

        Chrome 136+ silently ignores --remote-debugging-port on the user's default data directory
        (a defence against session theft), so the agent's Chrome lives in its own directory under
        the data root. The owner signs in there once, in the window the agent opens; nothing is
        ever copied from their everyday profile.
        """
        configured = self.config.values.get("chrome_user_data_dir") or ""
        if configured:
            return configured
        path = self.config.root / "chrome-automation"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def profile(self):
        return self.config.values.get("chrome_profile") or "Default"

    def launch_args(self, url):
        return [self.binary(), f"--remote-debugging-port={self.port()}", "--remote-allow-origins=*",
                f"--user-data-dir={self.user_data_dir()}", f"--profile-directory={self.profile()}",
                "--no-first-run", "--no-default-browser-check", "--new-window", url]

    # ------------------------------------------------------------------ extension protocol
    def tools(self, session):
        target = {"ref": {"type": "integer"}, "selector": STRING, "text": STRING}
        return [
            tool("chrome_open", "Open a URL in AIGent's Chrome window (a separate Chrome profile with its own "
                 "logins, started on demand; the owner signs in there once, by hand) and make the tab scriptable. "
                 "Returns a snapshot; sign_in_required in it means: ask the owner to sign in and wait.",
                 {"url": STRING, "restart_chrome": {"type": "boolean"}}, ["url"]),
            tool("chrome_snapshot", "The current tab: url, title, visible text and the interactive elements with refs "
                 "(buttons, links, inputs, menu items — shadow DOM included). Refs are valid until the page changes.",
                 {}, []),
            tool("chrome_click", "Click an element by ref (from chrome_snapshot), CSS selector or visible text: a real "
                 "mouse click at its centre after scrolling it into view.", target, []),
            tool("chrome_type", "Focus an element (ref, selector or text) and type text into it; works for inputs, "
                 "textareas and contenteditable fields. clear=true empties it first; press_enter=true submits.",
                 {**target, "input": STRING, "clear": {"type": "boolean"}, "press_enter": {"type": "boolean"}}, ["input"]),
            tool("chrome_press", "Press a key in the tab: Enter, Tab, Escape, Backspace, Delete, ArrowUp/Down/Left/Right, "
                 "Home, End, PageUp, PageDown, Space, or a letter with modifiers like ctrl+a.",
                 {"key": STRING}, ["key"]),
            tool("chrome_upload", "Put a file into a file input (ref, selector or text of the input/button that owns "
                 "it) without an OS dialog. The path is workspace-relative or absolute; approval.",
                 {**target, "path": STRING}, ["path"]),
            tool("chrome_wait", "Wait until the page shows text, a selector matches, or the URL contains a string "
                 "(any that are given), up to timeout_seconds (default 30, max 50 per call; call again to keep "
                 "waiting). Returns a snapshot.",
                 {"text": STRING, "selector": STRING, "url_contains": STRING,
                  "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 50}}, []),
            tool("chrome_screenshot", "Screenshot of the tab saved into the workspace and shown to you when the model "
                 "can see images.", {}, []),
            tool("chrome_eval", "Evaluate a JavaScript expression in the page and return its JSON value; approval.",
                 {"expression": STRING}, ["expression"]),
        ]

    def running(self, sid):
        return False

    async def execute(self, session, name, args):
        sid = session["id"]
        if name == "chrome_open":
            return await self.open(session, str(args.get("url") or ""), bool(args.get("restart_chrome")))
        tab = self.tabs.get(sid)
        if not tab:
            raise ValueError("No Chrome tab for this session yet: call chrome_open first.")
        socket = tab["socket"]
        if name == "chrome_snapshot":
            return await self.snapshot(socket)
        if name == "chrome_click":
            return await self.click(socket, args)
        if name == "chrome_type":
            return await self.type(socket, args)
        if name == "chrome_press":
            return await self.press(socket, str(args.get("key") or "Enter"))
        if name == "chrome_upload":
            return await self.upload(session, socket, args)
        if name == "chrome_wait":
            return await self.wait(socket, args)
        if name == "chrome_screenshot":
            return await self.screenshot(session, socket)
        if name == "chrome_eval":
            expression = str(args.get("expression") or "")
            if not await self.agent.approve(session, "chrome_eval", expression[:1400]):
                return {"denied": True}
            return {"value": await self.evaluate(socket, expression)}
        raise ValueError(f"Unknown Chrome tool {name}")

    # ------------------------------------------------------------------ Chrome itself
    async def alive(self):
        try:
            response = await self.client.get(f"http://127.0.0.1:{self.port()}/json/version", timeout=2)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def ensure_chrome(self, session, url, restart=False):
        """AIGent's Chrome instance, started on demand; the owner's everyday Chrome is never touched."""
        if await self.alive():
            return {"launched": False}
        if not await self.agent.approve(session, "chrome_open · запуск Chrome AIGent",
                                        "Запустить отдельный Chrome AIGent (свой профиль, порт отладки): "
                                        + " ".join(self.launch_args(url)), admin_only=True):
            return {"denied": True}
        subprocess.Popen(self.launch_args(url), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), close_fds=True)
        for _ in range(50):
            await asyncio.sleep(0.5)
            if await self.alive():
                return {"launched": True}
        raise ValueError("Chrome не поднял порт отладки за 25 секунд; проверьте browser_binary и chrome_user_data_dir.")

    async def targets(self):
        response = await self.client.get(f"http://127.0.0.1:{self.port()}/json", timeout=5)
        return [t for t in response.json() if t.get("type") == "page"]

    async def attach(self, sid, target_id=None, url=None):
        """Attach to a page target (a new tab when none is given), replacing the session's socket."""
        old = self.tabs.pop(sid, None)
        if old:
            await old["socket"].close()
        if target_id is None:
            response = await self.client.put(f"http://127.0.0.1:{self.port()}/json/new?{url or 'about:blank'}", timeout=5)
            target = response.json()
        else:
            target = next(t for t in await self.targets() if t["id"] == target_id)
        socket = await CDPSocket(target["webSocketDebuggerUrl"]).connect()
        await socket.call("Page.enable")
        await socket.call("Runtime.enable")
        self.tabs[sid] = {"target": target["id"], "socket": socket}
        return socket

    async def open(self, session, url, restart):
        if not url.startswith(("http://", "https://")):
            raise ValueError("Only http(s) URLs can be opened.")
        sid = session["id"]
        async with self.lock:
            state = await self.ensure_chrome(session, url, restart)
            if state.get("denied"):
                return state
            tab = self.tabs.get(sid)
            socket = None
            if tab and not state["launched"]:
                try:
                    await tab["socket"].call("Runtime.evaluate", {"expression": "1"}, timeout=5)
                    socket = tab["socket"]
                except Exception:
                    socket = None
            if socket is None:
                if state["launched"]:
                    # The launch opened the URL in a new window: attach to that tab instead of a second one.
                    pages = await self.targets()
                    match = next((t for t in pages if t.get("url", "").startswith(url.split("#")[0][:40])), pages[0] if pages else None)
                    socket = await self.attach(sid, match["id"] if match else None, url)
                else:
                    socket = await self.attach(sid, None, url)
            if not state["launched"]:
                await socket.call("Page.navigate", {"url": url})
            await self.settle(socket, seconds=10)
            snapshot = await self.snapshot(socket)
            snapshot["launched_chrome"] = state["launched"]
            if "accounts.google.com" in str(snapshot.get("url") or "") or "ServiceLogin" in str(snapshot.get("url") or ""):
                snapshot["sign_in_required"] = ("Это отдельный Chrome AIGent: владелец должен один раз войти в Google "
                                                "в этом окне сам. Попроси его войти и жди chrome_wait по url_contains.")
            return snapshot

    # ------------------------------------------------------------------ page operations
    async def evaluate(self, socket, expression, timeout=30):
        result = await socket.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                        "awaitPromise": True}, timeout=timeout)
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text") or "JavaScript error"
            raise ValueError(str(text)[:400])
        return (result.get("result") or {}).get("value")

    async def settle(self, socket, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if await self.evaluate(socket, "document.readyState", timeout=5) == "complete":
                    await asyncio.sleep(0.5)
                    return True
            except ValueError:
                pass
            await asyncio.sleep(0.3)
        return False

    async def snapshot(self, socket):
        data = await self.evaluate(socket, SNAPSHOT_JS) or {}
        return {"url": data.get("url"), "title": data.get("title"), "elements": data.get("elements") or [],
                "text": data.get("text") or "", "note": "Click or type by ref; refs expire when the page changes."}

    async def locate(self, socket, args):
        ref = args.get("ref")
        found = await self.evaluate(socket, f"{FIND_JS}({json.dumps(ref if ref not in ('', None) else None)}, "
                                            f"{json.dumps(args.get('selector') or None)}, {json.dumps(args.get('text') or None)})")
        if not found:
            raise ValueError("Element not found; take a fresh chrome_snapshot and use one of its refs.")
        return found

    async def click(self, socket, args):
        found = await self.locate(socket, args)
        x, y = found["x"], found["y"]
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await socket.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                           "clickCount": 1 if kind != "mouseMoved" else 0})
        await asyncio.sleep(0.4)
        return {"clicked": found.get("label") or found.get("tag"), "x": round(x), "y": round(y),
                "next": "chrome_snapshot to see the result"}

    async def type(self, socket, args):
        text = str(args.get("input") or "")
        if args.get("ref") not in (None, "") or args.get("selector") or args.get("text"):
            found = await self.locate(socket, args)
            await self.click(socket, {"ref": args.get("ref"), "selector": args.get("selector"), "text": args.get("text")})
            await self.evaluate(socket, f"{FOCUS_JS}({json.dumps(bool(args.get('clear')))})")
        else:
            found = {"label": "(focused element)"}
        if text:
            await socket.call("Input.insertText", {"text": text})
        if args.get("press_enter"):
            await self.press(socket, "Enter")
        return {"typed": len(text), "into": found.get("label") or found.get("tag"), "next": "chrome_snapshot"}

    async def press(self, socket, combo):
        parts = [p for p in combo.replace("-", "+").split("+") if p]
        modifiers, key = 0, parts[-1] if parts else "Enter"
        for part in parts[:-1]:
            modifiers |= MODIFIERS.get(part.lower(), 0)
        name = {k.lower(): k for k in KEYS}.get(key.lower(), key)
        code, text = KEYS.get(name, (ord(name.upper()[0]) if len(name) == 1 else 0, name if len(name) == 1 and not modifiers else ""))
        base = {"key": name, "code": name if name in KEYS else f"Key{name.upper()}" if len(name) == 1 else name,
                "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code, "modifiers": modifiers}
        await socket.call("Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": text} if text else {})})
        await socket.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})
        await asyncio.sleep(0.3)
        return {"pressed": combo}

    async def upload(self, session, socket, args):
        raw = str(args.get("path") or "")
        path = Path(raw)
        if not path.is_absolute():
            path = safe_path(self.agent.workspace(session["id"]), raw)
        if not path.is_file():
            raise ValueError(f"File not found: {raw}")
        if not await self.agent.approve(session, "chrome_upload", f"{path}\n→ {socket.url}"):
            return {"denied": True}
        target = args if any(args.get(k) not in (None, "") for k in ("ref", "selector", "text")) else {"selector": "input[type=file]"}
        await self.locate(socket, target)
        handle = await socket.call("Runtime.evaluate", {"expression": TARGET_JS})
        node = (handle.get("result") or {})
        if not node.get("objectId"):
            raise ValueError("The target element vanished; take a fresh snapshot.")
        if node.get("className") != "HTMLInputElement":
            # A button that owns the file input: find the nearest file input in the document instead.
            await self.locate(socket, {"selector": "input[type=file]"})
            handle = await socket.call("Runtime.evaluate", {"expression": TARGET_JS})
            node = handle.get("result") or {}
        await socket.call("DOM.setFileInputFiles", {"files": [str(path)], "objectId": node["objectId"]})
        await asyncio.sleep(1)
        return {"uploaded": str(path), "bytes": path.stat().st_size, "next": "chrome_wait for the page to react"}

    async def wait(self, socket, args):
        timeout = max(1, min(50, int(args.get("timeout_seconds") or 30)))  # the MCP client gives a call one minute
        text, selector, url_part = args.get("text"), args.get("selector"), args.get("url_contains")
        deadline = time.monotonic() + timeout
        while True:
            checks = []
            if url_part:
                checks.append(url_part in str(await self.evaluate(socket, "location.href")))
            if selector:
                checks.append(bool(await self.evaluate(socket, f"{FIND_JS}(null, {json.dumps(selector)}, null)")))
            if text:
                checks.append(bool(await self.evaluate(socket, f"{FIND_JS}(null, null, {json.dumps(text)})")))
            if checks and any(checks):
                return {"satisfied": True, **await self.snapshot(socket)}
            if not checks or time.monotonic() > deadline:
                snapshot = await self.snapshot(socket)
                return {"satisfied": False, "timeout_seconds": timeout, **snapshot} if checks else snapshot
            await asyncio.sleep(1)

    async def screenshot(self, session, socket):
        result = await socket.call("Page.captureScreenshot", {"format": "png"}, timeout=30)
        data = base64.b64decode(result["data"])
        sid = session["id"]
        target = self.agent.workspace(sid) / f"chrome-{secrets.token_hex(6)}.png"
        target.write_bytes(data)
        from .vision import VISION_MODELS
        seen = (session.get("model") or self.config["model"]) in VISION_MODELS
        if seen:
            self.agent.pending_images.setdefault(sid, []).append(
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + result["data"], "detail": "original"}})
        self.agent.store.event(sid, "media", {"path": target.name, "kind": "photo", "direction": "generated",
                                              "caption": "Скриншот Chrome"})
        return {"path": target.name, "bytes": len(data), "shown": seen,
                "note": "" if seen else "The model cannot see images; use chrome_snapshot for the page text."}

    async def close(self):
        for tab in list(self.tabs.values()):
            await tab["socket"].close()
        self.tabs.clear()
