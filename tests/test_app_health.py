"""The health endpoint reports how long this server process has been up."""

from fastapi.testclient import TestClient

from connector.app import create_app


def test_healthz_reports_uptime_seconds(tmp_path):
    app = create_app(tmp_path, polling=False)
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], int) and body["uptime_seconds"] >= 0
