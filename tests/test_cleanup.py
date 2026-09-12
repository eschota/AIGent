"""Periodic SQLite cleanup removes only old, transient rows."""

import time

import pytest
from fastapi.testclient import TestClient

from connector.app import create_app
from connector.config import Config, password_hash
from connector.store import Store

pytestmark = pytest.mark.usefixtures("tmp_path")


def test_old_stream_events_are_deleted_but_fresh_and_other_kinds_stay(tmp_path):
    store = Store(tmp_path / "cleanup.db")
    sid = store.resolve(1, 0, 1)["id"]
    for _ in range(3):
        store.event(sid, "stream", {"text": "old"})
    store.execute("UPDATE events SET created=? WHERE kind='stream'", (time.time() - 20 * 86400,))
    store.event(sid, "stream", {"text": "fresh"})
    store.event(sid, "assistant", {"text": "old assistant"})
    store.execute("UPDATE events SET created=? WHERE kind='assistant'", (time.time() - 20 * 86400,))

    r = store.cleanup(days=14)

    assert r["stream_events"] == 3
    assert r["total"] == 3
    assert r["vacuum"] is False
    remaining = store.rows("SELECT kind FROM events ORDER BY id")
    assert [row["kind"] for row in remaining] == ["stream", "assistant"]
    store.db.close()


def test_sent_and_cancelled_queued_rows_are_deleted_but_pending_stays(tmp_path):
    store = Store(tmp_path / "cleanup_queue.db")
    sid = store.resolve(2, 0, 1)["id"]
    q1 = store.queue_message(sid, "a")
    q2 = store.queue_message(sid, "b")
    store.take_queued(sid)  # q1 -> sent
    store.drop_queued(sid, q2)  # q2 -> cancelled
    old = time.time() - 20 * 86400
    store.execute("UPDATE queued_messages SET created=? WHERE id IN (?,?)", (old, q1, q2))
    store.queue_message(sid, "c")  # pending and fresh, must stay

    r = store.cleanup(days=14)

    assert r["queued_messages"] == 2
    queued = store.queued(sid)
    assert len(queued) == 1
    assert queued[0]["payload"] == "c"
    store.db.close()


def test_closed_async_questions_are_deleted_but_open_stay(tmp_path):
    store = Store(tmp_path / "cleanup_async.db")
    sid = store.resolve(3, 0, 1)["id"]
    old = time.time() - 20 * 86400
    fresh = time.time()
    sql = ("INSERT INTO async_questions(id,session_id,question,assumption,created,status) "
           "VALUES (?,?,?,?,?,?)")
    store.execute(sql, ("q1", sid, "question 1", "assumption 1", old, "closed"))
    store.execute(sql, ("q2", sid, "question 2", "assumption 2", old, "open"))
    store.execute(sql, ("q3", sid, "question 3", "assumption 3", fresh, "open"))

    r = store.cleanup(days=14)

    assert r["async_questions"] == 1
    remaining = {row["id"] for row in store.rows("SELECT id FROM async_questions")}
    assert remaining == {"q2", "q3"}
    store.db.close()


def test_cleanup_endpoint_returns_counters(tmp_path):
    app = create_app(tmp_path, polling=False)
    config: Config = app.state.config
    config.values.update(admin_password=password_hash("admin-password-1"),
                         chat_password=password_hash("chat-pass"))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        store = app.state.store
        sid = store.resolve(4, 0, 1)["id"]
        event_id = store.event(sid, "stream", {"text": "old"})
        store.execute("UPDATE events SET created=? WHERE id=?", (time.time() - 20 * 86400, event_id))

        response = client.post("/api/maintenance/cleanup", json={"days": 14})

        assert response.status_code == 200
        payload = response.json()
        assert payload["stream_events"] == 1
        assert "queued_messages" in payload
