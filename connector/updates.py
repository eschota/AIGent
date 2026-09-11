"""Update channel for the AIGent desktop build.

Only anonymous, read-only requests to the public GitHub release page are made: no
token, telemetry or user data leaves the machine. The published build names its
files `AIGent-Setup-<version>.exe` (installer) and `AIGent-<version>-portable.exe`.
"""

import re
import time
from xml.etree import ElementTree

API_URL = "https://api.github.com/repos/{repo}/releases/latest"
FEED_URL = "https://github.com/{repo}/releases.atom"
USER_AGENT = "AIGent-UpdateCheck"
FILES = {
    "installer": re.compile(r"^AIGent-Setup-(?P<version>\d+(?:\.\d+)*)\.exe$", re.IGNORECASE),
    "portable": re.compile(r"^AIGent-(?P<version>\d+(?:\.\d+)*)-portable\.exe$", re.IGNORECASE),
}
TAG = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")
ATOM = "{http://www.w3.org/2005/Atom}"


def parse_version(text):
    """Comparable tuple, or empty for anything that is not a numeric tag."""
    match = TAG.match(str(text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def is_newer(candidate, current):
    latest, running = parse_version(candidate), parse_version(current)
    if not latest or not running:
        return False
    width = max(len(latest), len(running))
    return latest + (0,) * (width - len(latest)) > running + (0,) * (width - len(running))


def pick_asset(assets, kind="installer"):
    """The release file for the running platform; the newest matching name wins."""
    pattern = FILES[kind]
    best = None
    for asset in assets or []:
        match = pattern.match(str(asset.get("name") or ""))
        if match and (best is None or is_newer(match.group("version"), best[0])):
            best = (match.group("version"), asset)
    return best[1] if best else None


def normalize_api(payload):
    """GitHub REST release payload -> the fields the interface and updater use."""
    tag = str(payload.get("tag_name") or "")
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "notes": str(payload.get("body") or ""),
        "page": str(payload.get("html_url") or ""),
        "published": str(payload.get("published_at") or ""),
        "assets": [{"name": str(a.get("name") or ""), "url": str(a.get("browser_download_url") or ""),
                    "size": int(a.get("size") or 0),
                    "sha256": str(a.get("digest") or "").removeprefix("sha256:")}
                   for a in payload.get("assets") or []],
    }


def normalize_feed(text):
    """Fallback when the anonymous API is rate limited: the newest tag from the Atom feed."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    entry = root.find(ATOM + "entry")
    if entry is None:
        return None
    title = (entry.findtext(ATOM + "title") or "").strip()
    if not parse_version(title):
        return None
    link = entry.find(ATOM + "link")
    return {"tag": title, "version": title.lstrip("vV"), "notes": "",
            "page": link.get("href") if link is not None else "",
            "published": entry.findtext(ATOM + "updated") or "", "assets": []}


class UpdateChannel:
    """Reads the release page once per ttl so an open interface cannot hammer GitHub."""

    def __init__(self, client, repo, ttl=900):
        self.client = client
        self.repo = (repo or "").strip()
        self.ttl = ttl
        self.cached = None
        self.checked = 0.0

    async def release(self, force=False):
        if not self.repo:
            return None, "Update channel is disabled"
        if not force and self.cached and time.time() - self.checked < self.ttl:
            return self.cached, ""
        release, error = None, ""
        try:
            response = await self.client.get(API_URL.format(repo=self.repo), timeout=15,
                headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
            if response.status_code == 200:
                release = normalize_api(response.json())
            else:
                error = f"GitHub API HTTP {response.status_code}"
        except Exception as error_object:  # offline or blocked network is a normal outcome
            error = f"GitHub API {type(error_object).__name__}"
        if release is None:
            try:
                feed = await self.client.get(FEED_URL.format(repo=self.repo), timeout=15,
                    headers={"User-Agent": USER_AGENT})
                feed.raise_for_status()
                release = normalize_feed(feed.text)
                error = "" if release else error + "; no release published yet"
            except Exception as error_object:
                error += f"; GitHub feed {type(error_object).__name__}"
        if release is not None:
            self.cached, self.checked = release, time.time()
        else:
            self.checked = time.time()
            self.cached = None
        return release, error

    async def check(self, current, force=False):
        """The full status shown by the desktop update banner and the settings dialog."""
        release, error = await self.release(force)
        status = {"current": str(current), "latest": "", "update_available": False,
                  "installer": None, "assets": [], "page": "", "notes": "", "published": "",
                  "channel": self.repo, "checked_at": time.time(), "error": error}
        if not release:
            return status
        return status | {
            "latest": release["version"],
            "update_available": is_newer(release["version"], current),
            "assets": release["assets"],
            "installer": (pick_asset(release["assets"], "installer")
                          or pick_asset(release["assets"], "portable")),
            "page": release["page"], "notes": release["notes"][:4000],
            "published": release["published"], "error": "",
        }
