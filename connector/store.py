import json
import sqlite3
import time
import uuid


class Store:
    def __init__(self, path, recover=False):
        # Synchronous observers of event(); a failing observer never breaks the journal.
        self.observers = []
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, chat_id INTEGER, topic_id INTEGER, user_id INTEGER,
          title TEXT, created REAL, active INTEGER DEFAULT 1, status TEXT DEFAULT 'idle');
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, session_id TEXT, kind TEXT, payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, epoch INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS admin_sessions (
          token_hash TEXT PRIMARY KEY, expires REAL NOT NULL, credential_revision TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS message_requests (
          session_id TEXT, request_id TEXT, text_hash TEXT, created REAL,
          PRIMARY KEY(session_id,request_id));
        CREATE TABLE IF NOT EXISTS usage (
          id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS inbox (
          id INTEGER PRIMARY KEY, payload TEXT, status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS accounts (
          id TEXT PRIMARY KEY, provider TEXT, name TEXT, auth_path TEXT, browser_profile TEXT,
          created REAL, metadata TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, name TEXT, path TEXT UNIQUE, created REAL);
        CREATE TABLE IF NOT EXISTS queued_messages (
          id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, created REAL,
          status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS skills (
          id TEXT PRIMARY KEY, path TEXT UNIQUE, name TEXT, title TEXT, summary TEXT,
          source TEXT, origin TEXT, tags TEXT DEFAULT '[]', digest TEXT, bytes INTEGER,
          modified REAL, indexed REAL, uses INTEGER DEFAULT 0, used_at REAL, analysis TEXT DEFAULT 'local');
        CREATE TABLE IF NOT EXISTS sync_state (
          session_id TEXT PRIMARY KEY, chat_id INTEGER, topic_id INTEGER,
          header_message_id INTEGER, last_event_id INTEGER DEFAULT 0,
          status TEXT DEFAULT 'pending', updated REAL);
        CREATE TABLE IF NOT EXISTS media_files (
          id INTEGER PRIMARY KEY, session_id TEXT, path TEXT, sha256 TEXT, file_id TEXT,
          file_unique_id TEXT, kind TEXT, size INTEGER, message_id INTEGER, uploaded_at REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS media_files_path ON media_files(session_id, path);
        CREATE TABLE IF NOT EXISTS async_questions (
          id TEXT PRIMARY KEY, session_id TEXT, question TEXT, assumption TEXT,
          created REAL, closed REAL, status TEXT DEFAULT 'open', answer TEXT);
        CREATE INDEX IF NOT EXISTS async_questions_session ON async_questions(session_id, status);
        CREATE INDEX IF NOT EXISTS skills_modified ON skills(modified DESC);
        CREATE INDEX IF NOT EXISTS queue_session ON queued_messages(session_id, id);
        CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
        CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}
        for name, definition in {"provider": "TEXT DEFAULT 'deepseek'", "account_id": "TEXT DEFAULT 'deepseek-default'",
                                 "external_id": "TEXT", "model": "TEXT DEFAULT ''", "effort": "TEXT DEFAULT 'medium'",
                                 "workspace": "TEXT", "project_id": "TEXT", "archived": "INTEGER DEFAULT 0",
                                 "pinned": "INTEGER DEFAULT 0", "forked": "INTEGER DEFAULT 0", "deleted": "INTEGER DEFAULT 0",
                                 "auto_approve": "INTEGER DEFAULT 0",
                                 "auto_continue": "INTEGER DEFAULT 1"}.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
        for provider in ("deepseek", "codex", "claude"):
            self.db.execute("INSERT OR IGNORE INTO accounts(id,provider,name,created) VALUES (?,?,?,?)",
                            (provider + "-default", provider, provider.capitalize() + " · текущий аккаунт", time.time()))
        if recover:
            self.db.execute("UPDATE sessions SET status='interrupted' WHERE status IN ('running','approval')")
        self.db.commit()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    def get_state(self, key, default="0"):
        rows = self.rows("SELECT value FROM state WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key, value):
        self.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, str(value)))

    def session(self, sid):
        rows = self.rows("SELECT * FROM sessions WHERE id=?", (sid,))
        return rows[0] if rows else None

    def sessions(self, archived=False, deleted=False):
        if deleted:
            return self.rows("SELECT * FROM sessions WHERE deleted=1 ORDER BY created DESC LIMIT 300")
        return self.rows("SELECT * FROM sessions WHERE archived=? AND deleted=0 ORDER BY pinned DESC,created DESC LIMIT 300", (int(archived),))

    def account(self, aid):
        rows = self.rows("SELECT * FROM accounts WHERE id=?", (aid,))
        return rows[0] if rows else None

    def accounts(self):
        return self.rows("SELECT * FROM accounts ORDER BY created")

    def update_session(self, sid, **fields):
        allowed = {"title", "provider", "account_id", "external_id", "model", "effort", "workspace", "project_id", "archived", "pinned", "forked", "deleted", "auto_approve", "auto_continue"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid session fields")
        self.execute("UPDATE sessions SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), sid))
        return self.session(sid)

    def resolve(self, chat, topic, user, title="New session", new=False):
        args = (chat, topic or 0, user)
        rows = self.rows("SELECT * FROM sessions WHERE chat_id=? AND topic_id=? AND user_id=? "
                         "AND active=1 AND deleted=0 ORDER BY created DESC LIMIT 1", args)
        if rows and not new:
            return rows[0]
        self.execute("UPDATE sessions SET active=0 WHERE chat_id=? AND topic_id=? AND user_id=?", args)
        sid = uuid.uuid4().hex[:16]
        self.execute("INSERT INTO sessions(id,chat_id,topic_id,user_id,title,created) VALUES (?,?,?,?,?,?)",
                     (sid, *args, title[:100], time.time()))
        return self.session(sid)

    def topic_session(self, chat, topic, user=None):
        """The session bound to a forum topic, whoever created it.

        A session mirrored from the IDE starts with user_id 0; the first authorized Telegram
        writer in its topic adopts it, so both sides continue the same conversation.
        """
        rows = self.rows("SELECT * FROM sessions WHERE chat_id=? AND topic_id=? AND deleted=0 "
                         "AND active=1 ORDER BY created DESC", (chat, topic or 0))
        owned = [r for r in rows if user is not None and r["user_id"] == user]
        shared = [r for r in rows if not r["user_id"]]
        found = (owned or shared)
        if not found:
            return None
        session = found[0]
        if user is not None and not session["user_id"]:
            self.execute("UPDATE sessions SET user_id=? WHERE id=?", (user, session["id"]))
            session = self.session(session["id"])
        return session

    def sync_state(self, sid):
        rows = self.rows("SELECT * FROM sync_state WHERE session_id=?", (sid,))
        return rows[0] if rows else None

    def set_sync_state(self, sid, **fields):
        if not self.sync_state(sid):
            self.execute("INSERT INTO sync_state(session_id,updated) VALUES (?,?)", (sid, time.time()))
        if fields:
            allowed = {"chat_id", "topic_id", "header_message_id", "last_event_id", "status"}
            if not set(fields) <= allowed:
                raise ValueError("Invalid sync fields")
            self.execute("UPDATE sync_state SET " + ",".join(f"{k}=?" for k in fields) +
                         ",updated=? WHERE session_id=?", (*fields.values(), time.time(), sid))
        return self.sync_state(sid)

    def media_file(self, sid, path):
        rows = self.rows("SELECT * FROM media_files WHERE session_id=? AND path=?", (sid, path))
        return rows[0] if rows else None

    def media_files(self, sid):
        return self.rows("SELECT * FROM media_files WHERE session_id=? ORDER BY id", (sid,))

    def record_media(self, sid, path, **fields):
        columns = ("sha256", "file_id", "file_unique_id", "kind", "size", "message_id")
        values = [fields.get(c) for c in columns]
        self.execute("INSERT INTO media_files(session_id,path,sha256,file_id,file_unique_id,kind,size,"
                     "message_id,uploaded_at) VALUES (?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(session_id,path) DO UPDATE SET sha256=excluded.sha256,"
                     "file_id=excluded.file_id,file_unique_id=excluded.file_unique_id,kind=excluded.kind,"
                     "size=excluded.size,message_id=excluded.message_id,uploaded_at=excluded.uploaded_at",
                     (sid, path, *values, time.time()))
        return self.media_file(sid, path)

    def event(self, sid, kind, payload):
        cur = self.execute("INSERT INTO events(session_id,kind,payload,created) VALUES (?,?,?,?)",
                           (sid, kind, json.dumps(payload, ensure_ascii=False), time.time()))
        for observer in list(self.observers):
            try:
                observer(sid, kind, payload, cur.lastrowid)
            except Exception:
                pass  # Mirroring is best effort; the event journal is the source of truth.
        return cur.lastrowid

    def events(self, sid, after=0):
        rows = self.rows("SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 500",
                         (sid, after))
        return [r | {"payload": json.loads(r["payload"])} for r in rows]

    def message(self, sid, message):
        self.execute("INSERT INTO messages(session_id,payload,created) VALUES (?,?,?)",
                     (sid, json.dumps(message, ensure_ascii=False), time.time()))

    def history(self, sid):
        return [json.loads(r["payload"]) for r in self.rows(
            "SELECT payload FROM messages WHERE session_id=? ORDER BY id", (sid,))]

    def queue_message(self, sid, content):
        """Persist a turn requested while the session was busy. Nothing is dropped or interrupted."""
        cur = self.execute("INSERT INTO queued_messages(session_id,payload,created) VALUES (?,?,?)",
                           (sid, json.dumps(content, ensure_ascii=False), time.time()))
        return cur.lastrowid

    def queued(self, sid=None, status="pending"):
        sql = "SELECT * FROM queued_messages WHERE status=?" + (" AND session_id=?" if sid else "") + " ORDER BY id"
        rows = self.rows(sql, (status, sid) if sid else (status,))
        return [r | {"payload": json.loads(r["payload"])} for r in rows]

    def take_queued(self, sid):
        rows = self.queued(sid)
        if not rows:
            return None
        self.execute("UPDATE queued_messages SET status='sent' WHERE id=?", (rows[0]["id"],))
        return rows[0]

    def drop_queued(self, sid, qid=None):
        sql = "UPDATE queued_messages SET status='cancelled' WHERE session_id=? AND status='pending'"
        cur = self.execute(sql + (" AND id=?" if qid else ""), (sid, qid) if qid else (sid,))
        return cur.rowcount

    OPEN_QUESTIONS = 3

    def ask_async(self, sid, qid, question, assumption):
        """Record a non-blocking question and retire the oldest ones so they cannot pile up."""
        self.execute("INSERT OR REPLACE INTO async_questions(id,session_id,question,assumption,created,status) "
                     "VALUES (?,?,?,?,?,'open')", (qid, sid, question, assumption, time.time()))
        # A coarse clock gives identical timestamps, so insertion order decides which ones retire.
        extra = self.rows("SELECT id FROM async_questions WHERE session_id=? AND status='open' "
                          "ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?", (sid, self.OPEN_QUESTIONS))
        for row in extra:
            self.close_question(row["id"], "expired")
        return qid

    def open_questions(self, sid):
        return self.rows("SELECT * FROM async_questions WHERE session_id=? AND status='open' ORDER BY created, rowid", (sid,))

    def close_question(self, qid, status="closed", answer=None):
        cur = self.execute("UPDATE async_questions SET status=?, closed=?, answer=? WHERE id=? AND status='open'",
                           (status, time.time(), answer, qid))
        return cur.rowcount

    def close_questions(self, sid, status="superseded"):
        rows = self.open_questions(sid)
        for row in rows:
            self.close_question(row["id"], status)
        return [row["id"] for row in rows]

    def add_usage(self, sid, usage):
        self.execute("INSERT INTO usage(session_id,payload,created) VALUES (?,?,?)",
                     (sid, json.dumps(usage), time.time()))
        self.event(sid, "usage", usage)

    def usage(self, sid=None):
        rows = self.rows("SELECT payload FROM usage" + (" WHERE session_id=?" if sid else ""),
                         (sid,) if sid else ())
        totals = dict(requests=len(rows), prompt_tokens=0, completion_tokens=0,
                      cache_hit_tokens=0, cache_miss_tokens=0, cost_usd=0., saved_usd=0.,
                      unknown_cache_requests=0, unpriced_requests=0)
        for row in rows:
            value = json.loads(row["payload"])
            for key in totals:
                if key != "requests":
                    totals[key] += value.get(key) or 0
        totals["cache_hit_percent"] = (100 * totals["cache_hit_tokens"] / totals["prompt_tokens"]
                                        if totals["prompt_tokens"] else 0)
        return totals
