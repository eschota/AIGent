import json
import sqlite3
import time
import uuid


class Store:
    def __init__(self, path):
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
        CREATE TABLE IF NOT EXISTS usage (
          id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS inbox (
          id INTEGER PRIMARY KEY, payload TEXT, status TEXT DEFAULT 'pending');
        CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
        CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
        """)
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

    def sessions(self):
        return self.rows("SELECT * FROM sessions ORDER BY created DESC LIMIT 300")

    def resolve(self, chat, topic, user, title="New session", new=False):
        args = (chat, topic or 0, user)
        rows = self.rows("SELECT * FROM sessions WHERE chat_id=? AND topic_id=? AND user_id=? "
                         "AND active=1 ORDER BY created DESC LIMIT 1", args)
        if rows and not new:
            return rows[0]
        self.execute("UPDATE sessions SET active=0 WHERE chat_id=? AND topic_id=? AND user_id=?", args)
        sid = uuid.uuid4().hex[:16]
        self.execute("INSERT INTO sessions(id,chat_id,topic_id,user_id,title,created) VALUES (?,?,?,?,?,?)",
                     (sid, *args, title[:100], time.time()))
        return self.session(sid)

    def event(self, sid, kind, payload):
        self.execute("INSERT INTO events(session_id,kind,payload,created) VALUES (?,?,?,?)",
                     (sid, kind, json.dumps(payload, ensure_ascii=False), time.time()))

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
