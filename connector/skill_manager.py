"""Skill Manager: a local index of Markdown skills found in Codex, Claude and AIGent sessions.

Indexing, tagging and search are pure local analysis: a filesystem walk plus Markdown parsing.
No provider is called and no tokens are spent, so the widget can run continuously in the
background. A paid model is never used as a fallback.
"""

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".next",
             ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages", ".idea", ".vs"}
MAX_BYTES = 256 * 1024
MAX_FILES = 4000
STOP_WORDS = {"the", "and", "for", "with", "that", "this", "you", "your", "use", "using", "from",
              "when", "what", "into", "not", "are", "can", "その", "или", "для", "как", "это",
              "если", "тоже", "этот", "всех", "чтобы", "быть"}


def parse_front_matter(text):
    """Read a leading `---` block without a YAML dependency: scalars and simple lists only."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block, rest = text[3:end], text[end + 4:].lstrip("\n")
    data, key = {}, None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key:
            data.setdefault(key, []).append(item.group(1).strip().strip("'\""))
            continue
        found = re.match(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$", line)
        if not found:
            continue
        key, value = found.group(1).lower(), found.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            data[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        elif value:
            data[key] = value.strip("'\"")
        else:
            data[key] = []
    return data, rest


def kind(path: Path):
    """Classify a Markdown file by where it lives: a skill, a memory note, a prompt or a doc."""
    lowered = [part.lower() for part in path.parts]
    if path.name.lower() in ("skill.md", "skills.md") or "skills" in lowered:
        return "skill"
    if "memory" in lowered:
        return "memory"
    if "commands" in lowered or "prompts" in lowered:
        return "prompt"
    if "agents" in lowered or path.name.lower() in ("agents.md", "claude.md"):
        return "instructions"
    if "plugins" in lowered:
        return "plugin"
    return "doc"


def describe(path: Path, text: str, source: str, origin: str):
    """Derive title, summary and tags locally. Deterministic, offline and free."""
    meta, body = parse_front_matter(text)
    heading = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
    title = str(meta.get("name") or meta.get("title") or heading or path.stem)[:150]
    summary = str(meta.get("description") or "")
    if not summary:
        paragraph = []
        for line in body.splitlines():
            if line.startswith(("#", "```", "|", "<!--")):
                if paragraph:
                    break
                continue
            if not line.strip():
                if paragraph:
                    break
                continue
            paragraph.append(line.strip())
        summary = " ".join(paragraph)
    tags = {str(t).lower().strip("#, ") for t in (meta.get("tags") or []) if str(t).strip()}
    tags |= {t.lower() for t in re.findall(r"(?<![\w/])#([A-Za-z][\w-]{2,24})", body)}
    tags.add(source)
    tags.add(kind(path))
    tags |= {part.lower() for part in path.parts[-3:-1] if re.fullmatch(r"[A-Za-z][\w.-]{2,24}", part)}
    for word in re.findall(r"[A-Za-zА-Яа-яЁё][\w-]{3,20}", title.lower()):
        if word not in STOP_WORDS and len(tags) < 16:
            tags.add(word)
    tags.discard("")
    stat = path.stat()
    return {
        "id": hashlib.sha256(str(path).encode("utf-8", "replace")).hexdigest()[:16],
        "path": str(path), "name": path.name, "title": title, "summary": summary[:600],
        "source": source, "origin": origin, "kind": kind(path),
        "tags": json.dumps(sorted(tags), ensure_ascii=False),
        "digest": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        "bytes": stat.st_size, "modified": stat.st_mtime, "indexed": time.time(),
        "preview": text[:4000], "analysis": "local",
    }


class SkillIndex:
    """Background index of `.md` skills. Search, tags and attachment run off the local database."""

    def __init__(self, config, store, workspace=None):
        self.config, self.store, self.workspace = config, store, workspace
        self.task = None
        self.state = {"state": "idle", "scanned": 0, "files": 0, "indexed": 0, "removed": 0,
                      "started": None, "finished": None, "error": None, "roots": [],
                      "analysis": "local", "free_model": None, "tokens_spent": 0}
        self.store.db.executescript("""
        CREATE TABLE IF NOT EXISTS skills (
          id TEXT PRIMARY KEY, path TEXT UNIQUE, name TEXT, title TEXT, summary TEXT,
          source TEXT, origin TEXT, tags TEXT DEFAULT '[]', digest TEXT, bytes INTEGER,
          modified REAL, indexed REAL, uses INTEGER DEFAULT 0, used_at REAL,
          analysis TEXT DEFAULT 'local', kind TEXT DEFAULT 'doc');""")
        columns = {r[1] for r in self.store.db.execute("PRAGMA table_info(skills)")}
        for name, definition in {"preview": "TEXT DEFAULT ''", "uses": "INTEGER DEFAULT 0",
                                 "used_at": "REAL", "analysis": "TEXT DEFAULT 'local'",
                                 "kind": "TEXT DEFAULT 'doc'"}.items():
            if name not in columns:
                self.store.db.execute(f"ALTER TABLE skills ADD COLUMN {name} {definition}")
        self.store.db.commit()

    # ---------------------------------------------------------------- discovery
    def roots(self):
        """Session and configuration directories of the installed agents, plus opened projects."""
        home, found = Path.home(), []
        for source, base, parts in (
            ("claude", home / ".claude", ("skills", "commands", "memory", "plugins", "projects", "agents", "")),
            ("codex", home / ".codex", ("prompts", "sessions", "skills", "")),
        ):
            for part in parts:
                path = base / part if part else base
                if path.is_dir():
                    found.append({"source": source, "path": path, "depth": 4 if part else 1})
        for project in self.store.rows("SELECT name,path FROM projects ORDER BY created DESC"):
            path = Path(project["path"])
            if path.is_dir():
                found.append({"source": "project", "path": path, "depth": 3, "label": project["name"]})
        workspaces = Path(self.config.root) / "workspaces"
        if workspaces.is_dir():
            found.append({"source": "session", "path": workspaces, "depth": 3})
        return found

    def walk(self, root, depth):
        base = root["path"]
        for current, dirs, files in os.walk(base):
            here = Path(current)
            if len(here.relative_to(base).parts) >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".aigent")]
            for name in files:
                if name.lower().endswith((".md", ".markdown")):
                    yield here / name

    # ---------------------------------------------------------------- indexing
    def scan(self):
        started = time.time()
        self.state.update(state="scanning", started=started, finished=None, error=None,
                          scanned=0, files=0, indexed=0, removed=0)
        seen, indexed, scanned = set(), 0, 0
        roots = self.roots()
        self.state["roots"] = [{"source": r["source"], "path": str(r["path"])} for r in roots]
        try:
            for root in roots:
                for path in self.walk(root, root["depth"]):
                    if scanned >= MAX_FILES:
                        break
                    scanned += 1
                    self.state["scanned"] = scanned
                    try:
                        stat = path.stat()
                        if stat.st_size > MAX_BYTES or stat.st_size == 0:
                            continue
                        key = str(path)
                        seen.add(key)
                        known = self.store.rows("SELECT digest,modified FROM skills WHERE path=?", (key,))
                        if known and known[0]["modified"] == stat.st_mtime:
                            continue
                        text = path.read_text(encoding="utf-8", errors="replace")
                        record = describe(path, text, root["source"], root.get("label") or root["path"].name)
                        fields = ",".join(record)
                        self.store.db.execute(
                            f"INSERT INTO skills({fields}) VALUES ({','.join('?' * len(record))}) "
                            f"ON CONFLICT(path) DO UPDATE SET {','.join(f'{k}=excluded.{k}' for k in record if k != 'id')}",
                            tuple(record.values()))
                        indexed += 1
                        self.state["indexed"] = indexed
                    except (OSError, ValueError):
                        continue
            removed = 0
            for row in self.store.rows("SELECT id,path FROM skills"):
                if row["path"] not in seen and not Path(row["path"]).exists():
                    self.store.db.execute("DELETE FROM skills WHERE id=?", (row["id"],))
                    removed += 1
            self.store.db.commit()
            self.state.update(removed=removed)
        except Exception as exc:  # a broken mount must not stop the widget
            self.state["error"] = str(exc)[:300]
        total = self.store.rows("SELECT COUNT(*) AS n FROM skills")[0]["n"]
        self.state.update(state="idle", finished=time.time(), files=total, scanned=scanned,
                          duration=round(time.time() - started, 2))
        return dict(self.state)

    async def loop(self, interval=900, delay=3):
        await asyncio.sleep(delay)
        while True:
            try:
                await asyncio.to_thread(self.scan)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.update(state="idle", error=str(exc)[:300])
            await asyncio.sleep(interval)

    def start(self, interval=900):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.loop(interval))
        return self.task

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    # ---------------------------------------------------------------- reading
    @staticmethod
    def row(record):
        item = {k: v for k, v in record.items() if k != "preview"}
        item["tags"] = json.loads(record.get("tags") or "[]")
        return item

    def status(self, recent=5):
        state = dict(self.state)
        state["total"] = self.store.rows("SELECT COUNT(*) AS n FROM skills")[0]["n"]
        state["progress"] = (1.0 if state["state"] == "idle" and state["finished"]
                             else min(0.99, state["scanned"] / MAX_FILES) if state["scanned"] else 0.0)
        state["recent"] = [self.row(r) for r in self.store.rows(
            "SELECT * FROM skills ORDER BY modified DESC LIMIT ?", (recent,))]
        state["active"] = [self.row(r) for r in self.store.rows(
            "SELECT * FROM skills WHERE uses>0 ORDER BY uses DESC, used_at DESC LIMIT ?", (recent,))]
        state["sources"] = {r["source"]: r["n"] for r in self.store.rows(
            "SELECT source, COUNT(*) AS n FROM skills GROUP BY source")}
        return state

    def search(self, query="", tags=(), source="", sort="relevant", limit=40):
        rows = self.store.rows("SELECT * FROM skills")
        words = [w for w in re.split(r"\s+", query.strip().lower()) if w]
        wanted = [t.lower() for t in tags if t]
        now = time.time()
        results = []
        for row in rows:
            if source and source not in (row["source"], row.get("kind")):
                continue
            row_tags = [t.lower() for t in json.loads(row["tags"] or "[]")]
            if any(t not in row_tags for t in wanted):
                continue
            haystack = " ".join(str(row.get(k) or "") for k in ("name", "title", "summary", "origin")).lower()
            body = (row.get("preview") or "").lower()
            score = 0.0
            for word in words:
                if word in row_tags:
                    score += 6
                if word in (row["title"] or "").lower():
                    score += 4
                if word in haystack:
                    score += 2
                elif word in body:
                    score += 1
                else:
                    score -= 3
            if words and score <= 0:
                continue
            age_days = max(0.0, (now - (row["modified"] or now)) / 86400)
            score += min(5.0, (row["uses"] or 0) * 1.5) + 4 / (1 + age_days / 7)
            results.append((score, row))
        if sort == "recent":
            results.sort(key=lambda item: item[1]["modified"] or 0, reverse=True)
        elif sort == "active":
            results.sort(key=lambda item: ((item[1]["uses"] or 0), item[1]["used_at"] or 0), reverse=True)
        else:
            results.sort(key=lambda item: item[0], reverse=True)
        return [self.row(row) | {"score": round(score, 2)} for score, row in results[:limit]]

    def tag_cloud(self, limit=30):
        counts = {}
        for row in self.store.rows("SELECT tags FROM skills"):
            for tag in json.loads(row["tags"] or "[]"):
                counts[tag] = counts.get(tag, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [{"tag": tag, "count": count} for tag, count in ordered[:limit]]

    def get(self, skill_id, text=True):
        rows = self.store.rows("SELECT * FROM skills WHERE id=?", (skill_id,))
        if not rows:
            raise ValueError("Скилл не найден в индексе")
        record = self.row(rows[0])
        if text:
            path = Path(rows[0]["path"])
            record["text"] = (path.read_text(encoding="utf-8", errors="replace")[:MAX_BYTES]
                              if path.is_file() else rows[0]["preview"] or "")
            record["available"] = path.is_file()
        return record

    # ---------------------------------------------------------------- sharing
    def attach(self, skill_id, sid):
        """Copy a skill into a session workspace so any chat can use and pass it on."""
        from .agent import safe_path

        record = self.get(skill_id)
        if not record.get("available"):
            raise ValueError("Файл скилла больше не доступен на диске")
        root = self.workspace(sid) if self.workspace else Path(self.config.root) / "workspaces" / sid
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", record["name"]).strip("-") or "skill.md"
        relative = "skills/" + (slug if slug.lower().endswith(".md") else slug + ".md")
        target = safe_path(Path(root), relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(record["text"], encoding="utf-8")
        self.store.execute("UPDATE skills SET uses=uses+1, used_at=? WHERE id=?", (time.time(), skill_id))
        self.store.event(sid, "skill_attached", {"id": skill_id, "title": record["title"],
                                                 "path": relative, "source": record["source"],
                                                 "tags": record["tags"]})
        return {"path": relative, "title": record["title"], "id": skill_id, "source": record["source"],
                "tags": record["tags"], "bytes": len(record["text"].encode("utf-8")),
                "reference": f"Скилл «{record['title']}» приложен к сессии: {relative}\n"
                             f"Источник: {record['source']} · {record['origin']}"}
