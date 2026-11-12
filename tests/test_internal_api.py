from fastapi.testclient import TestClient

from banknifty_foundation.internal_api import app, runtime


def test_internal_api_simulation_vertical_slice(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    client = TestClient(app)
    headers = {"X-Internal-Token": "test-token"}
    assert client.get("/internal/health").status_code == 200
    assert client.get("/internal/dashboard").status_code == 401
    started = client.post("/internal/live/simulation/start", headers=headers)
    assert started.status_code == 200 and started.json()["connected"] is True
    duplicate_start = client.post("/internal/live/simulation/start", headers=headers)
    assert duplicate_start.status_code == 409
    assert duplicate_start.json()["error"] == "SIMULATION_ALREADY_RUNNING"
    dashboard = client.get("/internal/dashboard", headers=headers).json()
    assert dashboard["available"] is True and len(dashboard["predictions"]) == 4
    assert all(item["available"] is False for item in dashboard["predictions"])
    assert client.get("/internal/candles", headers=headers).json()["candles"]
    options = client.get("/internal/options", headers=headers).json()
    assert options["rows"] and options["atmStrike"] and options["eligibleExpiries"]
    assert client.get("/internal/futures", headers=headers).json()["available"] is True
    models = client.get("/internal/models/status", headers=headers).json()
    assert models["bundleStatus"] == "UNAVAILABLE"
    assert client.get("/internal/data-quality", headers=headers).status_code == 200
    assert client.get("/internal/predictions/history", headers=headers).json()["items"] == []
    stopped = client.post("/internal/live/simulation/stop", headers=headers)
    assert stopped.status_code == 200 and stopped.json()["connected"] is False
    duplicate_stop = client.post("/internal/live/simulation/stop", headers=headers)
    assert duplicate_stop.status_code == 409
    assert duplicate_stop.json()["error"] == "SIMULATION_NOT_RUNNING"


def test_health_and_dashboard_report_offline_when_live_worker_disabled(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("LIVE_MARKET_ENABLED", "false")
    monkeypatch.setattr(runtime.live_worker, "status", lambda: {
        "state": "CONNECTED", "message": None, "connected": True, "collectorMode": "LIVE",
    })
    runtime.running = False
    client = TestClient(app)
    headers = {"X-Internal-Token": "test-token"}
    health = client.get("/internal/health").json()
    dashboard = client.get("/internal/dashboard", headers=headers).json()
    assert health["collector"]["collectorMode"] == "OFFLINE"
    assert dashboard["dataMode"] == "OFFLINE"
