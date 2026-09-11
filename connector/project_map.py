"""Project map: a free local inventory, a token estimate and a pausable paid DeepSeek scan.

The inventory, the dependency edges, the git summary and the estimate cost nothing: they are a
filesystem walk, `ast`/regex parsing and one `git` subprocess bounded exactly like
`workspace.WorkspaceService.git` (a parent repository is never touched). Only `probe`, `start`
and the single map-generation request spend provider tokens, and every one of them is recorded
through `store.add_usage` tagged `source="project_map"`, so the widget shows real spend.
"""

import asyncio
import ast
import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .providers import DeepSeek, ProviderError

SKIP_DIRS = {".git", ".hg", ".svn", ".local", ".venv", "venv", "env", "node_modules", "__pycache__",
             "Library", "Temp", "Logs", "obj", "build", "dist", ".next", ".nuxt", ".aigent",
             ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages", ".idea", ".vs",
             ".gradle", ".terraform", ".cache", "coverage", "htmlcov"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".pdf",
              ".zip", ".gz", ".bz2", ".xz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
              ".pyc", ".pyd", ".class", ".jar", ".wasm", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
              ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".avi", ".mkv", ".webm",
              ".ttf", ".otf", ".woff", ".woff2", ".eot", ".psd", ".ai", ".blend", ".fbx", ".obj3d",
              ".glb", ".gltf", ".unitypackage", ".asset", ".lockb"}
LANGUAGES = {".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
             ".cjs": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
             ".cs": "C#", ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".rs": "Rust",
             ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".rb": "Ruby",
             ".php": "PHP", ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell",
             ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "CSS", ".less": "CSS",
             ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI",
             ".md": "Markdown", ".markdown": "Markdown", ".rst": "Markdown", ".txt": "Text",
             ".sql": "SQL", ".xml": "XML", ".csv": "Data", ".cfg": "INI", ".shader": "Shader"}
PYTHON_EXT = {".py", ".pyi"}
JS_EXT = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}

MAX_FILES = 4000
MAX_TEXT_BYTES = 200 * 1024
PER_FILE_CHARS = 60000          # what one request may carry from a single file
BATCH_INPUT_TOKENS = 24000      # target input size of one scan request
BATCH_MAX_FILES = 40
SYSTEM_OVERHEAD_TOKENS = 600    # instruction block of every request
OUTPUT_TOKENS_PER_FILE = 350    # one JSON summary per file
SUMMARY_TOKENS_PER_FILE = 120   # what one summary contributes to the map-generation request
MAP_OUTPUT_TOKENS = 500
PROGRESS_INTERVAL = 2.0
MEMORY_LIMIT = 3000

SCAN_INSTRUCTION = (
    "You map a software project. For every file given below, answer with STRICT JSON and nothing "
    "else: {\"files\":[{\"path\":\"...\",\"purpose\":\"1-2 sentences\",\"key_symbols\":[\"...\"],"
    "\"depends_on\":[\"in-project paths or module names\"],\"tags\":[\"short keywords\"]}]}. "
    "Keep one entry per given path, use the exact path string, no markdown, no commentary."
)
MAP_INSTRUCTION = (
    "You are given per-file summaries of one project. Write a project overview of at most 200 words: "
    "what the project is, how its modules fit together, where the risky or central parts are. "
    "Plain text, no markdown headings, no lists."
)


# ------------------------------------------------------------------ free local analysis
def token_estimate(chars, non_ascii_ratio):
    """Cyrillic-heavy prose costs ~3.2 chars per token; code ~4. Deterministic and free."""
    if chars <= 0:
        return 0
    return math.ceil(chars / (3.2 if non_ascii_ratio > 0.2 else 4.0))


def language_of(path: Path):
    return LANGUAGES.get(path.suffix.lower(), "Other")


def redact_remote(url):
    return re.sub(r"//[^/@\s]+@", "//[REDACTED]@", url or "")


def python_imports(text):
    """`import x` / `from x import y` with the relative level kept for resolution."""
    found = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        for match in re.finditer(r"^\s*(?:from\s+([.\w]+)\s+import|import\s+([\w.]+))", text, re.M):
            module = match.group(1) or match.group(2)
            level = len(module) - len(module.lstrip("."))
            found.append((module.lstrip("."), level))
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level or 0))
    return found


def js_imports(text):
    """import/export-from specifiers plus require() and dynamic import()."""
    pattern = (r"""(?:import|export)\s[^;'"]*?from\s*['"]([^'"]+)['"]"""
               r"""|import\s*\(\s*['"]([^'"]+)['"]\s*\)"""
               r"""|require\s*\(\s*['"]([^'"]+)['"]\s*\)"""
               r"""|import\s+['"]([^'"]+)['"]""")
    return [next(g for g in match.groups() if g) for match in re.finditer(pattern, text)]


def _normalise(path):
    return os.path.normpath(path).replace(os.sep, "/")


def resolve_python(known, current, module, level=0):
    """Resolve an import to an in-project file path, or None when it is external."""
    base = Path(current).parent
    for _ in range(max(0, level - 1)):
        base = base.parent
    parts = [p for p in module.split(".") if p] if module else []
    roots = [base] if level else [Path(".")]
    candidates = []
    for root in roots:
        target = root.joinpath(*parts) if parts else root
        candidates += [str(target) + ".py", str(target / "__init__.py")]
        if len(parts) > 1:
            parent = root.joinpath(*parts[:-1])
            candidates += [str(parent) + ".py", str(parent / "__init__.py")]
    for candidate in candidates:
        name = _normalise(candidate)
        if name in known:
            return name
    return None


def resolve_js(known, current, specifier):
    if not specifier.startswith("."):
        return None
    base = _normalise(str(Path(current).parent / specifier))
    for candidate in [base] + [base + ext for ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs")] \
            + [base + "/index" + ext for ext in (".js", ".ts", ".tsx", ".jsx")]:
        if candidate in known:
            return candidate
    return None


def module_of(path):
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "(root)"


class ProjectMap:
    """Inventory, estimate, paid scan job and the generated map for one session workspace."""

    def __init__(self, config, store, agent, skills=None):
        self.config, self.store, self.agent, self.skills = config, store, agent, skills
        self.tasks, self._progress = {}, {}
        self.store.db.executescript("""
        CREATE TABLE IF NOT EXISTS project_maps (
          id TEXT PRIMARY KEY, session_id TEXT, root TEXT, revision TEXT, created REAL,
          payload TEXT, estimate TEXT, usage TEXT);
        CREATE TABLE IF NOT EXISTS project_map_jobs (
          id TEXT PRIMARY KEY, session_id TEXT, root TEXT, mode TEXT, state TEXT, reason TEXT,
          created REAL, updated REAL, cursor INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
          max_cost_usd REAL, payload TEXT);
        CREATE INDEX IF NOT EXISTS project_maps_session ON project_maps(session_id, created DESC);
        CREATE INDEX IF NOT EXISTS project_map_jobs_session ON project_map_jobs(session_id, created DESC);
        """)
        self.store.db.commit()

    # ------------------------------------------------------------ inventory (0 tokens)
    def root(self, sid):
        return self.agent.workspace(sid)

    def walk(self, root: Path):
        records, skipped = [], 0
        for base, dirs, names in os.walk(root, followlinks=False):
            here = Path(base)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".aigent")
                             and not (here / d).is_symlink())
            for name in sorted(names):
                if len(records) >= MAX_FILES:
                    return records, skipped + 1
                path = here / name
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                relative = path.relative_to(root).as_posix()
                suffix = path.suffix.lower()
                item = {"path": relative, "size": stat.st_size, "mtime": stat.st_mtime,
                        "language": language_of(path), "lines": 0, "chars": 0,
                        "binary": suffix in BINARY_EXT, "sha256": "", "truncated": False}
                try:
                    data = path.read_bytes() if stat.st_size <= MAX_TEXT_BYTES else b""
                except OSError:
                    continue
                if stat.st_size > MAX_TEXT_BYTES:
                    item.update(binary=True, note="larger than 200 KB")
                    with path.open("rb") as stream:
                        digest = hashlib.sha256()
                        while chunk := stream.read(262144):
                            digest.update(chunk)
                    item["sha256"] = digest.hexdigest()
                else:
                    item["sha256"] = hashlib.sha256(data).hexdigest()
                    if not item["binary"]:
                        if b"\x00" in data:
                            item["binary"] = True
                        else:
                            text = data.decode("utf-8", errors="replace")
                            ascii_count = sum(1 for ch in text if ord(ch) < 128)
                            ratio = 1 - (ascii_count / len(text)) if text else 0
                            lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
                            item.update(chars=len(text), lines=lines,
                                        non_ascii_ratio=round(ratio, 4),
                                        est_chars=min(len(text), PER_FILE_CHARS),
                                        truncated=len(text) > PER_FILE_CHARS)
                            item["est_tokens"] = token_estimate(item["est_chars"], ratio)
                            if suffix in PYTHON_EXT or suffix in JS_EXT:
                                # Only parsed languages keep their text, and only until edges are built.
                                item["text"] = text
                records.append(item)
        return records, skipped

    def dependencies(self, root: Path, records):
        known = {r["path"] for r in records}
        edges = []
        for item in records:
            if item.get("binary") or "text" not in item:
                continue
            suffix = Path(item["path"]).suffix.lower()
            resolved, external = [], []
            if suffix in PYTHON_EXT:
                for module, level in python_imports(item["text"]):
                    target = resolve_python(known, item["path"], module, level)
                    (resolved if target else external).append(target or module)
            elif suffix in JS_EXT:
                for specifier in js_imports(item["text"]):
                    target = resolve_js(known, item["path"], specifier)
                    (resolved if target else external).append(target or specifier)
            if resolved or external:
                item["imports"] = sorted(set(resolved))
                item["external_imports"] = sorted(set(external))[:20]
            for target in sorted(set(resolved)):
                edges.append({"source": item["path"], "target": target, "origin": "static"})
        for item in records:
            item.pop("text", None)
        return edges

    def git_info(self, root: Path):
        """Same guard as workspace.git: a parent repository is never read or changed."""
        def run(args, timeout=15):
            env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.parent), GIT_TERMINAL_PROMPT="0",
                       GIT_OPTIONAL_LOCKS="0")
            try:
                done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                      timeout=timeout, env=env, text=True, errors="replace")
            except (OSError, subprocess.SubprocessError):
                return None
            return done.stdout.strip() if done.returncode == 0 else None

        top = run(["rev-parse", "--show-toplevel"])
        if not top:
            return {"repository": False, "note": "В рабочей папке нет Git-репозитория."}
        try:
            top_path = Path(top).resolve()
        except OSError:
            return {"repository": False, "note": "Git-корень недоступен."}
        if top_path != root.resolve() and not top_path.is_relative_to(root.resolve()):
            return {"repository": False,
                    "note": "Родительский репозиторий не читается из вложенной рабочей папки."}
        status = run(["status", "--porcelain=v1"]) or ""
        log = run(["log", "-20", "--format=%H%x1f%aI%x1f%s"]) or ""
        remotes = {}
        for line in (run(["remote", "-v"]) or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                remotes[parts[0]] = redact_remote(parts[1])
        commits = []
        for line in log.splitlines():
            pieces = line.split("\x1f")
            if len(pieces) == 3:
                commits.append({"hash": pieces[0][:12], "date": pieces[1], "subject": pieces[2][:200]})
        return {"repository": True, "root": str(top_path),
                "branch": run(["rev-parse", "--abbrev-ref", "HEAD"]) or "",
                "head": (run(["rev-parse", "HEAD"]) or "")[:12],
                "dirty": len([line for line in status.splitlines() if line.strip()]),
                "commits": commits, "remotes": remotes}

    def project_skills(self, root: Path, records):
        """Indexed skills and memory notes that belong to, or mention, this project. Local only."""
        result = {"own": [], "related": [], "memory": []}
        if not self.skills:
            return result
        try:
            rows = self.store.rows("SELECT * FROM skills")
        except Exception:
            return result
        names = {module_of(r["path"]) for r in records} | {root.name}
        names = {n.lower() for n in names if n and n != "(root)" and len(n) > 2}
        base = str(root).lower()
        for row in rows:
            tags = json.loads(row["tags"] or "[]")
            item = {"id": row["id"], "title": row["title"] or row["name"], "path": row["path"],
                    "kind": row.get("kind") or "doc", "source": row["source"], "tags": tags[:8]}
            own = row["path"].lower().startswith(base)
            haystack = " ".join(str(row.get(k) or "") for k in ("title", "summary", "preview")).lower()
            mentions = any(name in haystack or name in " ".join(tags).lower() for name in names)
            if own:
                result["own"].append(item)
            elif mentions:
                result["related"].append(item)
            if (row.get("kind") == "memory") and (own or mentions):
                result["memory"].append(item)
        for key in result:
            result[key] = result[key][:20]
        return result

    def inventory(self, sid, with_text=False):
        root = self.root(sid)
        records, truncated = self.walk(root)
        edges = self.dependencies(root, records)
        info = self.git_info(root)
        skills = self.project_skills(root, records)
        by_language = {}
        for item in records:
            entry = by_language.setdefault(item["language"], {"files": 0, "chars": 0, "est_tokens": 0})
            entry["files"] += 1
            entry["chars"] += item.get("chars", 0)
            entry["est_tokens"] += item.get("est_tokens", 0)
        text_files = [r for r in records if not r.get("binary")]
        revision = info["head"] if info.get("head") else hashlib.sha256(
            "".join(r["sha256"] for r in records).encode()).hexdigest()[:12]
        result = {"root": str(root), "revision": revision, "files": len(records),
                  "text_files": len(text_files), "binary_files": len(records) - len(text_files),
                  "total_chars": sum(r.get("chars", 0) for r in text_files),
                  "truncated": bool(truncated), "by_language": by_language, "git": info,
                  "skills": skills, "edges": edges, "created": time.time(),
                  "records": records if with_text else [
                      {k: v for k, v in r.items() if k != "text"} for r in records]}
        return result

    # ------------------------------------------------------------ estimate (0 tokens)
    @staticmethod
    def batches(records):
        """Split text files into requests of at most ~24k input tokens."""
        plan, current, tokens = [], [], 0
        for item in sorted((r for r in records if not r.get("binary")), key=lambda r: r["path"]):
            cost = item.get("est_tokens", 0)
            if current and (tokens + cost > BATCH_INPUT_TOKENS or len(current) >= BATCH_MAX_FILES):
                plan.append(current)
                current, tokens = [], 0
            current.append({"path": item["path"], "est_tokens": cost, "language": item["language"]})
            tokens += cost
        if current:
            plan.append(current)
        return plan

    def calibration(self, root):
        key = "project_map_calibration:" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
        try:
            value = float(self.store.get_state(key, "1"))
        except (TypeError, ValueError):
            value = 1.0
        return key, round(min(5.0, max(0.2, value)), 4)

    def rates(self, sid):
        session = self.store.session(sid) or {}
        model = session.get("model") or self.config["model"]
        return model, self.config["pricing"].get(model)

    def estimate(self, sid, inventory=None):
        inventory = inventory or self.inventory(sid)
        plan = self.batches(inventory["records"])
        key, factor = self.calibration(inventory["root"])
        files = sum(len(batch) for batch in plan)
        body = sum(item["est_tokens"] for batch in plan for item in batch)
        est_input = math.ceil((body + SYSTEM_OVERHEAD_TOKENS * len(plan)) * factor)
        est_output = math.ceil(OUTPUT_TOKENS_PER_FILE * files * factor)
        map_input = math.ceil((SUMMARY_TOKENS_PER_FILE * files + SYSTEM_OVERHEAD_TOKENS) * factor)
        model, rates = self.rates(sid)
        def price(prompt, output, cached=0.0):
            if not rates:
                return None
            hit_rate, miss_rate, output_rate = rates
            hit = prompt * cached
            return round((hit * hit_rate + (prompt - hit) * miss_rate + output * output_rate) / 1e6, 8)
        result = {
            "files": inventory["files"], "text_files": inventory["text_files"],
            "binary_files": inventory["binary_files"], "total_chars": inventory["total_chars"],
            "scanned_files": files, "est_input_tokens": est_input, "est_output_tokens": est_output,
            "est_requests": len(plan), "est_cost_usd": price(est_input, est_output),
            "est_cost_usd_cached": price(est_input, est_output, cached=0.8),
            "est_cost_map_generation": price(map_input, MAP_OUTPUT_TOKENS),
            "by_language": inventory["by_language"], "calibration": factor,
            "calibration_key": key, "model": model, "priced": bool(rates),
            "pricing_date": self.config["pricing_date"],
            "batch_input_tokens": BATCH_INPUT_TOKENS,
            "formula": "tokens ≈ chars/3.2 (кириллица) или chars/4 (код) + 600 на запрос; "
                       f"выход ≈ {OUTPUT_TOKENS_PER_FILE} токенов на файл",
            "note": "Оценка по тарифу пикового времени; вне пика DeepSeek считает вдвое дешевле.",
        }
        result["total_cost_usd"] = (None if not rates else
                                    round((result["est_cost_usd"] or 0) + (result["est_cost_map_generation"] or 0), 6))
        return result

    # ------------------------------------------------------------ jobs
    def backend(self, session):
        options = dict(self.config.values)
        options["model"] = session.get("model") or self.config["model"]
        account = session.get("account_id", "deepseek-default")
        options["deepseek_key"] = (self.config["deepseek_key"] if account == "deepseek-default"
                                   else self.config["account_keys"].get(account, ""))
        return DeepSeek(options, self.agent.deepseek.client)

    @staticmethod
    async def _silent(text, reasoning):
        return None

    def job(self, job_id):
        rows = self.store.rows("SELECT * FROM project_map_jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        row = rows[0]
        row["payload"] = json.loads(row["payload"] or "{}")
        return row

    def jobs(self, sid, limit=20):
        rows = self.store.rows("SELECT * FROM project_map_jobs WHERE session_id=? "
                               "ORDER BY created DESC LIMIT ?", (sid, limit))
        return [self.public_job(r | {"payload": json.loads(r["payload"] or "{}")}) for r in rows]

    @staticmethod
    def public_job(job):
        payload = job.get("payload") or {}
        return {"id": job["id"], "session_id": job["session_id"], "mode": job["mode"],
                "state": job["state"], "reason": job["reason"], "created": job["created"],
                "updated": job["updated"], "done": job["cursor"], "total": job["total"],
                "max_cost_usd": job["max_cost_usd"], "stats": payload.get("stats", {}),
                "files": payload.get("files", 0), "unparsed": payload.get("unparsed", 0)}

    def _save(self, job_id, **fields):
        payload = fields.pop("payload", None)
        if payload is not None:
            fields["payload"] = json.dumps(payload, ensure_ascii=False)
        fields["updated"] = time.time()
        self.store.execute("UPDATE project_map_jobs SET " + ",".join(f"{k}=?" for k in fields)
                           + " WHERE id=?", (*fields.values(), job_id))

    def _emit(self, job_id, kind="map_progress", force=False):
        job = self.job(job_id)
        if not job:
            return
        now = time.monotonic()
        if kind == "map_progress" and not force and now - self._progress.get(job_id, 0) < PROGRESS_INTERVAL:
            return
        self._progress[job_id] = now
        stats = job["payload"].get("stats", {})
        self.store.event(job["session_id"], kind, {
            "job_id": job_id, "done": job["cursor"], "total": job["total"], "state": job["state"],
            "reason": job["reason"], "tokens": stats.get("prompt_tokens", 0) + stats.get("completion_tokens", 0),
            "cost_usd": stats.get("cost_usd", 0.0), "requests": stats.get("requests", 0)})

    def start(self, sid, mode="summaries", max_cost_usd=None, inventory=None):
        """Create and launch a job. Called from the event loop: it creates the worker task."""
        session = self.store.session(sid)
        if not session:
            raise ValueError("Сессия не найдена")
        if mode not in ("summaries", "full"):
            raise ValueError("Режим может быть summaries или full")
        running = [j for j in self.jobs(sid) if j["state"] in ("running", "pausing", "queued")]
        if running:
            raise ValueError("Сканирование уже идёт. Поставьте его на паузу или отмените.")
        inventory = inventory or self.inventory(sid)
        plan = self.batches(inventory["records"])
        if not plan:
            raise ValueError("В рабочей папке нет текстовых файлов для сканирования.")
        estimate = self.estimate(sid, inventory)
        budget = self.config.values.get("project_map_budget_usd", 0.5) if max_cost_usd is None else max_cost_usd
        job_id = uuid.uuid4().hex[:16]
        payload = {"batches": plan, "estimate": estimate, "results": {}, "raw": {}, "unparsed": 0,
                   "files": sum(len(b) for b in plan), "inventory": {
                       k: v for k, v in inventory.items() if k != "records"},
                   "records": [{k: v for k, v in r.items() if k != "text"} for r in inventory["records"]],
                   "stats": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                             "cache_hit_tokens": 0, "cache_miss_tokens": 0, "cost_usd": 0.0,
                             "elapsed": 0.0}}
        self.store.execute(
            "INSERT INTO project_map_jobs(id,session_id,root,mode,state,reason,created,updated,"
            "cursor,total,max_cost_usd,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, sid, inventory["root"], mode, "running", "", time.time(), time.time(), 0,
             len(plan), float(budget), json.dumps(payload, ensure_ascii=False)))
        self._emit(job_id, "map_progress", force=True)
        self.tasks[job_id] = asyncio.create_task(self._worker(job_id))
        return self.public_job(self.job(job_id))

    def pause(self, job_id):
        job = self._require(job_id)
        if job["state"] not in ("running", "queued"):
            return self.public_job(job)
        # The in-flight request is always finished and paid for; the job stops before the next batch.
        self._save(job_id, state="pausing", reason="Пауза запрошена; текущий запрос будет завершён.")
        return self.public_job(self.job(job_id))

    def resume(self, job_id):
        job = self._require(job_id)
        if job["state"] in ("running", "queued"):
            return self.public_job(job)
        if job["state"] in ("completed", "cancelled"):
            raise ValueError("Эта задача уже завершена.")
        self._save(job_id, state="running", reason="")
        self.tasks[job_id] = asyncio.create_task(self._worker(job_id))
        return self.public_job(self.job(job_id))

    def cancel(self, job_id):
        job = self._require(job_id)
        if job["state"] in ("completed", "cancelled"):
            return self.public_job(job)
        self._save(job_id, state="cancelled", reason="Отменено пользователем.")
        self._emit(job_id, "map_paused", force=True)
        return self.public_job(self.job(job_id))

    def _require(self, job_id):
        job = self.job(job_id)
        if not job:
            raise ValueError("Задача карты не найдена")
        return job

    async def wait(self, job_id, timeout=10):
        task = self.tasks.get(job_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return self.public_job(self._require(job_id))

    # ------------------------------------------------------------ paid work
    def _batch_cost(self, job, batch):
        model, rates = self.rates(job["session_id"])
        if not rates:
            return 0.0
        _, factor = self.calibration(job["root"])
        prompt = (sum(item["est_tokens"] for item in batch) + SYSTEM_OVERHEAD_TOKENS) * factor
        output = OUTPUT_TOKENS_PER_FILE * len(batch) * factor
        return (prompt * rates[1] + output * rates[2]) / 1e6

    def _budget_guard(self, job, batch):
        limit = job["max_cost_usd"]
        if not limit:
            return None
        spent = job["payload"]["stats"]["cost_usd"] or 0.0
        projected = spent + self._batch_cost(job, batch)
        if projected > limit:
            return (f"Достигнут лимит бюджета: потрачено ${spent:.4f}, следующий запрос довёл бы "
                    f"до ${projected:.4f} при лимите ${limit:.4f}.")
        return None

    def _file_text(self, root, relative, limit=PER_FILE_CHARS):
        path = (root / relative)
        try:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                return ""
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return ""

    def _prompt(self, root, batch):
        blocks = []
        for item in batch:
            text = self._file_text(root, item["path"])
            blocks.append(f"### {item['path']} ({item['language']})\n{text}")
        return [{"role": "system", "content": SCAN_INSTRUCTION},
                {"role": "user", "content": "Files:\n\n" + "\n\n".join(blocks)}]

    @staticmethod
    def parse_json(text):
        """Tolerant parse: strip fences and surrounding prose before json.loads."""
        body = (text or "").strip()
        if body.startswith("```"):
            body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
            body = re.sub(r"```\s*$", "", body).strip()
        try:
            return json.loads(body)
        except ValueError:
            start, end = body.find("{"), body.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(body[start:end + 1])
                except ValueError:
                    return None
            return None

    def _account(self, job, usage, elapsed):
        payload = job["payload"]
        stats = payload["stats"]
        stats["requests"] += 1
        stats["elapsed"] = round(stats["elapsed"] + elapsed, 3)
        if usage:
            for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens"):
                stats[key] = (stats.get(key) or 0) + (usage.get(key) or 0)
            stats["cost_usd"] = round((stats["cost_usd"] or 0) + (usage.get("cost_usd") or 0), 8)
            self.store.add_usage(job["session_id"], usage | {
                "source": "project_map", "job_id": job["id"], "provider": "deepseek",
                "billing": "api", "account_id": self.store.session(job["session_id"]).get("account_id")})
        return stats

    async def _request(self, job, messages):
        session = self.store.session(job["session_id"])
        backend = self.backend(session)
        started = time.monotonic()
        message, usage = await backend.complete(messages, [], self._silent)
        return message, usage, time.monotonic() - started

    async def _run_batch(self, job_id, batch):
        job = self._require(job_id)
        root = Path(job["root"])
        message, usage, elapsed = await self._request(job, self._prompt(root, batch))
        job = self._require(job_id)
        payload = job["payload"]
        self._account(job, usage, elapsed)
        parsed = self.parse_json((message or {}).get("content") or "")
        entries = (parsed or {}).get("files") if isinstance(parsed, dict) else parsed
        found = {}
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    found[entry["path"]] = {
                        "purpose": str(entry.get("purpose") or "")[:600],
                        "key_symbols": [str(s)[:80] for s in (entry.get("key_symbols") or [])][:20],
                        "depends_on": [str(s)[:200] for s in (entry.get("depends_on") or [])][:20],
                        "tags": [str(s)[:40] for s in (entry.get("tags") or [])][:12]}
        for item in batch:
            if item["path"] in found:
                payload["results"][item["path"]] = found[item["path"]]
            else:
                payload["results"][item["path"]] = {"purpose": "", "key_symbols": [], "depends_on": [],
                                                    "tags": [], "unparsed": True}
                payload["unparsed"] += 1
                payload["raw"][item["path"]] = ((message or {}).get("content") or "")[:2000]
        self._save(job_id, cursor=job["cursor"] + 1, payload=payload)

    async def _worker(self, job_id):
        try:
            while True:
                job = self.job(job_id)
                if not job:
                    break
                if job["state"] == "pausing":
                    # Pause arrived between batches: nothing is in flight, so stop right here.
                    self._save(job_id, state="paused", reason="Пауза по запросу. Продолжите со следующего пакета.")
                    self._emit(job_id, "map_paused", force=True)
                    break
                if job["state"] != "running":
                    break
                batches = job["payload"]["batches"]
                if job["cursor"] >= len(batches):
                    await self._finish(job_id)
                    break
                guard = self._budget_guard(job, batches[job["cursor"]])
                if guard:
                    self._save(job_id, state="paused", reason=guard)
                    self._emit(job_id, "map_paused", force=True)
                    break
                try:
                    await self._run_batch(job_id, batches[job["cursor"]])
                except (ProviderError, ValueError, OSError) as exc:
                    self._save(job_id, state="paused",
                               reason="Запрос не выполнен: " + self.config.redact(exc))
                    self._emit(job_id, "map_paused", force=True)
                    break
                state = (self.job(job_id) or {}).get("state")
                if state == "pausing":
                    self._save(job_id, state="paused", reason="Пауза по запросу. Продолжите со следующего пакета.")
                    self._emit(job_id, "map_paused", force=True)
                    break
                if state != "running":
                    self._emit(job_id, "map_paused", force=True)
                    break
                self._emit(job_id, "map_progress")
        except asyncio.CancelledError:
            self._save(job_id, state="paused", reason="Остановлено при завершении работы сервера.")
            raise
        except Exception as exc:  # a broken scan must never take the server down
            self._save(job_id, state="failed", reason=self.config.redact(f"{type(exc).__name__}: {exc}")[:300])
            self._emit(job_id, "map_paused", force=True)
        finally:
            self.tasks.pop(job_id, None)

    async def _finish(self, job_id):
        job = self._require(job_id)
        payload = job["payload"]
        overview, usage = "", None
        summaries = "\n".join(
            f"{path}: {data.get('purpose', '')} [{', '.join(data.get('tags') or [])}]"
            for path, data in list(payload["results"].items())[:600] if not data.get("unparsed"))
        guard = None
        _, rates = self.rates(job["session_id"])
        if job["max_cost_usd"] and rates:
            projected = payload["stats"]["cost_usd"] + (
                (SUMMARY_TOKENS_PER_FILE * payload["files"] + SYSTEM_OVERHEAD_TOKENS) * rates[1]
                + MAP_OUTPUT_TOKENS * rates[2]) / 1e6
            if projected > job["max_cost_usd"]:
                guard = (f"Сводка карты не запрошена: лимит ${job['max_cost_usd']:.4f} был бы превышен "
                         f"(≈ ${projected:.4f}).")
        if summaries and not guard:
            try:
                message, usage, elapsed = await self._request(job, [
                    {"role": "system", "content": MAP_INSTRUCTION},
                    {"role": "user", "content": summaries[:60000]}])
                overview = ((message or {}).get("content") or "").strip()[:2000]
                job = self._require(job_id)
                payload = job["payload"]
                self._account(job, usage, elapsed)
            except (ProviderError, ValueError, OSError) as exc:
                guard = "Обзор проекта не получен: " + self.config.redact(exc)
        document = self.build_map(job, payload, overview)
        self.store.execute("INSERT INTO project_maps(id,session_id,root,revision,created,payload,estimate,usage)"
                           " VALUES (?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex[:16], job["session_id"], job["root"],
                            document["revision"], time.time(),
                            json.dumps(document, ensure_ascii=False),
                            json.dumps(payload["estimate"], ensure_ascii=False),
                            json.dumps(payload["stats"], ensure_ascii=False)))
        self._save(job_id, state="completed", reason=guard or "", payload=payload)
        self._emit(job_id, "map_completed", force=True)

    def build_map(self, job, payload, overview=""):
        inventory = payload["inventory"]
        records = payload["records"]
        results = payload["results"]
        modules, positions = {}, {}
        for item in records:
            name = module_of(item["path"])
            module = modules.setdefault(name, {"name": name, "files": [], "languages": {},
                                               "chars": 0, "tags": {}, "skills": []})
            summary = results.get(item["path"], {})
            module["files"].append({"path": item["path"], "language": item["language"],
                                    "lines": item.get("lines", 0), "size": item["size"],
                                    "purpose": summary.get("purpose", ""),
                                    "key_symbols": summary.get("key_symbols", []),
                                    "tags": summary.get("tags", []),
                                    "unparsed": bool(summary.get("unparsed"))})
            module["languages"][item["language"]] = module["languages"].get(item["language"], 0) + 1
            module["chars"] += item.get("chars", 0)
            for tag in summary.get("tags", []):
                module["tags"][tag] = module["tags"].get(tag, 0) + 1
            positions[item["path"]] = name
        weights = {}
        for edge in inventory.get("edges", []):
            source, target = positions.get(edge["source"]), positions.get(edge["target"])
            if source and target and source != target:
                weights[(source, target, "static")] = weights.get((source, target, "static"), 0) + 1
        for path, summary in results.items():
            source = positions.get(path)
            for dependency in summary.get("depends_on", []):
                target = positions.get(dependency) or (dependency if dependency in modules else None)
                if source and target and source != target:
                    weights[(source, target, "model")] = weights.get((source, target, "model"), 0) + 1
        edges = [{"source": s, "target": t, "origin": o, "weight": w}
                 for (s, t, o), w in sorted(weights.items(), key=lambda kv: -kv[1])][:300]
        skills = inventory.get("skills", {})
        catalogue = (skills.get("own", []) + skills.get("related", []))
        for module in modules.values():
            module["tags"] = sorted(module["tags"], key=lambda t: -module["tags"][t])[:8]
            tags = {t.lower() for t in module["tags"]} | {module["name"].lower()}
            module["skills"] = [s for s in catalogue
                                if module["name"].lower() in (s["path"] or "").lower()
                                or tags & {t.lower() for t in s["tags"]}][:6]
            module["count"] = len(module["files"])
        return {"root": job["root"], "revision": inventory.get("revision", ""), "created": time.time(),
                "mode": job["mode"], "job_id": job["id"], "overview": overview,
                "modules": sorted(modules.values(), key=lambda m: -m["count"]),
                "edges": edges, "git": inventory.get("git", {}), "skills": skills,
                "memory": skills.get("memory", []), "stats": payload["stats"],
                "files": inventory.get("files", 0), "unparsed": payload.get("unparsed", 0)}

    # ------------------------------------------------------------ probe & reading
    async def probe(self, sid, count=3):
        """One small real request: actual usage versus the estimate, then store the calibration."""
        session = self.store.session(sid)
        if not session:
            raise ValueError("Сессия не найдена")
        inventory = await asyncio.to_thread(self.inventory, sid)
        candidates = sorted((r for r in inventory["records"] if not r.get("binary") and r.get("chars")),
                            key=lambda r: r.get("est_tokens", 0))
        batch = [{"path": r["path"], "est_tokens": r["est_tokens"], "language": r["language"]}
                 for r in candidates[:count]]
        if not batch:
            raise ValueError("В рабочей папке нет текстовых файлов для проверки.")
        root = Path(inventory["root"])
        expected_input = sum(item["est_tokens"] for item in batch) + SYSTEM_OVERHEAD_TOKENS
        job = {"id": "probe", "session_id": sid, "root": inventory["root"],
               "payload": {"stats": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                     "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                                     "cost_usd": 0.0, "elapsed": 0.0}}}
        message, usage, elapsed = await self._request(job, self._prompt(root, batch))
        stats = self._account(job, usage, elapsed)
        actual_input = stats["prompt_tokens"]
        key, previous = self.calibration(inventory["root"])
        factor = previous
        if actual_input and expected_input:
            factor = round(min(5.0, max(0.2, actual_input / expected_input)), 4)
            self.store.set_state(key, factor)
        parsed = self.parse_json((message or {}).get("content") or "")
        estimate = self.estimate(sid, inventory)
        return {"files": [item["path"] for item in batch], "expected_input_tokens": expected_input,
                "actual_input_tokens": actual_input, "actual_output_tokens": stats["completion_tokens"],
                "cost_usd": stats["cost_usd"], "elapsed": stats["elapsed"],
                "calibration": factor, "previous_calibration": previous,
                "parsed": bool(parsed), "estimate": estimate,
                "note": f"Калибровка ×{factor:g} применена к последующим оценкам."}

    def latest(self, sid):
        rows = self.store.rows("SELECT * FROM project_maps WHERE session_id=? ORDER BY created DESC LIMIT 1",
                               (sid,))
        if not rows:
            return None
        row = rows[0]
        return {"id": row["id"], "created": row["created"], "revision": row["revision"],
                "map": json.loads(row["payload"]), "estimate": json.loads(row["estimate"] or "{}"),
                "usage": json.loads(row["usage"] or "{}")}

    def state(self, sid):
        latest = self.latest(sid)
        jobs = self.jobs(sid)
        active = next((j for j in jobs if j["state"] in ("running", "pausing", "paused", "queued")), None)
        return {"map": latest, "jobs": jobs, "active": active,
                "memory_context": self.memory_context(sid) if latest else "",
                "budget_usd": self.config.values.get("project_map_budget_usd", 0.5)}

    def memory_context(self, sid):
        """A compact project memory the agent can carry into its context. ≤ 3000 characters."""
        latest = self.latest(sid)
        if not latest:
            return ""
        document = latest["map"]
        git = document.get("git") or {}
        lines = ["Карта проекта AIGent (построена " +
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(document.get("created", time.time()))) + ")",
                 "Корень: " + document.get("root", "")]
        if git.get("repository"):
            lines.append(f"Git: ветка {git.get('branch') or '—'} · HEAD {git.get('head') or '—'} · "
                         f"изменённых файлов: {git.get('dirty', 0)}")
        if document.get("overview"):
            lines.append("Обзор: " + document["overview"])
        for module in document.get("modules", [])[:12]:
            purposes = "; ".join(f["purpose"] for f in module["files"][:3] if f.get("purpose"))
            lines.append(f"— {module['name']} ({module['count']} файлов): {purposes[:300]}")
        edges = ", ".join(f"{e['source']}→{e['target']}" for e in document.get("edges", [])[:12])
        if edges:
            lines.append("Связи: " + edges)
        titles = [s["title"] for s in (document.get("skills", {}).get("own", [])
                                       + document.get("skills", {}).get("related", []))[:8]]
        if titles:
            lines.append("Скиллы проекта: " + ", ".join(titles))
        notes = [s["title"] for s in document.get("memory", [])[:6]]
        if notes:
            lines.append("Заметки памяти: " + ", ".join(notes))
        text = "\n".join(lines)
        return text[:MEMORY_LIMIT]

    async def close(self):
        tasks = [t for t in self.tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
