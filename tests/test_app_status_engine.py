"""/api/status reports how many engine runtimes are alive."""

from fastapi.testclient import TestClient

from connector.app import create_app
from connector.config import password_hash


def test_status_reports_live_engine_runtimes(tmp_path):
    app = create_app(tmp_path, polling=False)
    app.state.config.values.update(admin_password=password_hash("admin-password-1"),
                                   chat_password=password_hash("chat-pass"))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"

        body = client.get("/api/status").json()

    runtimes = body["engine_runtimes"]
    assert isinstance(runtimes, int) and not isinstance(runtimes, bool)
    assert runtimes >= 0
